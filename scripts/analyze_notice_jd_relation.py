# -*- coding: utf-8 -*-
"""Build deterministic relations between job.alio notices and job-description documents."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

import html


RE_DOWNLOAD_LINK = re.compile(
    r'<a[^>]+href="([^"]*download\.json\?fileNo=\d+[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
RE_HTML = re.compile(r"<[^>]+>")
RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
RE_H1 = re.compile(
    r"<(h[1-6]|strong|b|p|div|span)[^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
RE_PATH_CLEAN = re.compile(r"[\\/:*?\"<>|]+")

NOTICE_KEYWORDS = {
    "notice",
    "recruit",
    "recruitment",
    "announcement",
    "hiring",
    "job notice",
    "\ucc44\uc6a9",
    "\uacf5\uace0",
    "\ucc44\uc6a9\uacf5\uace0",
    "\ubaa8\uc9d1",
}
JD_KEYWORDS = {
    "job description",
    "job-description",
    "job specification",
    "job spec",
    "jobtech",
    "jd",
    "position",
    "duty",
    "responsibility",
    "task",
    "role",
    "assignment",
    "\uc9c1\ubb34",
    "\uc9c1\ubb34\uae30\uc220",
    "\uc9c1\ubb34\uae30\uc220\uc11c",
    "\uc9c1\ubb34\uc124\uba85",
    "\uc9c1\ubb34\uc124\uba85\uc11c",
    "\uc9c1\ubb34\uba85\uc138",
    "\uc9c1\ubb34\uba85\uc138\uc11c",
    "\uc9c1\ubb34\uc18c\uac1c",
    "\uc9c1\ubb34\uc18c\uac1c\uc11c",
    "\uc9c1\ubb34\uae30\uc220\uc790\ub8cc",
    "\uc9c1\ubb34\uae30\uc220\uc124\uba85",
    "ncs",
}
OTHER_KEYWORDS = {
    "document",
    "form",
    "appendix",
    "guide",
    "notice attachment",
}

SUPPORTED_EXTS = {
    ".pdf",
    ".hwp",
    ".hwpx",
    ".doc",
    ".docx",
    ".txt",
    ".xls",
    ".xlsx",
    ".csv",
    ".pptx",
    ".zip",
    ".bin",
    ".json",
}


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = RE_HTML.sub(" ", text)
    text = text.replace("\u200b", "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_label(text: str) -> str:
    lowered = (text or "").strip().lower()
    return re.sub(r"\s+", "", lowered)


def _slug(text: str, max_len: int = 120) -> str:
    text = _clean_text(text).replace(" ", "_")
    text = RE_PATH_CLEAN.sub("_", text)
    text = text.strip("_")
    return text[:max_len] if len(text) > max_len else text


def _tokenize(text: str) -> list[str]:
    cleaned = _clean_text(text).lower()
    parts = re.split(r"[^\w]+", cleaned)
    return [token for token in parts if token]


def _token_overlap(a: str, b: str) -> float:
    a_tokens = set(_tokenize(a))
    b_tokens = set(_tokenize(b))
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def _normalized_variants(text: str) -> tuple[str, ...]:
    normalized = {_normalize_label(text)}
    try:
        decoded = text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        decoded = None
    if decoded:
        normalized.add(_normalize_label(decoded))
    return tuple(dict.fromkeys(filter(None, normalized)))


def _sequence_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _contains_keyword(label: str, keyword_set: Iterable[str]) -> bool:
    normalized = _normalize_label(label)
    return any(_normalize_label(keyword) in normalized for keyword in keyword_set)


def _is_job_description_candidate(label: str, url: str) -> bool:
    normalized_label = _normalize_label(label)
    normalized_url = _normalize_label(url)
    if "ncs" in normalized_label and any(
        token in normalized_label
        for token in ("job", "duty", "spec", "jobspec", "description", "\uc9c1\ubb34")
    ):
        return True
    if any(token in normalized_label for token in ("\uc9c1\ubb34", "\uc9c1\ubb34\uae30\uc220")):
        return True
    if "recruit" in normalized_label and "\uc9c1\ubb34" in normalized_label:
        return True
    if "\ucc44\uc6a9" in normalized_label and "ncs" in normalized_url:
        return True
    if "\ucc44\uc6a9" in normalized_label and "\uc9c1\ubb34" in normalized_label:
        return True
    return False


def _extract_file_no(url: str) -> str:
    parsed = urlparse(url)
    file_no = parse_qs(parsed.query).get("fileNo", [""])[0]
    return unquote(file_no)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while chunk := fp.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _list_notice_dirs(input_dir: Path) -> list[Path]:
    if not input_dir.is_dir():
        return []

    numeric_children = [
        child
        for child in sorted(input_dir.iterdir())
        if child.is_dir() and child.name.isdigit()
    ]
    if numeric_children:
        return numeric_children

    has_direct_assets = any(
        child.is_file() and child.suffix.lower() in SUPPORTED_EXTS | {".html", ".json"}
        for child in sorted(input_dir.iterdir())
    )
    if has_direct_assets:
        return [input_dir]

    nested: list[Path] = []
    for parent in sorted(input_dir.iterdir()):
        if not parent.is_dir():
            continue
        nested.extend(
            child
            for child in sorted(parent.iterdir())
            if child.is_dir() and child.name.isdigit()
        )
    return nested


def _extract_title(html_text: str) -> str | None:
    match = RE_TITLE.search(html_text)
    if match:
        title = _clean_text(match.group(1))
        if title:
            return title

    for match in RE_H1.finditer(html_text):
        title = _clean_text(match.group(2))
        if title:
            return title
    return None


def _guess_category(label: str, url: str) -> str:
    if _contains_keyword(label, JD_KEYWORDS):
        return "job_description"
    if _is_job_description_candidate(label, url):
        return "job_description"
    if _contains_keyword(label, NOTICE_KEYWORDS):
        return "notice"
    if _contains_keyword(label, OTHER_KEYWORDS):
        return "other"
    normalized_url = _normalize_label(url)
    if "job" in normalized_url or "jd" in normalized_url:
        return "job_description"
    return "other"


def _extract_attachments_from_html(html_text: str, idx: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for match in RE_DOWNLOAD_LINK.finditer(html_text):
        href, raw_label = match.groups()
        label = _clean_text(raw_label)
        if not label:
            continue
        absolute_url = href if href.startswith("http") else f"https://job.alio.go.kr{href}"
        category = _guess_category(label, absolute_url)
        results.append(
            {
                "idx": idx,
                "label": label,
                "url": absolute_url,
                "file_no": _extract_file_no(href),
                "category": category,
            }
        )
    return results


def _collect_downloaded_files(notice_dir: Path) -> list[Path]:
    files: list[Path] = []

    for file in sorted(notice_dir.iterdir()):
        if file.is_file() and file.suffix.lower() in SUPPORTED_EXTS:
            files.append(file)

    raw_dir = notice_dir / "raw"
    if raw_dir.is_dir():
        for file in sorted(raw_dir.iterdir()):
            if file.is_file() and file.suffix.lower() in SUPPORTED_EXTS:
                files.append(file)
    return files


def _find_matching_files(
    attachment: dict[str, str],
    downloaded_files: list[Path],
    *,
    min_score: float,
    max_matches: int,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not downloaded_files:
        return [], None

    label = attachment["label"]
    file_no = attachment["file_no"]
    category = attachment["category"]
    label_variants = _normalized_variants(label)

    matches: list[dict[str, Any]] = []
    for file in downloaded_files:
        filename = file.name
        filename_variants = _normalized_variants(filename)
        score = 0.0
        reasons: list[str] = []

        if file_no and any(file_no in variant for variant in filename_variants):
            score += 0.8
            reasons.append("file_no")

        if category == "job_description":
            score += 0.08

        token_score = max(
            (
                _token_overlap(label_variant, filename_variant)
                for label_variant in label_variants
                for filename_variant in filename_variants
            ),
            default=0.0,
        )
        score += token_score * 0.55
        if token_score > 0.0:
            reasons.append("token_overlap")

        if label_variants and filename_variants:
            seq = max(
                (
                    _sequence_ratio(label_variant, filename_variant)
                    for label_variant in label_variants
                    for filename_variant in filename_variants
                ),
                default=0.0,
            )
            score += seq * 0.45
            if seq >= 0.25:
                reasons.append("name_similarity")

        if score > 1.0:
            score = 1.0
        if score < min_score:
            continue

        matches.append(
            {
                "path": str(file),
                "name": filename,
                "extension": file.suffix.lower(),
                "size": file.stat().st_size if file.exists() else 0,
                "sha256_prefix": _file_hash(file) if file.exists() else None,
                "score": round(score, 4),
                "reasons": reasons,
            }
        )

    matches.sort(key=lambda item: item["score"], reverse=True)
    if not matches:
        return [], None

    # Keep ties close to the top score as secondary candidates.
    top = matches[0]
    threshold = max(top["score"] - 0.1, min_score)
    best_matches = [m for m in matches if m["score"] >= threshold][:max_matches]
    return best_matches, best_matches[0]


@dataclass(frozen=True)
class NoticeRelation:
    type: str
    confidence: float
    linked_jd_files: tuple[str, ...]
    flagged_attachments: int
    ambiguous_attachments: int


def _classify_notice(
    notice: dict[str, Any],
    *,
    min_score: float,
) -> NoticeRelation:
    attachments = notice["attachments"]
    relations = notice["relations"]
    attachment_count = len(attachments)
    jd_attachments = [item for item in attachments if item["category"] == "job_description"]
    jd_attachment_count = len(jd_attachments)

    linked_jd_files: set[str] = set()
    for rel in relations:
        if rel["attachment_category"] != "job_description":
            continue
        linked_jd_files.update(rel["linked_files"])

    ambiguous_attachments = 0
    for rel in relations:
        if rel["attachment_category"] != "job_description":
            continue
        if len(rel["matched_files"]) >= 2 and rel["match_top_score"] >= min_score:
            ambiguous_attachments += 1

    if attachment_count == 0:
        relation_type = "no_match"
        confidence = 0.0
    elif jd_attachment_count == 0:
        relation_type = "notice_only"
        confidence = 0.4
    else:
        linked_count = len(linked_jd_files)
        if linked_count == 0:
            relation_type = "job_description_unmatched"
            confidence = 0.6
        elif linked_count == 1 and jd_attachment_count == 1:
            relation_type = "one_to_one"
            confidence = 0.9
        elif linked_count == 1 and jd_attachment_count > 1:
            relation_type = "many_attachments_one_jd"
            confidence = 0.75
        else:
            relation_type = "one_to_many"
            confidence = 0.8

    if jd_attachment_count:
        ambiguity_ratio = ambiguous_attachments / jd_attachment_count
        confidence = confidence * (1.0 - ambiguity_ratio * 0.25)

    return NoticeRelation(
        type=relation_type,
        confidence=round(max(min(confidence, 1.0), 0.0), 3),
        linked_jd_files=tuple(sorted(linked_jd_files)),
        flagged_attachments=jd_attachment_count,
        ambiguous_attachments=ambiguous_attachments,
    )


def _read_html_candidates(notice_dir: Path) -> list[Path]:
    raw_dir = notice_dir / "raw"
    if raw_dir.is_dir():
        htmls = sorted(raw_dir.glob("*.html"))
        if htmls:
            return htmls
    return sorted(notice_dir.glob("*.html"))


def analyze(
    input_dir: Path,
    *,
    allow_missing_html: bool = False,
    min_score: float = 0.35,
    max_matches: int = 3,
) -> dict[str, Any]:
    notices: list[dict[str, Any]] = []
    notice_dirs = _list_notice_dirs(input_dir)

    for notice_dir in notice_dirs:
        idx = notice_dir.name
        notice_record: dict[str, Any] = {
            "idx": idx,
            "title": None,
            "source_url": f"https://job.alio.go.kr/recruitview.do?idx={idx}",
            "raw_html": None,
            "attachments": [],
            "downloaded_files": [],
            "relations": [],
            "unlinked_downloaded_files": [],
        }

        html_candidates = _read_html_candidates(notice_dir)
        if html_candidates:
            raw_html = html_candidates[0]
            notice_record["raw_html"] = str(raw_html)
            html_text = raw_html.read_text(encoding="utf-8", errors="ignore")
            notice_record["title"] = _extract_title(html_text)
            notice_record["attachments"] = _extract_attachments_from_html(html_text, idx)
        elif not allow_missing_html:
            continue

        downloaded_files = _collect_downloaded_files(notice_dir)
        notice_record["downloaded_files"] = [
            {
                "path": str(path),
                "size": path.stat().st_size if path.exists() else 0,
                "sha256_prefix": _file_hash(path) if path.exists() else None,
                "extension": path.suffix.lower(),
            }
            for path in downloaded_files
        ]

        linked_paths: set[str] = set()
        for attachment in notice_record["attachments"]:
            best_matches, best = _find_matching_files(
                attachment,
                downloaded_files,
                min_score=min_score,
                max_matches=max_matches,
            )
            linked = [item["path"] for item in best_matches]
            linked_paths.update(linked)
            notice_record["relations"].append(
                {
                    "attachment_label": attachment["label"],
                    "attachment_category": attachment["category"],
                    "attachment_url": attachment["url"],
                    "attachment_file_no": attachment["file_no"],
                    "matched_files": best_matches,
                    "matched_file_count": len(best_matches),
                    "match_top_score": best["score"] if best is not None else 0.0,
                    "match_top_path": best["path"] if best is not None else None,
                    "linked_files": linked,
                }
            )

        notice_record["unlinked_downloaded_files"] = [
            str(path)
            for path in downloaded_files
            if str(path) not in linked_paths
        ]

        relation = _classify_notice(
            notice_record,
            min_score=min_score,
        )
        notice_record["relation_type"] = relation.type
        notice_record["relation_confidence"] = relation.confidence
        notice_record["relation_linked_job_description_files"] = list(relation.linked_jd_files)
        notice_record["relation_job_description_attachments"] = relation.flagged_attachments
        notice_record["relation_ambiguous_links"] = relation.ambiguous_attachments
        notices.append(notice_record)

    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "input_dir": str(input_dir),
        "notice_count": len(notices),
        "system": "job.alio.go.kr",
        "min_score": min_score,
        "max_matches": max_matches,
        "notices": notices,
    }


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    relation_counter: Counter[str] = Counter()
    attached_count = 0
    matched_files = 0
    ambiguous_links = 0
    unlinked_files = 0
    reused_file_counter: Counter[str] = Counter()
    file_to_notices: dict[str, list[str]] = defaultdict(list)

    for notice in result["notices"]:
        relation_counter[notice["relation_type"]] += 1
        attached_count += len(notice["attachments"])
        matched_files += len(notice["relation_linked_job_description_files"])
        ambiguous_links += notice["relation_ambiguous_links"]
        unlinked_files += len(notice["unlinked_downloaded_files"])

        for relation in notice["relations"]:
            top_score = relation["match_top_score"]
            if top_score > 0:
                file_to_notices[relation["match_top_path"]].append(notice["idx"])

        for file_item in notice["downloaded_files"]:
            key = f"{file_item['size']}:{file_item['sha256_prefix']}"
            reused_file_counter[key] += 1

    ambiguous_by_notice = [
        {"idx": notice["idx"], "count": notice["relation_ambiguous_links"]}
        for notice in result["notices"]
        if notice["relation_ambiguous_links"] > 0
    ]
    reused = [
        {"fingerprint": fp, "appearances": count}
        for fp, count in reused_file_counter.items()
        if count > 1
    ]

    return {
        "notices": result["notice_count"],
        "attachments_total": attached_count,
        "relation_breakdown": dict(sorted(relation_counter.items())),
        "job_description_links": matched_files,
        "ambiguous_attachment_links": ambiguous_links,
        "unlinked_downloaded_files_total": unlinked_files,
        "notices_with_ambiguous_linking": ambiguous_by_notice,
        "reused_file_fingerprints": reused,
        "top_reused_notice_pairs": [
            {
                "file": file,
                "notices": sorted(set(notices_)),
            }
            for file, notices_ in sorted(file_to_notices.items())
            if len(set(notices_)) > 1
        ][:30],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze notice and JD attachment relations.")
    parser.add_argument(
        "--input-dir",
        default=str(Path(__file__).resolve().parents[1] / "build" / "alio_announcements"),
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_relation.json"),
    )
    parser.add_argument("--output-jsonl", default="")
    parser.add_argument("--allow-missing-html", action="store_true")
    parser.add_argument("--min-score", type=float, default=0.35)
    parser.add_argument("--max-matches", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    input_dir = Path(args.input_dir)
    output_path = Path(args.output)
    jsonl_output = Path(args.output_jsonl) if args.output_jsonl else None

    result = analyze(
        input_dir,
        allow_missing_html=args.allow_missing_html,
        min_score=args.min_score,
        max_matches=max(args.max_matches, 1),
    )
    summary = _summarize(result)
    result["summary"] = summary

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if jsonl_output is not None:
        jsonl_output.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_output.open("w", encoding="utf-8") as fp:
            for notice in result["notices"]:
                fp.write(json.dumps(notice, ensure_ascii=False) + "\n")

    print(f"result={output_path}")
    print(f"notices={result['notice_count']} attachments={summary['attachments_total']}")
    for key in sorted(summary["relation_breakdown"]):
        print(f"{key}={summary['relation_breakdown'][key]}")
    print(f"job_description_links={summary['job_description_links']}")
    print(f"ambiguous_links={summary['ambiguous_attachment_links']}")
    print(f"unlinked_files={summary['unlinked_downloaded_files_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
