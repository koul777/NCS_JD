"""Application contract for deterministic JobProfile document rendering.

``JobProfile`` is the sole source of facts.  Rendered HWPX bytes and SVG
previews are disposable representations and never become another domain
record.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from ncs_jd.domain.job_profile import JobProfile, ReviewFlag


HWPX_MEDIA_TYPE = "application/vnd.hancom.hwpx"
# The exact labels ``job_profile_to_template_values`` emits.  The agent-loop
# path needs them as its target field list, so they are named here rather than
# duplicated at each call site.
SUPPORTED_TEMPLATE_LABELS = (
    "채용분야",
    "대분류",
    "중분류",
    "소분류",
    "세분류",
    "능력단위",
    "직무수행내용",
    "필요지식",
    "필요기술",
    "직무수행태도",
    "필요자격",
    "직업기초능력",
    "비고/근거",
)

DRAFT_DISCLAIMER = (
    "본 문서는 검토용 초안(draft)이며 공식 승인 문서, 채용 결정, "
    "법적 적격성 판단 또는 공식 자격 인정이 아닙니다."
)


class DocumentRenderError(RuntimeError):
    """Base class for safe, structured renderer failures."""

    default_code = "document_render_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class DocumentRenderTooLargeError(DocumentRenderError):
    default_code = "document_render_too_large"


class DocumentRenderTimeoutError(DocumentRenderError):
    default_code = "document_render_timeout"


class RendererNodeUnavailableError(DocumentRenderError):
    default_code = "node_unavailable"


class KordocRendererError(DocumentRenderError):
    default_code = "kordoc_error"


class InvalidRendererResponseError(DocumentRenderError):
    default_code = "invalid_renderer_response"


class InvalidHwpxError(DocumentRenderError):
    default_code = "invalid_hwpx"


class InvalidTemplateError(DocumentRenderError):
    default_code = "invalid_template"


@dataclass(frozen=True, slots=True)
class HwpxTemplate:
    """Optional presentation template; it is never a source of job facts."""

    source_name: str
    content: bytes
    field_values: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self) -> None:
        if not self.source_name.strip():
            raise ValueError("template source_name must not be empty")
        if not isinstance(self.content, bytes):
            raise TypeError("template content must be bytes")
        if self.field_values is not None:
            labels = [label for label, _ in self.field_values]
            if any(not label.strip() for label in labels) or len(labels) != len(set(labels)):
                raise ValueError("template field labels must be unique and non-empty")
            if any(not isinstance(value, str) for _, value in self.field_values):
                raise TypeError("template field values must be strings")


@dataclass(frozen=True, slots=True)
class HwpxValidationSummary:
    valid: bool
    entry_count: int
    issue_count: int

    def __post_init__(self) -> None:
        if self.entry_count < 0 or self.issue_count < 0:
            raise ValueError("validation counts must not be negative")


@dataclass(frozen=True, slots=True)
class TemplateCapability:
    """Honest report of whether an uploaded template was preserved."""

    requested: bool = False
    supported: bool = False
    used: bool = False
    mode: Literal["standard_generate", "template_rebuild", "hwpx-preserve"] = "standard_generate"
    fallback_reason: str | None = None
    matched_fields: tuple[str, ...] = ()
    unmatched_fields: tuple[str, ...] = ()
    ambiguous_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.used and (
            not self.requested
            or not self.supported
            or self.mode not in {"template_rebuild", "hwpx-preserve"}
        ):
            raise ValueError("used template must be a supported requested template mode")
        if not self.requested and (self.used or self.matched_fields):
            raise ValueError("unrequested template cannot be used or matched")


@dataclass(frozen=True, slots=True)
class RenderedHwpx:
    filename: str
    content: bytes
    validation: HwpxValidationSummary
    template_capability: TemplateCapability = field(default_factory=TemplateCapability)
    media_type: str = HWPX_MEDIA_TYPE

    def __post_init__(self) -> None:
        if not self.filename.strip() or not self.filename.casefold().endswith(".hwpx"):
            raise ValueError("rendered filename must end with .hwpx")
        if not self.content:
            raise ValueError("rendered HWPX must not be empty")
        if not self.validation.valid:
            raise ValueError("rendered HWPX validation must be successful")


@dataclass(frozen=True, slots=True)
class HwpxRenderPreview:
    svg: str
    page_count: int
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.svg.lstrip().startswith("<svg"):
            raise ValueError("preview must contain an SVG document")
        if self.page_count < 1:
            raise ValueError("preview page_count must be positive")


@runtime_checkable
class DocumentRendererPort(Protocol):
    """Technology-neutral HWPX output boundary."""

    def render(
        self,
        profile: JobProfile,
        *,
        filename: str | None = None,
        template: HwpxTemplate | None = None,
    ) -> RenderedHwpx: ...

    def render_fields(
        self,
        field_values: tuple[tuple[str, str], ...],
        *,
        job_title: str,
        filename: str | None = None,
        template: HwpxTemplate | None = None,
    ) -> RenderedHwpx: ...

    def render_preview(self, content: bytes) -> HwpxRenderPreview: ...


def job_profile_to_markdown(profile: JobProfile) -> str:
    """Render a validated profile to stable, bounded, table-oriented Markdown.

    The function deliberately uses only presentation fields already present in
    the validated model.  Reference previews and raw criteria are not copied
    into the document body.
    """

    if not isinstance(profile, JobProfile):
        raise TypeError("profile must be a validated JobProfile")

    title = profile.job_description.job_title.text
    jd = profile.job_description
    spec = profile.person_specification

    lines = [
        f"# NCS 기반 채용 직무 설명자료 : {_md(title)}",
        "",
        f"> {DRAFT_DISCLAIMER}",
        "",
        "## 기본 정보",
        "",
        "| 항목 | 내용 |",
        "| --- | --- |",
        f"| 채용분야 | {_md(title)} |",
        f"| 문서상태 | {_md(profile.document_status)} |",
        "",
        "### NCS 분류체계",
        "",
        "| 대분류 | 중분류 | 소분류 | 세분류 | 전체 경로 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for path in profile.scope_selection.classification_paths:
        labels = _classification_labels(path.label)
        cells = (
            _coded_label(path.major_code, labels[0]),
            _coded_label(path.middle_code, labels[1]),
            _coded_label(path.small_code, labels[2]),
            _coded_label(path.sub_code, labels[3]),
            path.label,
        )
        lines.append("| " + " | ".join(_md(cell) for cell in cells) + " |")

    lines.extend(
        [
            "",
            "## 직무기술서",
            "",
            "| 항목 | 내용 |",
            "| --- | --- |",
            f"| 직무명 | {_md(title)} |",
            f"| 직무목적 | {_md(jd.job_purpose.text)} |",
            f"| 능력단위 | {_items(unit.unit_name + _unit_suffix(unit.unit_code, unit.unit_level) for unit in profile.scope_selection.included_units)} |",
            f"| 주요책무 | {_items(f'{item.title}: {item.summary}' for item in jd.duties)} |",
            f"| 직무수행내용 | {_items(f'{item.title}: {item.description}' for item in jd.tasks)} |",
            f"| 핵심책임 | {_items(item.text for item in jd.responsibilities)} |",
            f"| 의사결정 권한 | {_items(item.text for item in jd.decision_authority)} |",
            f"| KPI·기대성과 | {_items(item.text for item in jd.kpis)} |",
            f"| 협업대상 | {_items(item.text for item in jd.collaboration)} |",
            f"| 보고관계 | {_items(item.text for item in jd.reporting_relationships)} |",
            f"| 비고/근거 | {_evidence_note(_job_description_refs(profile), _flags_for(profile, ('scope_selection', 'job_description')))} |",
            "",
            "## 직무명세서",
            "",
            "| 항목 | 내용 |",
            "| --- | --- |",
            f"| 목표수준 | {_target_level(profile)} |",
            f"| 필요지식 | {_items(item.text for item in spec.knowledge)} |",
            f"| 필요기술 | {_items(item.text for item in spec.skills)} |",
            f"| 직무수행태도 | {_items(item.text for item in spec.attitudes)} |",
            f"| 경력요건 | {_items(item.text for item in spec.experience_requirements)} |",
            f"| 학력요건 | {_items(item.text for item in spec.education_requirements)} |",
            f"| 공고 자격조건 | {_items(item.text for item in spec.qualification_requirements)} |",
            f"| 공고 우대사항 | {_items(item.text for item in spec.preference_requirements)} |",
            f"| 자격 참고 | {_advisory_items(item.text for item in spec.qualification_references)} |",
            f"| 직업기초능력 | {_advisory_items(item.text for item in spec.job_base_references)} |",
            f"| 비고/근거 | {_evidence_note(_person_specification_refs(profile), _flags_for(profile, ('person_specification',)))} |",
            "",
            "---",
            "검토 후 조직의 승인 절차를 별도로 거쳐야 합니다.",
        ]
    )
    return "\n".join(lines) + "\n"


def field_values_to_markdown(
    field_values: Iterable[tuple[str, str]],
    *,
    job_title: str,
) -> str:
    """Render an already-composed label/value document to bounded Markdown.

    The agent-loop path produces a finished field set rather than a
    ``JobProfile``.  Its prose is written directly against NCS records the agent
    read itself, so there is no structured model to round-trip through: parsing
    that prose back into a profile just to render it would invent structure the
    agent never asserted.  Emitting the pairs keeps what it wrote intact.

    The document is therefore a draft with the same disclaimer as the
    deterministic path, but its per-item ``source_refs`` invariant does not
    apply -- the agent's own ``비고/근거`` field carries the evidence instead.
    """

    title = str(job_title).strip()
    if not title:
        raise ValueError("job_title must not be empty")
    rows = [
        (str(label).strip(), str(value).strip())
        for label, value in field_values
    ]
    rows = [row for row in rows if row[0]]
    if not rows:
        raise ValueError("field_values must not be empty")
    labels = [label for label, _ in rows]
    if len(labels) != len(set(labels)):
        raise ValueError("field labels must be unique")

    lines = [
        f"# NCS 기반 채용 직무 설명자료 : {_md(title)}",
        "",
        f"> {DRAFT_DISCLAIMER}",
        "",
        "| 항목 | 내용 |",
        "| --- | --- |",
    ]
    lines.extend(
        f"| {_md(label)} | {_md(value) if value else '조직 입력 필요'} |" for label, value in rows
    )
    lines.extend(["", "---", "검토 후 조직의 승인 절차를 별도로 거쳐야 합니다."])
    return "\n".join(lines) + "\n"


def job_profile_to_template_values(profile: JobProfile) -> dict[str, str]:
    """Return the exact-label value set accepted by the safe template path."""

    if not isinstance(profile, JobProfile):
        raise TypeError("profile must be a validated JobProfile")
    paths = profile.scope_selection.classification_paths
    split_labels = [_classification_labels(path.label) for path in paths]
    jd = profile.job_description
    spec = profile.person_specification
    all_flags = tuple(profile.review_flags)
    all_refs = _ordered_unique(
        ref
        for groups in (_job_description_refs(profile), _person_specification_refs(profile))
        for ref in groups
    )
    return {
        "채용분야": jd.job_title.text,
        "대분류": "\n".join(_coded_label(path.major_code, labels[0]) for path, labels in zip(paths, split_labels, strict=True)),
        "중분류": "\n".join(_coded_label(path.middle_code, labels[1]) for path, labels in zip(paths, split_labels, strict=True)),
        "소분류": "\n".join(_coded_label(path.small_code, labels[2]) for path, labels in zip(paths, split_labels, strict=True)),
        "세분류": "\n".join(_coded_label(path.sub_code, labels[3]) for path, labels in zip(paths, split_labels, strict=True)),
        "능력단위": _plain_items(
            unit.unit_name + _unit_suffix(unit.unit_code, unit.unit_level)
            for unit in profile.scope_selection.included_units
        ),
        "직무수행내용": _plain_items(f"{item.title}: {item.description}" for item in jd.tasks),
        "필요지식": _plain_items(item.text for item in spec.knowledge),
        "필요기술": _plain_items(item.text for item in spec.skills),
        "직무수행태도": _plain_items(item.text for item in spec.attitudes),
        "필요자격": _plain_items(item.text for item in spec.qualification_requirements),
        "직업기초능력": _plain_advisory(item.text for item in spec.job_base_references),
        "비고/근거": "\n".join(
            part
            for part in (
                _announcement_requirements_note(profile),
                _plain_evidence_note(all_refs, all_flags),
                DRAFT_DISCLAIMER,
            )
            if part
        ),
    }


def _classification_labels(label: str) -> tuple[str, str, str, str]:
    parts = [part.strip() for part in label.split(">") if part.strip()]
    parts.extend([""] * (4 - len(parts)))
    return tuple(parts[:4])  # type: ignore[return-value]


def _coded_label(code: str, label: str) -> str:
    return f"{code}. {label}" if label else code


def _unit_suffix(code: str, level: str | None) -> str:
    level_text = f", 수준 {level}" if level else ""
    return f" ({code}{level_text})"


def _md(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _items(values: Iterable[object]) -> str:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if not normalized:
        return "조직 입력 필요"
    return "<br>".join(f"• {_md(value)}" for value in normalized)


def _advisory_items(values: Iterable[object]) -> str:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    if not normalized:
        return "조직 입력 필요"
    return "<br>".join(f"• {_md(value)} (참고 전용, 필수조건 아님)" for value in normalized)


def _plain_items(values: Iterable[object]) -> str:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    return "\n".join(f"• {value}" for value in normalized) if normalized else "조직 입력 필요"


def _plain_advisory(values: Iterable[object]) -> str:
    normalized = [str(value).strip() for value in values if str(value).strip()]
    return "\n".join(f"• {value} (참고 전용, 필수조건 아님)" for value in normalized) if normalized else "조직 입력 필요"


def _ordered_unique(values: Iterable[object]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def _job_description_refs(profile: JobProfile) -> tuple[str, ...]:
    jd = profile.job_description
    groups = [jd.job_purpose.source_refs]
    groups.extend(item.source_refs for item in jd.duties)
    groups.extend(item.source_refs for item in jd.tasks)
    groups.extend(item.source_refs for field_name in ("responsibilities", "decision_authority", "kpis", "collaboration", "reporting_relationships") for item in getattr(jd, field_name))
    return _ordered_unique(ref for group in groups for ref in group)


def _person_specification_refs(profile: JobProfile) -> tuple[str, ...]:
    spec = profile.person_specification
    groups = []
    for field_name in (
        "knowledge",
        "skills",
        "attitudes",
        "experience_requirements",
        "education_requirements",
        "qualification_requirements",
        "preference_requirements",
        "qualification_references",
        "job_base_references",
    ):
        groups.extend(item.source_refs for item in getattr(spec, field_name))
    return _ordered_unique(ref for group in groups for ref in group)


def _flags_for(profile: JobProfile, prefixes: tuple[str, ...]) -> tuple[ReviewFlag, ...]:
    return tuple(flag for flag in profile.review_flags if flag.section.startswith(prefixes))


def _evidence_note(refs: tuple[str, ...], flags: tuple[ReviewFlag, ...]) -> str:
    return _md(_plain_evidence_note(refs, flags)).replace("\n", "<br>")


def _plain_evidence_note(refs: tuple[str, ...], flags: tuple[ReviewFlag, ...]) -> str:
    parts: list[str] = []
    if refs:
        parts.append("출처 ID: " + ", ".join(refs))
    else:
        parts.append("출처 ID: 없음")
    if flags:
        parts.extend(f"검토 플래그 {flag.code.value}: {flag.message}" for flag in flags)
    else:
        parts.append("검토 플래그: 없음")
    return "\n".join(parts)


def _target_level(profile: JobProfile) -> str:
    level = profile.person_specification.target_level
    org = level.organization_value or "조직 입력 필요"
    ncs = ", ".join(level.ncs_level_references) or "참고 없음"
    return _md(f"조직 목표: {org}; NCS 수준 참고: {ncs}; 자동 동일시하지 않음")


def _announcement_requirements_note(profile: JobProfile) -> str:
    """Keep announcement-owned conditions visible in legacy 12-field templates."""

    spec = profile.person_specification
    sections = []
    qualifications = _plain_items(item.text for item in spec.qualification_requirements)
    preferences = _plain_items(item.text for item in spec.preference_requirements)
    if qualifications != "조직 입력 필요":
        sections.append(f"공고 자격조건(조직 입력):\n{qualifications}")
    if preferences != "조직 입력 필요":
        sections.append(f"공고 우대사항(조직 입력):\n{preferences}")
    return "\n".join(sections)


__all__ = [
    "DocumentRenderError",
    "DocumentRendererPort",
    "DocumentRenderTimeoutError",
    "DocumentRenderTooLargeError",
    "DRAFT_DISCLAIMER",
    "HWPX_MEDIA_TYPE",
    "HwpxRenderPreview",
    "HwpxTemplate",
    "HwpxValidationSummary",
    "InvalidHwpxError",
    "InvalidRendererResponseError",
    "InvalidTemplateError",
    "KordocRendererError",
    "RenderedHwpx",
    "RendererNodeUnavailableError",
    "SUPPORTED_TEMPLATE_LABELS",
    "TemplateCapability",
    "field_values_to_markdown",
    "job_profile_to_markdown",
    "job_profile_to_template_values",
]
