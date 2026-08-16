"""Deterministic extraction of job-announcement facts for human review."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from ncs_jd.application.document_parser import ParsedBlock, ParsedDocument, SourceLocator


ExtractionField = Literal[
    "role_title",
    "recruitment_reason",
    "duty",
    "qualification",
    "preference",
    "ncs_subcategory",
]
ExtractionMethod = Literal["table_cell", "explicit_label", "heading_section"]
ReviewSeverity = Literal["info", "warning"]

_ROLE_LABELS = (
    "채용분야",
    "모집분야",
    "직무명",
    "채용직무",
    "채용직무명",
    "근무분야",
    "직무",
    "직무명칭",
    "모집직무",
    "지원분야",
    "직종",
    "직급",
    "직종/직급",
    "채용직종",
    "모집직종",
)
_RECRUITMENT_REASON_LABELS = (
    "채용사유",
    "채용 사유",
    "채용목적",
    "채용 목적",
    "채용개요",
    "채용개요 및 배경",
)
_DUTY_LABELS = (
    "담당업무",
    "수행업무",
    "직무수행내용",
    "직무내용",
    "주요업무",
    "주요업무내용",
    "담당직무",
    "업무내용",
)
_QUALIFICATION_LABELS = (
    "지원자격",
    "응시자격",
    "자격요건",
    "자격조건",
    "자격 조건",
    "채용조건",
    "채용 조건",
    "지원요건",
    "응시요건",
    "응시자격요건",
    "학력요건",
    "경력요건",
    "학력 및 경력요건",
    "자격사항",
    "응시 자격 요건",
)
_PREFERENCE_LABELS = (
    "우대사항",
    "우대조건",
    "우대요건",
    "우대자격",
    "가점사항",
    "가점요건",
    "가점및우대사항",
    "가점 및 우대 사항",
)
_NCS_LABELS = ("NCS세분류", "NCS 세분류", "세분류명", "세분류")
_ATTACHMENT_ONLY = re.compile(r"^(?:세부(?:내용|사항)(?:은|는)?\s*)?(?:별첨|붙임|첨부)(?:파일|문서)?\s*(?:참조|참고)$")
_LABEL_SEPARATOR = re.compile(r"\s*(?:[:：]|\s[-–—]\s)\s*", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ExtractedAnnouncementItem:
    field: ExtractionField
    text: str
    source_locator: SourceLocator
    extraction_method: ExtractionMethod
    confidence: float
    review_required: Literal[True] = True

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("extracted text must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class AnnouncementReviewFlag:
    code: str
    severity: ReviewSeverity
    message: str
    role_candidate_id: str | None = None
    source_locator: SourceLocator | None = None


@dataclass(frozen=True, slots=True)
class RoleCandidate:
    candidate_id: str
    role_title: ExtractedAnnouncementItem | None
    recruitment_reasons: tuple[ExtractedAnnouncementItem, ...] = ()
    duties: tuple[ExtractedAnnouncementItem, ...] = ()
    qualifications: tuple[ExtractedAnnouncementItem, ...] = ()
    preferences: tuple[ExtractedAnnouncementItem, ...] = ()
    ncs_subcategory_candidates: tuple[ExtractedAnnouncementItem, ...] = ()


@dataclass(frozen=True, slots=True)
class AnnouncementExtraction:
    source_name: str
    role_candidates: tuple[RoleCandidate, ...]
    review_flags: tuple[AnnouncementReviewFlag, ...]


@dataclass(slots=True)
class _CandidateBuilder:
    role_title: ExtractedAnnouncementItem | None = None
    recruitment_reasons: list[ExtractedAnnouncementItem] = field(default_factory=list)
    duties: list[ExtractedAnnouncementItem] = field(default_factory=list)
    qualifications: list[ExtractedAnnouncementItem] = field(default_factory=list)
    preferences: list[ExtractedAnnouncementItem] = field(default_factory=list)
    ncs_subcategories: list[ExtractedAnnouncementItem] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.role_title
            or self.recruitment_reasons
            or self.duties
            or self.qualifications
            or self.preferences
            or self.ncs_subcategories
        )

    def add(self, item: ExtractedAnnouncementItem) -> None:
        collection = {
            "recruitment_reason": self.recruitment_reasons,
            "duty": self.duties,
            "qualification": self.qualifications,
            "preference": self.preferences,
            "ncs_subcategory": self.ncs_subcategories,
        }.get(item.field)
        if collection is None:
            if item.field == "role_title" and self.role_title is None:
                self.role_title = item
            return
        signature = (item.text.casefold(), item.source_locator.block_id)
        if all((existing.text.casefold(), existing.source_locator.block_id) != signature for existing in collection):
            collection.append(item)


def extract_announcement(document: ParsedDocument) -> AnnouncementExtraction:
    """Extract explicitly stated announcement fields without inference or approval."""

    builders: list[_CandidateBuilder] = []
    unresolved_labels: list[str] = []
    for block in document.blocks:
        if block.block_type == "table" and (block.table_rows or block.markdown.strip()):
            _extract_table(block, builders)
    _extract_text_blocks(document.blocks, builders, unresolved_labels)

    builders = [builder for builder in builders if not builder.is_empty()]
    candidates = tuple(
        RoleCandidate(
            candidate_id=f"role-{index + 1:03d}",
            role_title=builder.role_title,
            recruitment_reasons=tuple(builder.recruitment_reasons),
            duties=tuple(builder.duties),
            qualifications=tuple(builder.qualifications),
            preferences=tuple(builder.preferences),
            ncs_subcategory_candidates=tuple(builder.ncs_subcategories),
        )
        for index, builder in enumerate(builders)
    )
    flags = _review_flags(document, candidates, tuple(dict.fromkeys(unresolved_labels)))
    return AnnouncementExtraction(document.source_name, candidates, flags)


def _extract_table(block: ParsedBlock, builders: list[_CandidateBuilder]) -> None:
    source_rows = block.table_rows or _markdown_table_rows(block.markdown)
    rows = [tuple(_clean_cell(cell) for cell in row) for row in source_rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return

    # Record table: 채용분야 | 담당업무 | 지원자격, followed by one row per role.
    headers = [_label_kind(cell) for cell in rows[0]]
    recognized = sum(kind is not None for kind in headers)
    role_index = next((index for index, kind in enumerate(headers) if kind == "role_title"), None)
    if role_index is None and recognized and any(_normalized_label(cell) == "구분" for cell in rows[0]):
        role_index = next(index for index, cell in enumerate(rows[0]) if _normalized_label(cell) == "구분")
    if len(rows) > 1 and recognized >= 2 and role_index is not None:
        for row in rows[1:]:
            if role_index >= len(row) or not row[role_index]:
                continue
            builder = _builder_for_role(builders, row[role_index], block.locator, "table_cell", 0.99)
            for index, kind in enumerate(headers):
                if kind and kind != "role_title" and index < len(row):
                    _add_values(builder, kind, row[index], block.locator, "table_cell", 0.97)
        return

    # Matrix table: labels down the first column and one role per following column.
    row_kinds = [_label_kind(row[0]) if row else None for row in rows]
    role_row_index = next((index for index, kind in enumerate(row_kinds) if kind == "role_title"), None)
    if role_row_index is not None and len(rows[role_row_index]) > 2:
        role_row = rows[role_row_index]
        for column in range(1, len(role_row)):
            role_text = role_row[column]
            if not role_text:
                continue
            builder = _builder_for_role(builders, role_text, block.locator, "table_cell", 0.99)
            for row_index, kind in enumerate(row_kinds):
                if kind and kind != "role_title" and column < len(rows[row_index]):
                    _add_values(builder, kind, rows[row_index][column], block.locator, "table_cell", 0.97)
        return

    # Key/value table. Repeated role rows naturally start or select candidates.
    current: _CandidateBuilder | None = None
    for row in rows:
        kind = _label_kind(row[0]) if row else None
        if kind is None:
            continue
        value = "\n".join(cell for cell in row[1:] if cell).strip()
        if not value:
            continue
        if kind == "role_title":
            current = _builder_for_role(builders, value, block.locator, "table_cell", 0.99)
        else:
            current = current or _last_or_new(builders)
            _add_values(current, kind, value, block.locator, "table_cell", 0.97)


def _extract_text_blocks(
    blocks: tuple[ParsedBlock, ...],
    builders: list[_CandidateBuilder],
    unresolved_labels: list[str],
) -> None:
    current = builders[-1] if builders else None
    section: ExtractionField | None = None
    for block in blocks:
        if block.block_type == "table":
            section = None
            continue
        source = block.markdown or block.text
        for raw_line in source.splitlines():
            line = _clean_line(raw_line)
            if not line:
                continue
            if line.startswith("|") and line.endswith("|"):
                continue
            if re.fullmatch(r"\d+", line):
                section = None
                continue
            label, value = _split_labeled_line(line)
            if label is not None:
                section = label
                if value:
                    if label == "role_title":
                        current = _builder_for_role(builders, value, block.locator, "explicit_label", 0.97)
                    else:
                        current = current or _last_or_new(builders)
                        _add_values(current, label, value, block.locator, "explicit_label", 0.95)
                continue
            unresolved = _extract_unknown_labeled_line(line)
            if unresolved is not None:
                unresolved_labels.append(unresolved)
            heading_kind = _label_kind(line)
            if heading_kind is not None:
                section = heading_kind
                continue
            if section is not None:
                if section == "role_title":
                    current = _builder_for_role(builders, line, block.locator, "heading_section", 0.91)
                    section = None
                else:
                    current = current or _last_or_new(builders)
                    _add_values(current, section, line, block.locator, "heading_section", 0.89)


def _builder_for_role(
    builders: list[_CandidateBuilder],
    value: str,
    locator: SourceLocator,
    method: ExtractionMethod,
    confidence: float,
) -> _CandidateBuilder:
    role = " ".join(_clean_cell(value).split())
    for builder in builders:
        if builder.role_title and builder.role_title.text.casefold() == role.casefold():
            return builder
    builder = _CandidateBuilder(
        role_title=ExtractedAnnouncementItem(
            "role_title", role, locator, method, confidence
        )
    )
    builders.append(builder)
    return builder


def _last_or_new(builders: list[_CandidateBuilder]) -> _CandidateBuilder:
    if builders:
        return builders[-1]
    builder = _CandidateBuilder()
    builders.append(builder)
    return builder


def _add_values(
    builder: _CandidateBuilder,
    kind: ExtractionField,
    value: str,
    locator: SourceLocator,
    method: ExtractionMethod,
    confidence: float,
) -> None:
    for text in _split_values(value):
        builder.add(ExtractedAnnouncementItem(kind, text, locator, method, confidence))


def _split_values(value: str) -> tuple[str, ...]:
    normalized = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    raw_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    bullet = re.compile(r"^(?:[-*+]\s+|[•●○▪◦]\s*|\d+[.)]\s*)")
    pieces: list[str] = []
    if any(bullet.match(line) for line in raw_lines):
        current: list[str] = []
        for line in raw_lines:
            match = bullet.match(line)
            if match:
                if current:
                    pieces.append(" ".join(current))
                current = [line[match.end() :].strip()]
            elif current:
                current.append(line)
            else:
                current = [line]
        if current:
            pieces.append(" ".join(current))
    else:
        pieces = [bullet.sub("", line).strip() for line in raw_lines]
    pieces = [piece for piece in pieces if piece]
    return tuple(pieces) or ((_clean_cell(value),) if _clean_cell(value) else ())


def _split_labeled_line(line: str) -> tuple[ExtractionField | None, str]:
    for match in _LABEL_SEPARATOR.finditer(line):
        label_text = line[: match.start()].strip()
        kind = _label_kind(label_text)
        if kind is not None:
            return kind, line[match.end() :].strip()
    # Also accept compact labels such as "담당업무 홍보 기획".
    for label in (
        *_NCS_LABELS,
        *_RECRUITMENT_REASON_LABELS,
        *_DUTY_LABELS,
        *_QUALIFICATION_LABELS,
        *_PREFERENCE_LABELS,
        *_ROLE_LABELS,
    ):
        match = re.match(rf"^{re.escape(label)}\s+(.+)$", line, flags=re.IGNORECASE)
        if match:
            return _label_kind(label), match.group(1).strip()
    return None, ""


def _extract_unknown_labeled_line(line: str) -> str | None:
    for match in _LABEL_SEPARATOR.finditer(line):
        label = line[: match.start()].strip()
        value = line[match.end() :].strip()
        if not label or not value:
            continue
        if _label_kind(label) is not None:
            continue
        if len(label) < 2 or len(value) > 800:
            continue
        if not re.search(r"[가-힣]", label):
            continue
        normalized = _normalized_label(label)
        if (
            not normalized
            or normalized in {"공고문", "입사지원서", "직무기술서", "첨부", "기타첨부파일", "원문", "근무분야"}
        ):
            continue
        return label
    return None


def _label_kind(value: str) -> ExtractionField | None:
    normalized = _normalized_label(value)
    if normalized.startswith("및"):
        normalized = normalized[1:]
    lookup: tuple[tuple[tuple[str, ...], ExtractionField], ...] = (
        (_NCS_LABELS, "ncs_subcategory"),
        (_RECRUITMENT_REASON_LABELS, "recruitment_reason"),
        (_DUTY_LABELS, "duty"),
        (_QUALIFICATION_LABELS, "qualification"),
        (_PREFERENCE_LABELS, "preference"),
        (_ROLE_LABELS, "role_title"),
    )
    for labels, kind in lookup:
        if any(normalized == _normalized_label(label) for label in labels):
            return kind
    return None


def _normalized_label(value: str) -> str:
    return re.sub(r"[\s#>*_`()[\]{}·/:：]+", "", value).casefold()


def _markdown_table_rows(markdown: str) -> tuple[tuple[str, ...], ...]:
    """Recover Kordoc tables when only the normalized Markdown is populated."""

    rows: list[tuple[str, ...]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = tuple(
            re.sub(r"<br\s*/?>", "\n", cell.replace(r"\|", "|"), flags=re.IGNORECASE).strip()
            for cell in re.split(r"(?<!\\)\|", line[1:-1])
        )
        compact = tuple(re.sub(r"\s+", "", cell) for cell in cells)
        if compact and all(re.fullmatch(r":?-{3,}:?", cell) for cell in compact):
            continue
        if any(cells):
            rows.append(cells)
    return tuple(rows)


def _clean_cell(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.replace("\x00", "")).strip()


def _clean_line(value: str) -> str:
    line = value.strip()
    line = re.sub(r"^#{1,6}\s*", "", line)
    line = re.sub(r"^(?:[-*+]\s+|[•●○▪]\s*)", "", line)
    line = re.sub(r"\\([*_~`])", r"\1", line)
    return re.sub(r"[*_`]", "", line).strip()


def _is_attachment_only(text: str) -> bool:
    compact = re.sub(r"[.。\s]+", " ", text).strip()
    return bool(_ATTACHMENT_ONLY.fullmatch(compact))


def _review_flags(
    document: ParsedDocument,
    candidates: tuple[RoleCandidate, ...],
    unresolved_labels: tuple[str, ...],
) -> tuple[AnnouncementReviewFlag, ...]:
    flags: list[AnnouncementReviewFlag] = []
    corpus = "\n".join(filter(None, (document.markdown, *(block.text for block in document.blocks))))
    meaningful_items = [
        item
        for candidate in candidates
        for item in (
            *((candidate.role_title,) if candidate.role_title else ()),
            *candidate.recruitment_reasons,
            *candidate.duties,
            *candidate.qualifications,
            *candidate.preferences,
            *candidate.ncs_subcategory_candidates,
        )
    ]
    if not corpus.strip() and not meaningful_items:
        flags.append(
            AnnouncementReviewFlag(
                "announcement_empty",
                "warning",
                "파싱된 공고문에 검토할 텍스트가 없습니다.",
            )
        )
    attachment_items = meaningful_items and all(_is_attachment_only(item.text) for item in meaningful_items)
    if "붙임" in corpus or "별첨" in corpus or "첨부" in corpus:
        visible = [_clean_line(line) for line in corpus.splitlines() if _clean_line(line)]
        attachment_text_only = bool(visible) and all(
            _is_attachment_only(line) or _label_kind(line) is not None for line in visible
        )
        if attachment_items or attachment_text_only:
            flags.append(
                AnnouncementReviewFlag(
                    "attachment_reference_only",
                    "warning",
                    "공고문 본문이 붙임 문서만 참조합니다. 붙임 파일을 추가로 확인해야 합니다.",
                )
            )
    for candidate in candidates:
        if candidate.role_title is None:
            flags.append(
                AnnouncementReviewFlag(
                    "role_title_missing",
                    "warning",
                    "추출 항목에 연결할 채용분야 또는 직무명이 없습니다.",
                    candidate.candidate_id,
                )
            )
        if not candidate.duties or all(_is_attachment_only(item.text) for item in candidate.duties):
            flags.append(
                AnnouncementReviewFlag(
                    "duties_missing",
                    "warning",
                    "담당업무 또는 직무수행내용이 비어 있어 사람의 확인이 필요합니다.",
                    candidate.candidate_id,
                    candidate.role_title.source_locator if candidate.role_title else None,
                )
            )
    if len(candidates) > 1:
        flags.append(
            AnnouncementReviewFlag(
                "multiple_roles_review_required",
                "info",
                "공고문에서 여러 직무 후보가 추출되었습니다. 직무별 연결을 확인해야 합니다.",
            )
        )
    if not candidates and corpus.strip() and not any(flag.code == "attachment_reference_only" for flag in flags):
        flags.append(
            AnnouncementReviewFlag(
                "announcement_fields_missing",
                "warning",
                "채용분야, 업무, 자격 또는 우대사항을 결정적으로 찾지 못했습니다.",
            )
        )
    if unresolved_labels:
        sample = ", ".join(unresolved_labels[:4])
        if len(unresolved_labels) > 4:
            sample = f"{sample} 외 {len(unresolved_labels) - 4}건"
        flags.append(
            AnnouncementReviewFlag(
                "unrecognized_section_labels",
                "info",
                f"인식되지 않은 라벨 패턴입니다: {sample}. 필요 시 다음 라벨을 추가해 보강하세요.",
            )
        )
    return tuple(flags)


__all__ = [
    "AnnouncementExtraction",
    "AnnouncementReviewFlag",
    "ExtractedAnnouncementItem",
    "ExtractionField",
    "ExtractionMethod",
    "RoleCandidate",
    "extract_announcement",
]
