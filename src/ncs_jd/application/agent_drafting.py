"""Agent-loop drafting: the provider drives NCS MCP itself and writes the fields.

The one-shot selector in :mod:`ncs_jd.application.llm_scope_selection` hands a
pre-fetched candidate list to the provider, which cannot then go back and look
something up.  That is the gap against an interactive session, where the model
searches, reads unit detail, and searches again until it is satisfied.

This module models the interactive behaviour instead: the provider is given the
NCS MCP tools directly and loops on its own.  Two consequences follow, and both
are deliberate.

* Output is *composed* by the provider, not assembled from KSA raw text, so
  values here do not carry per-sentence ``source_ref`` the way
  ``JobProfile`` fields do.  ``AgentDraftResult.unit_codes`` records which units
  the run consulted, and callers must mark these fields as agent-composed.
* Runs take minutes and an unknown number of steps, so progress is reported as
  a stream of :class:`AgentProgress` events rather than a percentage.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ncs_jd.application.ncs_source import ScopeCandidate
from ncs_jd.application.template_mapping import TemplateField


ProgressKind = Literal[
    "started",
    "tool_call",
    "tool_result",
    "notice",
    "composing",
    "completed",
    "failed",
]

MAX_TEMPLATE_LABELS = 40
# Per-field budget when a form has many items.  A form with few items packs the
# same job description into fewer cells, so the allowance is spread from a
# document-wide total instead of being a flat per-field number -- otherwise a
# five-item form is rejected for the very content a thirteen-item form fits.
MAX_FIELD_VALUE_CHARS = 8000
MAX_DOCUMENT_CHARS = 80_000
TRUNCATION_MARKER = " …[분량 제한으로 잘림]"
# A form item name; anything longer is prose the detector mistook for a label.
MAX_LABEL_CHARS = 60
# Captions that name a table column rather than a job attribute.  Only applied
# to the first row, so a form that legitimately has an item by one of these
# names further down keeps it.
_COLUMN_CAPTIONS = frozenset({"항목", "내용", "구분", "번호", "순번", "항목명"})
# Filled from the adopted units rather than by the model, so the agent is never
# asked for them and never allowed to return them.
CLASSIFICATION_FIELD_LABELS = ("대분류", "중분류", "소분류", "세분류")


class AgentDraftError(RuntimeError):
    """Normalized agent-loop failure carrying no raw CLI or identity detail."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AgentProgress:
    """One observable step, suitable for a live progress list in the UI."""

    kind: ProgressKind
    step: int
    label: str
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "step": self.step,
            "label": self.label,
            "detail": self.detail,
        }


ProgressCallback = Callable[[AgentProgress], None]


@dataclass(frozen=True, slots=True)
class AgentDraftRequest:
    """Human-confirmed announcement facts plus the target template labels."""

    job_title: str
    duties: tuple[str, ...]
    template_labels: tuple[str, ...]
    qualifications: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    organization_context: str = ""

    def __post_init__(self) -> None:
        if not self.job_title.strip():
            raise ValueError("job_title must not be empty")
        if not self.duties:
            raise ValueError("duties must not be empty")
        if not self.template_labels:
            raise ValueError("template_labels must not be empty")
        if len(self.template_labels) > MAX_TEMPLATE_LABELS:
            raise ValueError("too many template labels")
        if len(set(self.template_labels)) != len(self.template_labels):
            raise ValueError("template labels must be unique")


@dataclass(frozen=True, slots=True)
class AgentDraftResult:
    """Composed field values plus the NCS units the run actually consulted."""

    field_values: tuple[tuple[str, str], ...]
    unit_codes: tuple[str, ...]
    notes: tuple[str, ...] = ()
    turns: int = 0
    duration_ms: int = 0
    tool_calls: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "field_values": [list(pair) for pair in self.field_values],
            "unit_codes": list(self.unit_codes),
            "notes": list(self.notes),
            "turns": self.turns,
            "duration_ms": self.duration_ms,
            "tool_calls": self.tool_calls,
        }


@runtime_checkable
class AgentDraftPort(Protocol):
    """Run the agent loop, reporting progress as it goes."""

    def run_draft(
        self,
        request: AgentDraftRequest,
        on_progress: ProgressCallback | None = None,
    ) -> AgentDraftResult: ...


def template_labels_from_inspection(
    fields: Iterable[TemplateField],
    *,
    limit: int = MAX_TEMPLATE_LABELS,
) -> tuple[str, ...]:
    """Reduce a form's detected cells to the item list the agent should fill.

    Form detection is heuristic and reports every label-looking cell, so three
    kinds of non-item make it through.  All are dropped here because the agent
    fills whatever label it is handed:

    * A table's column header (``항목 | 내용``).  Filling it overwrites the
      form's own caption with invented content.  It is identified structurally
      -- first row, and the label itself is a generic column caption -- so a
      real item never matches.
    * Prose that happens to sit in the label column.  A paragraph is not an
      item name, and filling it fabricates a whole section.
    * A sample answer the form ships with.  Detection pairs each cell with the
      one after it, so across a merged or multi-column table the sample's own
      values arrive as items of their own -- an institution form filled in with
      an example post reports ``전문직(국제교류)`` and ``02. 경영∙회계∙사무``
      alongside the real items.  They are dropped by position: an item name
      lives in the label column, and those copies always sit right of it.

    Whatever survives is shown to a person before the run starts, so a bad
    reading costs a glance rather than minutes of searching.
    """

    if limit < 1:
        raise ValueError("limit must be positive")
    selected: list[str] = []
    seen: set[str] = set()
    for field in fields:
        # An inspector that reports no column is trusted, since nothing
        # contradicts it; only a cell known to sit further right is a value.
        if field.col is not None and field.col > 0:
            continue
        label = " ".join(str(field.label).split())
        if not label or len(label) > MAX_LABEL_CHARS or label in seen:
            continue
        if field.row == 0 and label in _COLUMN_CAPTIONS:
            continue
        if "".join(label.split()) in CLASSIFICATION_FIELD_LABELS:
            # Read back from the adopted units instead; see
            # classification_field_values.
            continue
        seen.add(label)
        selected.append(label)
        if len(selected) == limit:
            break
    return tuple(selected)


def classification_field_values(
    candidates: Iterable[ScopeCandidate],
) -> tuple[tuple[str, str], ...]:
    """Render the 대분류~세분류 cells from the units the agent adopted.

    The classification is not something to write: it is already recorded against
    every unit in the NCS database, so it is read back from the units the run
    chose rather than asked of the model.  That keeps the four cells consistent
    with the능력단위 list beside them and removes the one place a rephrasing step
    could silently move a job into the wrong industry.

    Units from several subcategories are kept in the order first seen and joined
    per level, which is how the published forms show a multi-subcategory post.
    """

    levels: dict[str, list[str]] = {label: [] for label in CLASSIFICATION_FIELD_LABELS}
    for candidate in candidates:
        parts = (
            (candidate.major_code, candidate.major_name),
            (candidate.middle_code, candidate.middle_name),
            (candidate.small_code, candidate.small_name),
            (candidate.sub_code, candidate.sub_name),
        )
        if any(not (code or "").strip() or not (name or "").strip() for code, name in parts):
            # A partial path would print a level that silently disagrees with the
            # others, so the whole candidate is skipped.
            continue
        for label, (code, name) in zip(CLASSIFICATION_FIELD_LABELS, parts, strict=True):
            entry = f"{str(code).strip()}. {str(name).strip()}"
            if entry not in levels[label]:
                levels[label].append(entry)
    return tuple(
        (label, " / ".join(entries)) for label, entries in levels.items() if entries
    )


def field_value_budget(label_count: int) -> int:
    """Characters one field may hold, given how many the form has."""

    if label_count < 1:
        raise ValueError("label_count must be positive")
    return max(MAX_FIELD_VALUE_CHARS, MAX_DOCUMENT_CHARS // label_count)


def agent_draft_prompt(request: AgentDraftRequest) -> str:
    """Render the Korean instruction for the tool-using agent loop.

    The search guidance is not decoration.  ``ncs_search`` matches literal
    substrings, so multi-word queries mostly return nothing, and querying a
    subcategory *name* returns that subcategory's whole roster because every
    unit row carries its ``duty_definition``.  Stating both rules up front
    removes the wasted NOT_FOUND round trips an unguided run spends on them.
    """

    import json

    payload = {
        "직무명": request.job_title,
        "직무수행내역": list(request.duties),
        "공고_자격요건": list(request.qualifications),
        "공고_우대사항": list(request.preferences),
        "기관_맥락": request.organization_context,
        "작성할_양식_항목": list(request.template_labels),
    }
    return (
        "당신은 NCS 기반 채용 직무기술서를 작성합니다. 근거는 오직 ncs_search와 "
        "ncs_unit_detail 도구로 조회한 NCS DB 레코드에서만 가져오세요.\n"
        "\n"
        "[검색 규칙 — 지키지 않으면 대부분 NOT_FOUND가 납니다]\n"
        "1. ncs_search는 부분 문자열 매칭입니다. 공백이 들어간 다중 키워드는 실패합니다.\n"
        "   '전기설비 유지보수' → 실패 / '수변전설비' → 성공. 띄어쓰기 없는 단일 복합명사로 넣으세요.\n"
        "2. NOT_FOUND가 나오면 즉시 더 짧은 핵심 명사로 분해해 재시도하세요.\n"
        "3. 세분류명을 query로 넣으면 그 세분류의 능력단위가 전량 반환됩니다.\n"
        "   각 unit 레코드의 path.duty_definition까지 매칭되기 때문입니다.\n"
        "4. limit 기본값은 20입니다. 탐색은 작게(10~20), 세분류 전량 수집은 크게(100~500) 쓰세요.\n"
        "   결과 개수가 limit과 같으면 잘렸다는 뜻이니 limit을 올려 다시 부르세요.\n"
        "5. ncs_unit_detail은 include=[\"elements\",\"ksa\"]로 부르세요. KSA는 elements 하위에 "
        "중첩되어 있어서 elements를 빼면 KSA도 사라집니다.\n"
        "\n"
        "[작업 절차]\n"
        "1. 직무수행내역에서 설비군·업무군 키워드를 뽑습니다.\n"
        "2. 키워드별로 ncs_search하여 후보 능력단위를 모으고 path.sub(세분류) 분포를 봅니다.\n"
        "3. 공고를 가장 잘 덮는 세분류를 고르고, 세분류명으로 재검색해 능력단위 전량을 확보합니다.\n"
        "4. 채택 후보는 ncs_unit_detail로 열어 능력단위정의·요소·KSA를 확인하고 최종 선별합니다.\n"
        "   이름만 보고 고르지 마세요. 업종 특화 단위(예: 병원·항공·선박 전용)는 기관 성격과 "
        "맞지 않으면 제외하세요.\n"
        "5. 세분류는 최대 3개까지만 채택하세요. 능력단위는 총 25개를 넘기지 마세요.\n"
        "\n"
        "[작성 규칙]\n"
        "- '작성할_양식_항목'의 각 항목을 채웁니다. 항목명을 바꾸거나 추가하지 마세요.\n"
        "- 필요지식·필요기술·직무수행태도는 조회한 KSA 원문 표현을 유지하고 중복을 제거하세요.\n"
        "- 직업기초능력은 NCS DB에서 도출되지 않습니다. 선정하되 'KSA에서 직접 도출된 항목이 "
        "아님'을 해당 값 안에 명시하세요.\n"
        "- 공고 자격요건·우대사항은 원문 그대로 보존하고 새 자격·학력·경력연수를 만들지 마세요.\n"
        "- DB에서 근거를 찾지 못한 내용은 쓰지 말고 notes에 남기세요.\n"
        f"- 각 항목의 value는 {field_value_budget(len(request.template_labels))}자를 넘기지 "
        "마세요. 넘으면 뒤가 잘립니다. 항목 수가 적으면 한 항목에 여러 주제를 묶되 "
        "핵심을 앞에 쓰세요.\n"
        "\n"
        "[출력]\n"
        "마지막 메시지는 설명 없이 아래 JSON 하나만 출력하세요. 코드펜스도 쓰지 마세요.\n"
        '{"fields":[{"label":"<양식 항목명>","value":"<내용>"}],'
        '"unit_codes":["<채택한 능력단위 코드>"],'
        '"notes":["<검토가 필요한 사항>"]}\n'
        "\nINPUT=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def validate_agent_draft(
    payload: Mapping[str, object],
    request: AgentDraftRequest,
    *,
    known_unit_codes: Sequence[str] | None = None,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[str, ...]]:
    """Validate the agent's JSON answer against the requested labels.

    Labels are pinned to the template so the agent cannot invent a field, and
    ``known_unit_codes`` — when the caller has re-checked them against the MCP —
    drops any code the source does not actually have.  Unknown codes are removed
    rather than failing the run, because the prose may still be sound while one
    citation is wrong; the caller surfaces the drop through review flags.
    """

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise AgentDraftError("invalid_agent_fields", retryable=True)

    allowed_labels = set(request.template_labels)
    budget = field_value_budget(len(request.template_labels))
    values: list[tuple[str, str]] = []
    seen: set[str] = set()
    truncated: list[str] = []
    for item in raw_fields:
        if not isinstance(item, Mapping):
            raise AgentDraftError("invalid_agent_fields", retryable=True)
        label, value = item.get("label"), item.get("value")
        if not isinstance(label, str) or label not in allowed_labels:
            raise AgentDraftError("agent_field_label_unknown", retryable=False)
        if label in seen:
            raise AgentDraftError("agent_field_label_duplicated", retryable=False)
        if not isinstance(value, str):
            raise AgentDraftError("invalid_agent_fields", retryable=True)
        normalized = value.strip()
        if len(normalized) > budget:
            # Discarding a run that took minutes over one long field would throw
            # away sound prose for a formatting bound.  The cut is marked, noted
            # for review, and the field is editable before export.
            normalized = normalized[: budget - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
            truncated.append(label)
        seen.add(label)
        values.append((label, normalized))

    allowed_codes = set(known_unit_codes) if known_unit_codes is not None else None
    codes: list[str] = []
    for item in payload.get("unit_codes") or ():
        if not isinstance(item, str):
            continue
        code = item.strip()
        if not code or code in codes:
            continue
        if allowed_codes is not None and code not in allowed_codes:
            continue
        codes.append(code)

    notes: list[str] = []
    if truncated:
        notes.append(
            f"분량 제한({budget}자)으로 다음 항목의 뒷부분이 잘렸습니다: "
            + ", ".join(truncated)
            + ". 내보내기 전에 내용을 확인하고 필요하면 직접 줄여 주세요."
        )
    for item in payload.get("notes") or ():
        if isinstance(item, str) and (note := " ".join(item.split())):
            if note not in notes:
                notes.append(note)

    return tuple(values), tuple(codes), tuple(notes)


__all__ = [
    "CLASSIFICATION_FIELD_LABELS",
    "MAX_DOCUMENT_CHARS",
    "MAX_FIELD_VALUE_CHARS",
    "MAX_LABEL_CHARS",
    "MAX_TEMPLATE_LABELS",
    "TRUNCATION_MARKER",
    "field_value_budget",
    "AgentDraftError",
    "AgentDraftPort",
    "AgentDraftRequest",
    "AgentDraftResult",
    "AgentProgress",
    "ProgressCallback",
    "ProgressKind",
    "agent_draft_prompt",
    "classification_field_values",
    "template_labels_from_inspection",
    "validate_agent_draft",
]
