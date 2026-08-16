"""LLM-assisted NCS scope selection over an MCP-returned candidate pool.

The deterministic planner scores subcategories with character-bigram overlap.
That cannot bridge vocabulary gaps such as "차량 입출차시스템" and "주차관제설비"
or "CCTV" and "영상정보처리기기", so the correct competency units are sometimes
unreachable no matter how the weights are tuned.  This module lets an official
CLI provider make that one decision instead.

The selector is deliberately *retrieval only*.  It receives candidates that the
NCS MCP already returned and may answer with nothing but ``unit_code`` values
drawn from that pool.  Every selected unit is then rebuilt from its MCP row, so
unit names, levels, definitions, and classification codes never originate from
the model.  A response naming an unknown code is rejected whole rather than
partially applied, and callers keep the deterministic planner as the fallback.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


MAX_SELECTABLE_UNITS = 25
MAX_SELECTION_REASON_LENGTH = 300


class ScopeSelectionError(RuntimeError):
    """Normalized selector failure that must fall back to the deterministic plan."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ScopeSelectionCandidate:
    """One MCP-returned unit, flattened to the fields a selector may read."""

    unit_code: str
    unit_name: str
    classification_label: str
    unit_definition: str | None = None
    duty_definition: str | None = None

    def as_prompt_dict(self) -> dict[str, str]:
        payload = {
            "unit_code": self.unit_code,
            "unit_name": self.unit_name,
            "classification": self.classification_label,
        }
        if self.unit_definition:
            payload["unit_definition"] = self.unit_definition
        if self.duty_definition:
            payload["duty_definition"] = self.duty_definition
        return payload


@dataclass(frozen=True, slots=True)
class ScopeSelectionRequest:
    """Announcement facts plus the closed candidate pool a selector may use."""

    job_title: str
    duties: tuple[str, ...]
    candidates: tuple[ScopeSelectionCandidate, ...]

    def __post_init__(self) -> None:
        if not self.duties:
            raise ValueError("duties must not be empty")
        if not self.candidates:
            raise ValueError("candidates must not be empty")
        codes = [candidate.unit_code for candidate in self.candidates]
        if len(set(codes)) != len(codes):
            raise ValueError("candidate unit codes must be unique")

    @property
    def candidate_codes(self) -> tuple[str, ...]:
        return tuple(candidate.unit_code for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class SelectedUnitChoice:
    """One chosen unit code with the selector's stated reason."""

    unit_code: str
    reason: str


@dataclass(frozen=True, slots=True)
class ScopeSelectionResult:
    """Validated selection restricted to codes from the request pool."""

    choices: tuple[SelectedUnitChoice, ...]
    unmatched_duties: tuple[str, ...] = ()

    @property
    def selected_codes(self) -> tuple[str, ...]:
        return tuple(choice.unit_code for choice in self.choices)


@runtime_checkable
class ScopeSelectorPort(Protocol):
    """Choose competency units from a closed, MCP-sourced candidate pool."""

    def select_scope(self, request: ScopeSelectionRequest) -> ScopeSelectionResult: ...


def scope_selection_schema(candidate_codes: Sequence[str]) -> dict[str, Any]:
    """Build the provider output schema that pins codes to the candidate pool.

    Constraining ``unit_code`` to an enum means a schema-compliant response
    cannot name a unit the NCS MCP did not return.  ``validate_scope_selection``
    still re-checks, because schema enforcement is the provider's promise rather
    than something this process observed.
    """

    codes = list(dict.fromkeys(candidate_codes))
    if not codes:
        raise ValueError("candidate_codes must not be empty")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["selections", "unmatched_duties"],
        "properties": {
            "selections": {
                "type": "array",
                "minItems": 1,
                "maxItems": min(MAX_SELECTABLE_UNITS, len(codes)),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["unit_code", "reason"],
                    "properties": {
                        "unit_code": {"type": "string", "enum": codes},
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_SELECTION_REASON_LENGTH,
                        },
                    },
                },
            },
            "unmatched_duties": {
                "type": "array",
                "maxItems": 30,
                "items": {"type": "string", "minLength": 1, "maxLength": 300},
            },
        },
    }


def scope_selection_prompt(request: ScopeSelectionRequest) -> str:
    """Render the Korean selection instruction and its closed candidate list."""

    import json

    payload = {
        "직무명": request.job_title,
        "공고_직무수행내역": list(request.duties),
        "후보_능력단위": [candidate.as_prompt_dict() for candidate in request.candidates],
    }
    return (
        "당신은 NCS 직무기술서 작성을 위한 능력단위 선정기입니다. 아래 '후보_능력단위'는 NCS 원천에서 "
        "이미 검색된 목록입니다. 각 '공고_직무수행내역' 항목을 실제로 수행하는 데 필요한 능력단위를 "
        "후보 목록에서만 고르세요.\n"
        "규칙:\n"
        "1. unit_code는 반드시 후보 목록에 있는 값만 사용하세요. 새 코드를 만들거나 추측하지 마세요.\n"
        "2. 능력단위명, 정의, 분류를 바꾸거나 새로 쓰지 마세요. 고르기만 하세요.\n"
        "3. 표현이 달라도 같은 설비·업무면 연결하세요. 예: 차량 입출차 ↔ 주차관제설비, "
        "CCTV ↔ 영상정보처리기기, 구내 전화 ↔ 구내통신설비, 회의실 음향·영상 ↔ 음향설비·전관방송설비.\n"
        "4. 공고 업무와 직접 관련 없는 능력단위는 고르지 마세요. 관련 단위가 적으면 적게 고르세요.\n"
        "5. reason에는 어떤 공고 업무 때문에 골랐는지 한 문장으로 쓰세요.\n"
        "6. 후보 목록으로 담을 수 없는 공고 업무는 unmatched_duties에 원문 그대로 넣으세요.\n"
        "7. 자격, 학력, 경력연수, 법적 의무를 판단하거나 추가하지 마세요.\n"
        "설명이나 마크다운 없이 제공된 JSON 스키마만 반환하세요.\nINPUT="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def validate_scope_selection(
    payload: Mapping[str, Any],
    request: ScopeSelectionRequest,
) -> ScopeSelectionResult:
    """Accept a provider payload only if every code came from the pool.

    Rejecting the whole response on any unknown or duplicate code keeps a
    partially hallucinated answer from silently contributing real units.
    """

    if set(payload) != {"selections", "unmatched_duties"}:
        raise ScopeSelectionError("invalid_scope_selection_envelope")
    raw_selections = payload["selections"]
    raw_unmatched = payload["unmatched_duties"]
    if not isinstance(raw_selections, list) or not raw_selections:
        raise ScopeSelectionError("invalid_scope_selection_envelope")
    if not isinstance(raw_unmatched, list):
        raise ScopeSelectionError("invalid_scope_selection_envelope")
    if len(raw_selections) > MAX_SELECTABLE_UNITS:
        raise ScopeSelectionError("scope_selection_too_large", retryable=False)

    allowed = set(request.candidate_codes)
    choices: list[SelectedUnitChoice] = []
    seen: set[str] = set()
    for item in raw_selections:
        if not isinstance(item, Mapping) or set(item) != {"unit_code", "reason"}:
            raise ScopeSelectionError("invalid_scope_selection_item")
        unit_code, reason = item["unit_code"], item["reason"]
        if not isinstance(unit_code, str) or unit_code not in allowed:
            raise ScopeSelectionError("scope_selection_unknown_unit_code", retryable=False)
        if unit_code in seen:
            raise ScopeSelectionError("scope_selection_duplicate_unit_code", retryable=False)
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_SELECTION_REASON_LENGTH
        ):
            raise ScopeSelectionError("invalid_scope_selection_reason")
        seen.add(unit_code)
        choices.append(SelectedUnitChoice(unit_code, " ".join(reason.split())))

    unmatched: list[str] = []
    for item in raw_unmatched:
        if not isinstance(item, str):
            raise ScopeSelectionError("invalid_scope_selection_envelope")
        normalized = " ".join(item.split())
        if normalized and normalized not in unmatched:
            unmatched.append(normalized)

    return ScopeSelectionResult(tuple(choices), tuple(unmatched))


__all__ = [
    "MAX_SELECTABLE_UNITS",
    "ScopeSelectionCandidate",
    "ScopeSelectionError",
    "ScopeSelectionRequest",
    "ScopeSelectionResult",
    "ScopeSelectorPort",
    "SelectedUnitChoice",
    "scope_selection_prompt",
    "scope_selection_schema",
    "validate_scope_selection",
]
