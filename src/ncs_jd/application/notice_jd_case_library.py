"""Utilities for loading and matching notice-to-job-description case libraries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


_WORD_RE = re.compile(r"[A-Za-z0-9\uac00-\ud7a3]+", re.UNICODE)
_LABEL_CLEANER = re.compile(r"[\s_\-./()]+")
_RECORD_MIN_TOKEN_OVERLAP = 1

@dataclass(frozen=True, slots=True)
class MatchScoringConfig:
    overlap_weight: float = 0.54
    title_weight: float = 0.22
    duty_weight: float = 0.16
    coverage_weight: float = 0.08
    min_overlap_tokens: int = 1

    def __post_init__(self) -> None:
        if any(
            value < 0.0
            for value in (
                self.overlap_weight,
                self.title_weight,
                self.duty_weight,
                self.coverage_weight,
            )
        ):
            raise ValueError("weights must be non-negative")
        if self.min_overlap_tokens < 1:
            raise ValueError("min_overlap_tokens must be >= 1")


_LABEL_ALIASES = {
    "role_title": {
        "role_title",
        "roletitle",
        "role",
        "jobtitle",
        "job_title",
        "position",
        "positiontitle",
    },
    "recruitment_reason": {
        "recruitment_reason",
        "recruitmentreason",
        "reason",
        "recruitreason",
    },
    "duties": {
        "duties",
        "duty",
        "responsibility",
        "tasks",
        "task",
        "main_duty",
        "work",
    },
    "qualifications": {
        "qualifications",
        "qualification",
        "required_qualification",
        "education",
        "experience",
        "license",
        "career",
    },
    "preferences": {
        "preferences",
        "preference",
        "preferred",
        "wish",
        "bonus",
    },
    "ncs_subcategory": {
        "ncs",
        "ncs_subcategory",
        "ncssubcategory",
        "sub_category",
    },
}


def _norm_label(label: str) -> str:
    """Normalize section labels for alias matching."""

    return _LABEL_CLEANER.sub("", (label or "").lower().strip())


def _tokenize(text: str) -> tuple[str, ...]:
    """Normalize and split notice/case texts into deterministic tokens."""

    raw = (text or "").lower()
    tokens = _WORD_RE.findall(raw)
    normalized = []
    for token in tokens:
        if len(token) >= 2:
            normalized.append(token)
        elif token.isdigit():
            normalized.append(token)
    return tuple(sorted(set(normalized)))


def _relation_boost(relation_type: str, confidence: float) -> float:
    """Map attachment relation to deterministic score multipliers."""

    relation_type_boost = {
        "one_to_one": 1.0,
        "many_attachments_one_jd": 0.93,
        "one_to_many": 0.86,
        "job_description_unmatched": 0.52,
        "notice_only": 0.18,
        "no_match": 0.02,
    }.get(relation_type, 0.7)
    safe_confidence = max(0.0, min(1.0, confidence))
    # Base quality from confidence and relation type.
    return (0.4 + 0.6 * safe_confidence) * relation_type_boost


@dataclass(frozen=True, slots=True)
class NoticeJDCase:
    """Single historical case from a notice and linked job-description file."""

    case_id: str
    notice_idx: str
    notice_title: str
    notice_url: str
    relation_type: str
    relation_confidence: float
    source_file: str
    job_title: str
    duties: tuple[str, ...]
    qualifications: tuple[str, ...]
    preferences: tuple[str, ...]
    fields: tuple[tuple[str, str], ...]
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CaseMatch:
    """Scored candidate used for traceable case retrieval."""

    case: NoticeJDCase
    score: float
    overlap_ratio: float
    title_overlap_ratio: float
    duty_overlap_ratio: float
    coverage_ratio: float
    relation_confidence: float
    relation_type: str
    overlap_tokens: tuple[str, ...]
    reasons: tuple[str, ...]


def _as_text_list(payload: Any) -> tuple[str, ...]:
    if payload is None:
        return ()
    if isinstance(payload, str):
        text = " ".join(payload.split())
        return (text,) if text else ()
    if not isinstance(payload, Sequence):
        return ()
    values: list[str] = []
    for item in payload:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if text:
            values.append(text)
    return tuple(values)


def _normalize_case(record: dict[str, Any], *, index: int) -> NoticeJDCase | None:
    notice_idx = str(record.get("notice_idx") or "").strip()
    if not notice_idx:
        return None

    source_file = str(record.get("source_file") or "").strip()
    if not source_file:
        return None

    job_title = str(record.get("job_title") or "").strip()
    if not job_title:
        return None

    fields_raw = record.get("fields")
    fields: list[tuple[str, str]] = []
    if isinstance(fields_raw, list):
        for item in fields_raw:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if not label or not value:
                continue
            fields.append((label, value))

    duties = tuple(_as_text_list(record.get("duties")))
    qualifications = tuple(_as_text_list(record.get("qualifications")))
    preferences = tuple(_as_text_list(record.get("preferences")))

    tokens_text = " ".join(
        (
            str(record.get("notice_title") or ""),
            str(record.get("job_title") or ""),
            *duties,
            *qualifications,
            *preferences,
        )
    )
    tokens = _tokenize(tokens_text)

    return NoticeJDCase(
        case_id=str(record.get("case_id") or f"{notice_idx}-{index}"),
        notice_idx=notice_idx,
        notice_title=str(record.get("notice_title") or ""),
        notice_url=str(record.get("notice_url") or ""),
        relation_type=str(record.get("relation_type") or ""),
        relation_confidence=float(record.get("relation_confidence") or 0.0),
        source_file=source_file,
        job_title=job_title,
        duties=duties,
        qualifications=qualifications,
        preferences=preferences,
        fields=tuple(fields),
        tokens=tokens,
    )


def load_case_library(
    path: str | Path,
    *,
    max_records: int | None = None,
) -> tuple[NoticeJDCase, ...]:
    """Load JSON/JSONL case payloads produced by the build script."""

    resolved = Path(path)
    if not resolved.is_file():
        return ()

    text = resolved.read_text(encoding="utf-8")
    records: list[dict[str, Any]] = []

    if resolved.suffix.lower() == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
            elif isinstance(item, list):
                records.extend(entry for entry in item if isinstance(entry, dict))
    else:
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = payload.get("cases", [])
        if isinstance(payload, list):
            records = [record for record in payload if isinstance(record, dict)]

    parsed: list[NoticeJDCase] = []
    for index, record in enumerate(records):
        if max_records is not None and max_records > 0 and len(parsed) >= max_records:
            break
        if not isinstance(record, dict):
            continue
        case = _normalize_case(record, index=index)
        if case is not None:
            parsed.append(case)
    return tuple(parsed)


def _case_match(
    query_tokens: set[str],
    case: NoticeJDCase,
    *,
    config: MatchScoringConfig,
) -> CaseMatch | None:
    if not query_tokens or not case.tokens:
        return None

    overlap = query_tokens.intersection(case.tokens)
    if len(overlap) < config.min_overlap_tokens:
        return None

    overlap_ratio = len(overlap) / len(query_tokens)
    title_tokens = set(_tokenize(case.job_title))
    if case.notice_title:
        title_tokens.update(_tokenize(case.notice_title))
    title_overlap = query_tokens.intersection(title_tokens)
    title_overlap_ratio = len(title_overlap) / len(query_tokens)

    duty_tokens: set[str] = set()
    for item in case.duties:
        duty_tokens.update(_tokenize(item))
    duty_overlap = query_tokens.intersection(duty_tokens)
    duty_overlap_ratio = len(duty_overlap) / len(query_tokens)

    query_coverage_ratio = len(overlap) / len(case.tokens)

    relation_boost = _relation_boost(case.relation_type, case.relation_confidence)
    if overlap_ratio < 0.08 and relation_boost < 0.25:
        return None

    base_score = (
        overlap_ratio * config.overlap_weight
        + title_overlap_ratio * config.title_weight
        + duty_overlap_ratio * config.duty_weight
        + query_coverage_ratio * config.coverage_weight
    )
    score = base_score * relation_boost
    score = round(score, 6)
    if score <= 0:
        return None

    reasons: list[str] = [
        f"overlap={len(overlap)}/{len(query_tokens)}",
        f"query_coverage={query_coverage_ratio:.3f}",
        f"title_overlap={len(title_overlap)}",
        f"relation={case.relation_type or 'unknown'}",
        f"relation_conf={case.relation_confidence:.2f}",
        f"score={score:.4f}",
    ]

    return CaseMatch(
        case=case,
        score=score,
        overlap_ratio=round(overlap_ratio, 6),
        title_overlap_ratio=round(title_overlap_ratio, 6),
        duty_overlap_ratio=round(duty_overlap_ratio, 6),
        coverage_ratio=round(query_coverage_ratio, 6),
        relation_confidence=case.relation_confidence,
        relation_type=case.relation_type,
        overlap_tokens=tuple(sorted(overlap)),
        reasons=tuple(reasons),
    )


def match_cases_with_scores(
    query_title: str,
    query_duties: Sequence[str],
    cases: Sequence[NoticeJDCase],
    *,
    top_k: int = 5,
    min_confidence: float = 0.4,
    max_cases_per_notice: int = 1,
    scoring: MatchScoringConfig | None = None,
) -> tuple[CaseMatch, ...]:
    """Return ranked cases with explicit scoring metadata for traceability."""

    if not cases or top_k <= 0:
        return ()

    scoring = scoring or MatchScoringConfig()
    query_text = " ".join(
        part
        for part in (query_title, *query_duties)
        if isinstance(part, str) and part.strip()
    )
    query_tokens = set(_tokenize(query_text))
    if not query_tokens:
        return ()

    min_confidence = max(0.0, min(1.0, min_confidence))
    scored: list[CaseMatch] = []

    for case in cases:
        if case.relation_confidence < min_confidence:
            continue
        match = _case_match(query_tokens, case, config=scoring)
        if match is not None:
            scored.append(match)

    if not scored:
        return ()

    scored.sort(
        key=lambda item: (
            item.score,
            item.relation_confidence,
            item.overlap_ratio,
            item.title_overlap_ratio,
            len(item.overlap_tokens),
            item.case.notice_idx,
            item.case.case_id,
        ),
        reverse=True,
    )

    selected: list[CaseMatch] = []
    per_notice: dict[str, int] = {}
    for item in scored:
        if max_cases_per_notice > 0:
            count = per_notice.get(item.case.notice_idx, 0)
            if count >= max_cases_per_notice:
                continue
            per_notice[item.case.notice_idx] = count + 1

        selected.append(item)
        if len(selected) >= top_k:
            break

    return tuple(selected)


def match_cases(
    query_title: str,
    query_duties: Sequence[str],
    cases: Sequence[NoticeJDCase],
    *,
    top_k: int = 5,
    min_confidence: float = 0.4,
    scoring: MatchScoringConfig | None = None,
) -> tuple[NoticeJDCase, ...]:
    """Pick highest scoring cases for compatibility with the existing caller API."""

    return tuple(
        match.case
        for match in match_cases_with_scores(
            query_title,
            query_duties,
            cases,
            top_k=top_k,
            min_confidence=min_confidence,
            max_cases_per_notice=1,
            scoring=scoring,
        )
    )


def _resolve_alias_label(label: str) -> set[str]:
    normalized = _norm_label(label)
    expanded = {normalized}

    for canonical, aliases in _LABEL_ALIASES.items():
        canonical_aliases = {_norm_label(canonical), *( _norm_label(alias) for alias in aliases)}
        if normalized in canonical_aliases:
            expanded.update(canonical_aliases)
            return expanded

    return expanded


def _field_label_matches(field_label: str, target_aliases: set[str]) -> bool:
    normalized = _norm_label(field_label)
    if normalized in target_aliases:
        return True

    # Fallback for non-normalized inputs: one-way contain checks preserve
    # determinism while allowing "role_title" and "duties" style variations.
    return any(alias and alias in normalized for alias in target_aliases)


def examples_for_labels(
    cases: Sequence[NoticeJDCase],
    labels: Sequence[str],
    *,
    max_per_label: int = 1,
) -> tuple[tuple[str, str], ...]:
    """Extract sample values for target labels from matched cases."""

    if not cases or max_per_label <= 0:
        return ()

    selected: list[tuple[str, str]] = []
    filled: set[str] = set()
    for label in labels:
        normalized = _norm_label(label)
        if normalized in filled:
            continue

        aliases = _resolve_alias_label(label)
        values_by_label: list[str] = []
        for case in cases:
            for field_label, field_value in case.fields:
                if not _field_label_matches(field_label, aliases):
                    continue
                cleaned = " ".join(field_value.split())
                if not cleaned or cleaned in values_by_label:
                    continue
                values_by_label.append(cleaned)
                if len(values_by_label) >= max_per_label:
                    break
            if len(values_by_label) >= max_per_label:
                break

        if values_by_label:
            for value in values_by_label:
                selected.append((label, value))
            filled.add(normalized)

    return tuple(selected)


__all__ = [
    "CaseMatch",
    "MatchScoringConfig",
    "NoticeJDCase",
    "load_case_library",
    "match_cases",
    "match_cases_with_scores",
    "examples_for_labels",
]
