"""Build a notice-to-job-description case library from relation analysis outputs."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
import re
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ncs_jd.adapters.kordoc_parser import KordocDocumentParser
from ncs_jd.application.document_parser import (
    DocumentParserError,
    KordocParserError,
)
from ncs_jd.application.announcement_extraction import extract_announcement


SUPPORTED_FILE_EXTENSIONS = {
    ".pdf",
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".txt",
    ".xlsx",
    ".xls",
    ".csv",
    ".pptx",
}

FIELD_LABELS = {
    "role_title": "role_title",
    "recruitment_reason": "recruitment_reason",
    "duties": "duties",
    "qualifications": "qualifications",
    "preferences": "preferences",
    "ncs_subcategory": "ncs_subcategory",
}

ARCHIVE_FILE_EXTENSIONS = {".zip"}
MAX_FILE_BYTES = 48 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES = 24 * 1024 * 1024
MAX_CANDIDATE_FILES_PER_NOTICE = 6
MAX_ZIP_CANDIDATES = 6
MAX_FIELD_EXAMPLES = 4
_BULLET_RE = re.compile(r"^\s*(?:[0-9]+\.\s*|[-*]\s*|[a-zA-Z]\s*[.)]\s*)")
_LABEL_RE = re.compile(r"\s*[:：]\s*", re.UNICODE)
_MULTISPACE_RE = re.compile(r"\s+")
_HEADING_MARKER_RE = re.compile(
    r"^\s*(?:#|[-=]{3,}|\\*\\*|직무개요|직무내용|직무상세|직무요건|담당업무|필수요건|우대사항|자격요건|자격사항|지원자격|자격조건|자격사항|주요업무|근무지역|근무조건|지원자|공고|채용|고용|직무명|직무|요구사항|조건|업무|자격|우대|채용공고|job\\s*description|responsibilities|requirements|qualifications|preferred|duty|duties)$"
)  
_IMAGE_MARKER_RE = re.compile(
    r"^\s*(?:!\[.*\]\(.*\)|<img\b|이미지|image|표|그림|표지 파일|첨부파일|attachment|pdf 파일)",
    re.IGNORECASE | re.UNICODE,
)
_GENERIC_TEXT_HINTS = (
    "직무소개서",
    "직무기술서",
    "채용공고",
    "필수",
    "우대",
    "자격",
    "학력",
    "경력",
    "근무",
    "급여",
    "병역",
    "직무",
    "job",
    "description",
    "position",
    "duty",
    "responsibility",
    "requirement",
    "qualification",
    "prefer",
    "contact",
    "supporter",
)

_SECTION_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("duties", "duty", "tasks", "task", "work", "role", "position", "main duties", "main_duty", "job tasks", "직무", "주요업무", "업무", "담당업무"), "duties"),
    (("qualification", "qualifications", "education", "career", "experience", "license", "certificate", "major", "required", "자격", "요건", "자격요건", "우대"), "qualifications"),
    (("preference", "prefer", "preferred", "우대", "우대사항"), "preferences"),
    (("reason", "recruit", "reason", "채용사유", "모집사유", "근거"), "recruitment_reason"),
    (("position", "job title", "job", "role title", "직무명", "직책"), "role_title"),
)


def _clean_text(text: Any, *, limit: int = 1000) -> str:
    if not isinstance(text, str):
        return ""
    value = " ".join(text.split())
    return value[:limit]


def _safe_read_text(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _clean_text(raw)


def _normalized_for_match(value: str) -> str:
    lowered = (value or "").strip().lower()
    lowered = _MULTISPACE_RE.sub(" ", lowered)
    lowered = lowered.replace("-", "").replace("_", "").replace("/", "")
    return lowered


def _guess_section(label: str) -> str | None:
    normalized = _normalized_for_match(label)
    if not normalized:
        return None
    for keywords, section in _SECTION_HINTS:
        if any(keyword in normalized for keyword in keywords):
            return section
    return None


def _clean_fallback_line(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    text = text.replace("\u200b", "")
    text = text.replace("\r", " ").replace("\t", " ")
    text = _MULTISPACE_RE.sub(" ", text).strip()
    if not text:
        return ""
    text = _BULLET_RE.sub("", text)
    return text.strip()



def _normalize_ascii(value: str) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\\W_]+", "", _clean_text(value, limit=2000).lower())


def _is_generic_text(value: str) -> bool:
    cleaned = _clean_text(value, limit=500).lower()
    if not cleaned:
        return True
    if _HEADING_MARKER_RE.fullmatch(cleaned):
        return True
    if _IMAGE_MARKER_RE.fullmatch(cleaned) or _IMAGE_MARKER_RE.search(cleaned):
        return True
    if len(_normalize_ascii(cleaned)) <= 3:
        return True
    if any(hint in cleaned for hint in _GENERIC_TEXT_HINTS):
        return True
    if cleaned.startswith("#") and len(cleaned.replace("#", "").strip()) <= 4:
        return True
    if all(ch in "*-_=#." for ch in cleaned.replace(" ", "")):
        return True
    if cleaned.startswith("http"):
        return True
    return False


def _field_has_signal(values: tuple[str, ...] | list[str]) -> bool:
    for value in values:
        if not _is_generic_text(value):
            return True
    return False


def _case_has_enough_signal(
    *,
    fields: tuple[tuple[str, str], ...],
    duties: tuple[str, ...],
    qualifications: tuple[str, ...],
    preferences: tuple[str, ...],
    relation_type: str,
    relation_confidence: float,
) -> bool:
    if not fields:
        return False

    has_workload = _field_has_signal(duties) or _field_has_signal(qualifications) or _field_has_signal(preferences)
    role_title = ""
    for label, value in fields:
        if label == FIELD_LABELS["role_title"] and value:
            role_title = value
            break
    if not role_title or _is_generic_text(role_title):
        return False

    if not has_workload:
        if relation_type == "one_to_one" and relation_confidence >= 0.85:
            return True
        if len(fields) >= 3 and relation_confidence >= 0.6:
            return True
        return False

    if relation_type in {"notice_only", "job_description_unmatched"} and relation_confidence < 0.5:
        return False

    return True

def _dedupe_keep_order(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = (item or "").strip()
        if not text:
            continue
        normalized = _normalized_for_match(text)
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(text)
    return tuple(out)


def _iter_zip_member_candidates(path: Path) -> list[tuple[str, bytes]]:
    if path.suffix.lower() not in ARCHIVE_FILE_EXTENSIONS:
        return []
    if not path.is_file():
        return []
    try:
        with zipfile.ZipFile(path) as archive:
            items: list[tuple[float, str, bytes]] = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size <= 0 or info.file_size > MAX_ZIP_MEMBER_BYTES:
                    continue
                inner_ext = Path(info.filename).suffix.lower()
                if inner_ext not in SUPPORTED_FILE_EXTENSIONS:
                    continue
                try:
                    raw = archive.read(info.filename)
                except (zipfile.BadZipFile, ValueError, OSError, KeyError):
                    continue
                if not raw:
                    continue
                name_key = _normalized_for_match(Path(info.filename).name)
                score = 0.0
                if any(
                    token in name_key
                    for token in ("job", "직무", "description", "채용", "jd", "직무기술서")
                ):
                    score += 0.4
                if any(token in name_key for token in ("spec", "명세", "JD", "직무", "사양")):
                    score += 0.3
                if info.file_size < 1024 * 1024:
                    score += 0.05
                items.append((score, info.filename, raw))
    except (zipfile.BadZipFile, OSError, ValueError):
        return []
    items.sort(key=lambda item: (-item[0], _normalized_for_match(item[1])))
    return [(name, raw) for _, name, raw in items[:MAX_ZIP_CANDIDATES]]


def _extract_fallback_fields_from_document(parsed: Any, *, fallback_title: str | None = None) -> tuple[tuple[str, str], ...] | None:
    blocks = []
    if getattr(parsed, "markdown", ""):
        blocks.append(parsed.markdown)
    for block in getattr(parsed, "blocks", ()):
        text = getattr(block, "markdown", "") or getattr(block, "text", "")
        if text:
            blocks.append(text)

    lines: list[str] = []
    for block in blocks:
        for raw_line in str(block).splitlines():
            line = _clean_fallback_line(raw_line)
            if not line:
                continue
            if len(line) <= 1:
                continue
            lines.append(line)

    if not lines:
        return None

    section: str | None = None
    duties: list[str] = []
    qualifications: list[str] = []
    preferences: list[str] = []
    recruitment_reasons: list[str] = []
    role_title = ""
    title_candidates = _dedupe_keep_order([fallback_title] + lines) if fallback_title else _dedupe_keep_order(lines)
    for line in lines:
        split_match = _LABEL_RE.split(line, maxsplit=1)
        if len(split_match) == 2:
            left, right = split_match
            if left and right:
                section = _guess_section(left)
                if section in {"role_title", "recruitment_reason", "duties", "qualifications", "preferences"}:
                    if section == "role_title":
                        role_title = _clean_text(right, limit=120)
                    elif section == "recruitment_reason":
                        recruitment_reasons.append(right)
                    elif section == "duties":
                        duties.append(right)
                    elif section == "qualifications":
                        qualifications.append(right)
                    elif section == "preferences":
                        preferences.append(right)
                    continue

        if not role_title and section is None and len(line) <= 45:
            role_title = _clean_text(line, limit=120)
            continue

        if section == "recruitment_reason":
            recruitment_reasons.append(line)
            continue
        if section == "duties":
            duties.append(line)
            continue
        if section == "qualifications":
            qualifications.append(line)
            continue
        if section == "preferences":
            preferences.append(line)
            continue

        if line.isdigit():
            continue
        if section is not None:
            # Keep collecting until an explicit new section appears.
            if section == "recruitment_reason":
                recruitment_reasons.append(line)
            elif section == "duties":
                duties.append(line)
            elif section == "qualifications":
                qualifications.append(line)
            elif section == "preferences":
                preferences.append(line)
            continue

        guessed = _guess_section(line)
        if guessed in {"duties", "qualifications", "preferences", "recruitment_reason"}:
            section = guessed
            continue

    if not role_title and title_candidates:
        role_title = _clean_text(title_candidates[0], limit=120)

    duties = list(_dedupe_keep_order(duties))[:MAX_FIELD_EXAMPLES + 4]
    qualifications = list(_dedupe_keep_order(qualifications))[:MAX_FIELD_EXAMPLES + 4]
    preferences = list(_dedupe_keep_order(preferences))[:MAX_FIELD_EXAMPLES + 4]
    recruitment_reasons = list(_dedupe_keep_order(recruitment_reasons))[:MAX_FIELD_EXAMPLES]

    if not role_title and not duties and not qualifications and not preferences:
        return None

    result: list[tuple[str, str]] = []
    if role_title:
        result.append((FIELD_LABELS["role_title"], _clean_text(role_title, limit=120)))
    for item in duties[:MAX_FIELD_EXAMPLES]:
        result.append((FIELD_LABELS["duties"], item))
    for item in qualifications[:MAX_FIELD_EXAMPLES]:
        result.append((FIELD_LABELS["qualifications"], item))
    for item in preferences[:MAX_FIELD_EXAMPLES]:
        result.append((FIELD_LABELS["preferences"], item))
    for item in recruitment_reasons[:MAX_FIELD_EXAMPLES]:
        result.append((FIELD_LABELS["recruitment_reason"], item))

    return tuple(result)


def _pick_best_role(extraction: Any) -> Any | None:
    if not extraction.role_candidates:
        return None
    for candidate in extraction.role_candidates:
        if getattr(candidate, "role_title", None) is not None:
            return candidate
    return extraction.role_candidates[0]


def _record_to_fields(role_candidate: Any) -> tuple[tuple[str, str], ...]:
    if role_candidate is None:
        return ()

    output: list[tuple[str, str]] = []
    per_label_counter: Counter[str] = Counter()

    def add(label: str, value: str) -> None:
        normalized = (label or "").strip()
        if not normalized or per_label_counter[normalized] >= MAX_FIELD_EXAMPLES:
            return
        cleaned = _clean_text(value)
        if not cleaned:
            return
        output.append((normalized, cleaned))
        per_label_counter[normalized] += 1

    if role_candidate.role_title is not None:
        add(FIELD_LABELS["role_title"], role_candidate.role_title.text)
    for reason in getattr(role_candidate, "recruitment_reasons", ()):
        add(FIELD_LABELS["recruitment_reason"], reason.text)
    for duty in getattr(role_candidate, "duties", ()):
        add(FIELD_LABELS["duties"], duty.text)
    for item in getattr(role_candidate, "qualifications", ()):
        add(FIELD_LABELS["qualifications"], item.text)
    for item in getattr(role_candidate, "preferences", ()):
        add(FIELD_LABELS["preferences"], item.text)
    for item in getattr(role_candidate, "ncs_subcategory_candidates", ()):
        add(FIELD_LABELS["ncs_subcategory"], item.text)

    return tuple(output)


def _to_case_entry(
    *,
    idx: str,
    notice: dict[str, Any],
    file_path: str,
    file_sequence: int,
    fields: tuple[tuple[str, str], ...],
    duties: tuple[str, ...],
    qualifications: tuple[str, ...],
    preferences: tuple[str, ...],
    job_title: str,
) -> dict[str, Any]:
    return {
        "case_id": f"{idx}-{file_sequence}",
        "notice_idx": idx,
        "notice_title": notice.get("title") or "",
        "notice_url": notice.get("source_url") or "",
        "relation_type": notice.get("relation_type") or "",
        "relation_confidence": notice.get("relation_confidence") or 0.0,
        "source_file": file_path,
        "job_title": job_title,
        "duties": duties,
        "qualifications": qualifications,
        "preferences": preferences,
        "fields": [{"label": label, "value": value} for label, value in fields],
    }


def _parse_job_description(
    parser: KordocDocumentParser,
    source_name: str,
    raw: bytes,
) -> tuple[Any | None, Any | None]:
    try:
        parsed = parser.parse(source_name, raw)
    except (DocumentParserError, KordocParserError, ValueError):
        return None, None
    try:
        return extract_announcement(parsed), parsed
    except Exception:
        return None, parsed


def _extract_notice_file_candidates(notice: dict[str, Any]) -> list[tuple[float, str]]:
    paths: list[tuple[float, str]] = []
    for relation in notice.get("relations", ()):
        if relation.get("attachment_category") != "job_description":
            continue
        matched_files = relation.get("matched_files") or []
        if matched_files:
            for matched in matched_files:
                path = str(matched.get("path") or "").strip()
                if not path:
                    continue
                score = float(matched.get("score") or 0.0)
                paths.append((score, path))
            continue
        for path in relation.get("linked_files") or []:
            paths.append((0.0, str(path)))

    # deterministic ordering for ties keeps reproducible results
    seen: set[str] = set()
    ordered: list[tuple[float, str]] = []
    for score, path in sorted(paths, key=lambda item: (-item[0], item[1])):
        if path in seen:
            continue
        seen.add(path)
        ordered.append((score, path))
    if ordered:
        return ordered[:MAX_CANDIDATE_FILES_PER_NOTICE]

    # Fallback path: if no explicit job-description attachment mapping exists, use
    # notice/other attachments first, then all downloaded files.
    for relation in notice.get("relations", ()):
        if relation.get("attachment_category") not in {"notice", "other"}:
            continue
        matched_files = relation.get("matched_files") or []
        if matched_files:
            for matched in matched_files:
                path = str(matched.get("path") or "").strip()
                if not path:
                    continue
                score = float(matched.get("score") or 0.0)
                paths.append((score, path))
            continue
        for path in relation.get("linked_files") or []:
            paths.append((0.0, str(path)))

    if not paths:
        for downloaded in notice.get("downloaded_files", ()) or []:
            path = str(downloaded.get("path") or "").strip()
            if path:
                paths.append((0.0, path))

    seen = set()
    ordered = []
    for score, path in sorted(paths, key=lambda item: (-item[0], item[1])):
        if path in seen:
            continue
        seen.add(path)
        ordered.append((score, path))
    return ordered[:MAX_CANDIDATE_FILES_PER_NOTICE]


def _iter_candidate_documents(path: Path) -> list[tuple[str, bytes, str]]:
    if path.suffix.lower() == ".zip":
        return [
            (path.name, raw, f"{path}::{inner}")
            for inner, raw in _iter_zip_member_candidates(path)
        ]

    if path.stat().st_size > MAX_FILE_BYTES:
        return []

    return [(path.name, path.read_bytes(), str(path))]


def _build_case_payload(
    idx: str,
    notice: dict[str, Any],
    path_identifier: str,
    file_sequence: int,
    *,
    extraction: Any | None,
    parsed_document: Any | None,
    fallback_title: str,
) -> dict[str, Any] | None:
    fields: tuple[tuple[str, str], ...] = ()
    duties: tuple[str, ...] = ()
    qualifications: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    role_title = fallback_title or idx
    used_fallback = False

    if extraction is not None:
        role = _pick_best_role(extraction)
        if role is not None:
            fields = _record_to_fields(role)
            duties = tuple(item.text for item in getattr(role, "duties", ()))
            qualifications = tuple(item.text for item in getattr(role, "qualifications", ()))
            preferences = tuple(item.text for item in getattr(role, "preferences", ()))
            if getattr(role, "role_title", None) is not None:
                role_title = role.role_title.text

    if (not fields or not duties or not qualifications or not preferences) and parsed_document is not None:
        fallback_fields = _extract_fallback_fields_from_document(
            parsed_document,
            fallback_title=fallback_title or role_title,
        )
        if fallback_fields:
            used_fallback = True
            existing_by_label: dict[str, list[str]] = {}
            for field_name, value in fields:
                existing_by_label.setdefault(field_name, []).append(value)
            fallback_map: dict[str, list[str]] = {}
            for field_name, value in fallback_fields:
                fallback_map.setdefault(field_name, []).append(value)

            merged: list[tuple[str, str]] = list(fields)
            merged.append(("__fallback__", "true"))

            for section, candidates in fallback_map.items():
                for value in candidates:
                    if len(existing_by_label.get(section, ())) < MAX_FIELD_EXAMPLES:
                        merged.append((section, value))
            fields = tuple(
                field
                for field in merged
                if field[0] != "__fallback__"
            )

            if used_fallback:
                if not duties:
                    duties = tuple(fallback_map.get(FIELD_LABELS["duties"], ()))
                if not qualifications:
                    qualifications = tuple(fallback_map.get(FIELD_LABELS["qualifications"], ()))
                if not preferences:
                    preferences = tuple(fallback_map.get(FIELD_LABELS["preferences"], ()))
                if role_title == idx:
                    fallback_title_from_fields = [
                        value for field_name, value in fallback_fields if field_name == FIELD_LABELS["role_title"]
                    ]
                    if fallback_title_from_fields:
                        role_title = fallback_title_from_fields[0]

    if not fields:
        return None

    if not _case_has_enough_signal(
        fields=fields,
        duties=duties,
        qualifications=qualifications,
        preferences=preferences,
        relation_type=notice.get("relation_type", ""),
        relation_confidence=float(notice.get("relation_confidence", 0.0) or 0.0),
    ):
        return None

    if not role_title and (duties or qualifications or preferences):
        role_title = idx

    return _to_case_entry(
        idx=idx,
        notice=notice,
        file_path=path_identifier,
        file_sequence=file_sequence,
        fields=fields,
        duties=tuple(_clean_text(text, limit=1000) for text in _dedupe_keep_order(duties)[:12]),
        qualifications=tuple(_clean_text(text, limit=1000) for text in _dedupe_keep_order(qualifications)[:12]),
        preferences=tuple(_clean_text(text, limit=1000) for text in _dedupe_keep_order(preferences)[:12]),
        job_title=_clean_text(role_title) or idx,
    )


def _build_cases(
    relation_data: dict[str, Any],
    *,
    max_cases: int,
    max_cases_per_notice: int,
) -> list[dict[str, Any]]:
    notices = relation_data.get("notices", ())
    if not isinstance(notices, list):
        return []

    parser = KordocDocumentParser()
    case_entries: list[dict[str, Any]] = []
    parse_ok = 0
    parse_fail = 0
    skipped_non_supported = 0

    for notice in notices:
        if not isinstance(notice, dict):
            continue
        idx = str(notice.get("idx") or "").strip()
        if not idx:
            continue

        candidates = _extract_notice_file_candidates(notice)
        if not candidates:
            continue

        used_in_notice = 0
        for candidate_index, (score, raw_path) in enumerate(candidates):
            if max_cases_per_notice and used_in_notice >= max_cases_per_notice:
                break
            if max_cases and len(case_entries) >= max_cases:
                return case_entries
            path = Path(raw_path)
            if not path.is_file():
                parse_fail += 1
                continue
            if path.suffix.lower() not in SUPPORTED_FILE_EXTENSIONS and path.suffix.lower() not in ARCHIVE_FILE_EXTENSIONS:
                parse_fail += 1
                skipped_non_supported += 1
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                parse_fail += 1
                continue

            payloads = _iter_candidate_documents(path)
            for source_index, (source_name, raw, source_path) in enumerate(payloads):
                if max_cases_per_notice and used_in_notice >= max_cases_per_notice:
                    break
                if max_cases and len(case_entries) >= max_cases:
                    return case_entries

                # For plain text, extract lightweight fallback fields from the raw file.
                if path.suffix.lower() == ".txt":
                    text = _clean_text(raw.decode("utf-8", errors="replace"), limit=1200)
                    if text:
                        case_entries.append(
                            _to_case_entry(
                                idx=idx,
                                notice=notice,
                                file_path=source_path,
                                file_sequence=candidate_index * 100 + source_index,
                                fields=((FIELD_LABELS["recruitment_reason"], text),),
                                duties=(),
                                qualifications=(),
                                preferences=(),
                                job_title=path.stem[:120] or idx,
                            )
                        )
                        used_in_notice += 1
                        parse_ok += 1
                        continue

                extraction, parsed = _parse_job_description(parser, source_name, raw)
                if extraction is None and parsed is None:
                    parse_fail += 1
                    continue

                payload = _build_case_payload(
                    idx=idx,
                    notice=notice,
                    path_identifier=source_path,
                    file_sequence=candidate_index * 100 + source_index,
                    extraction=extraction,
                    parsed_document=parsed,
                    fallback_title=path.stem,
                )
                if payload is None:
                    parse_fail += 1
                    continue

                case_entries.append(payload)
                used_in_notice += 1
                parse_ok += 1

    return case_entries


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_results(
    cases: list[dict[str, Any]],
    *,
    output_path: Path,
    output_jsonl: Path | None,
    summary: dict[str, Any],
) -> None:
    payload = {
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "cases": cases,
        "summary": summary,
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if output_jsonl is not None:
        output_jsonl.parent.mkdir(parents=True, exist_ok=True)
        output_jsonl.write_text(
            "\n".join(json.dumps(case, ensure_ascii=False) for case in cases),
            encoding="utf-8",
        )


def _summarize(cases: list[dict[str, Any]], notices: int) -> dict[str, Any]:
    counters = Counter(case.get("relation_type") for case in cases)
    return {
        "total_cases": len(cases),
        "notices": notices,
        "relation_type_distribution": dict(sorted(counters.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a notice-to-JD case library from relation analysis output.",
    )
    parser.add_argument(
        "--relation-file",
        default=str(Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_relation.json"),
        help="relation analysis JSON produced by scripts/analyze_notice_jd_relation.py",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_case_library.json"),
        help="output JSON path",
    )
    parser.add_argument("--output-jsonl", default="")
    parser.add_argument("--max-cases", type=int, default=0, help="hard cap for total cases")
    parser.add_argument(
        "--max-cases-per-notice",
        type=int,
        default=1, help="max JD mappings to extract per notice",
    )
    args = parser.parse_args(argv)

    relation_path = Path(args.relation_file)
    if not relation_path.is_file():
        print(f"relation_file_not_found={relation_path}")
        return 1

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    relation_data = _read_json(relation_path)
    notices = relation_data.get("notices", ())
    if not isinstance(notices, list):
        print(f"invalid_relation_file={relation_path}")
        return 1

    cases = _build_cases(
        relation_data,
        max_cases=max(0, args.max_cases),
        max_cases_per_notice=max(0, args.max_cases_per_notice),
    )
    summary = _summarize(cases, notices=len(notices))
    output_path = Path(args.output)
    output_jsonl = Path(args.output_jsonl) if args.output_jsonl else None

    _write_results(cases, output_path=output_path, output_jsonl=output_jsonl, summary=summary)

    print(f"case_library={output_path}")
    print(f"cases={len(cases)}")
    print(f"notices={summary['notices']}")
    for relation_type, count in summary["relation_type_distribution"].items():
        print(f"relation_type={relation_type} count={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
