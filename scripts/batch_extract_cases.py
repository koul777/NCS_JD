"""Run announcement extraction over many documents and report robustness gaps."""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ncs_jd.adapters.kordoc_parser import KordocDocumentParser
from ncs_jd.application.document_parser import (
    DocumentParserError,
    KordocParserError,
)
from ncs_jd.application.announcement_extraction import (
    AnnouncementReviewFlag,
    extract_announcement,
)
from ncs_jd.application.document_parser import ParsedDocument
from ncs_jd.application import announcement_extraction as ae

DOC_EXTENSIONS = {".pdf", ".hwp", ".hwpx", ".docx", ".txt"}


def _iter_files(targets: Iterable[str], recursive: bool, pattern: str | None) -> list[Path]:
    files: list[Path] = []
    for raw in targets:
        path = Path(raw).expanduser()
        if path.is_file():
            if path.suffix.lower() in DOC_EXTENSIONS:
                files.append(path)
            continue
        if path.is_dir():
            if recursive:
                globber = path.rglob(pattern or "*")
            else:
                globber = path.glob(pattern or "*")
            for file_path in globber:
                if file_path.is_file() and file_path.suffix.lower() in DOC_EXTENSIONS:
                    files.append(file_path)
    return sorted(files)


def _extract_unknown_labeled_line_with_value(line: str) -> tuple[str, str] | None:
    for match in ae._LABEL_SEPARATOR.finditer(line):
        label = line[: match.start()].strip()
        value = line[match.end() :].strip()
        if not label or not value:
            continue
        if ae._label_kind(label) is not None:
            continue
        if len(label) < 2 or len(value) > 800:
            continue
        if not re.search(r"[가-힣A-Za-z]", label):
            continue
        normalized = ae._normalized_label(label)
        if (
            not normalized
            or normalized in {"채용공고", "원문", "첨부파일", "첨부", "근무분야"}
        ):
            continue
        return label, value
    return None


def _collect_unresolved_labels(document: ParsedDocument) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for block in document.blocks:
        if block.block_type == "table":
            continue
        source = block.markdown or block.text
        for raw_line in source.splitlines():
            line = ae._clean_line(raw_line)
            if not line or (line.startswith("|") and line.endswith("|")):
                continue
            detected = _extract_unknown_labeled_line_with_value(line)
            if detected is not None:
                items.append(detected)
    return tuple(items)


def _review_flag_codes(flags: tuple[AnnouncementReviewFlag, ...]) -> tuple[str, ...]:
    return tuple(sorted(flag.code for flag in flags))


def _analyze_file(parser: KordocDocumentParser, path: Path) -> dict:
    parsed = parser.parse(path.name, path.read_bytes())
    extraction = extract_announcement(parsed)
    unresolved = _collect_unresolved_labels(parsed)
    unresolved_all = tuple(label for label, _ in unresolved)
    flags = _review_flag_codes(extraction.review_flags)
    unresolved_distinct = tuple(sorted(set(unresolved_all)))
    unresolved_lines = tuple(
        {"label": label, "value": value} for label, value in unresolved[:12]
    )
    return {
        "path": str(path),
        "source_name": extraction.source_name,
        "parse_blocks": len(parsed.blocks),
        "parse_page_count": parsed.metadata.page_count,
        "parse_quality": parsed.quality.status,
        "warning_count": parsed.quality.warning_count,
        "role_candidates": len(extraction.role_candidates),
        "duties_total": sum(len(candidate.duties) for candidate in extraction.role_candidates),
        "qualifications_total": sum(len(candidate.qualifications) for candidate in extraction.role_candidates),
        "preferences_total": sum(len(candidate.preferences) for candidate in extraction.role_candidates),
        "recruitment_reasons_total": sum(
            len(candidate.recruitment_reasons) for candidate in extraction.role_candidates
        ),
        "ncs_sections_total": sum(
            len(candidate.ncs_subcategory_candidates) for candidate in extraction.role_candidates
        ),
        "review_flags": flags,
        "unresolved_labels_all": unresolved_all,
        "unresolved_labels": unresolved_distinct,
        "unresolved_lines": unresolved_lines,
        "unresolved_count": len(unresolved),
        "unresolved_distinct_count": len(unresolved_distinct),
    }


def _to_json(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(report_text, encoding="utf-8")


def _print_summary(report: dict) -> None:
    total = report["summary"]["total_files"]
    success = report["summary"]["success"]
    failed = report["summary"]["failed"]
    unresolved = report["summary"]["files_with_unresolved_labels"]
    total_unresolved = report["summary"]["total_unresolved_labels"]
    duties_missing = report["summary"]["duties_missing"]
    print(
        f"files={total} success={success} failed={failed} "
        f"unresolved_files={unresolved} total_unresolved={total_unresolved} duties_missing={duties_missing}"
    )
    print("top_unresolved_labels:")
    for label, count in report["summary"]["unresolved_top"][:12]:
        print(f"  {label}: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run announcement extraction on many job-announcement files.",
    )
    parser.add_argument("paths", nargs="+", help="files or directories to process")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="search input directories recursively",
    )
    parser.add_argument(
        "--pattern",
        default="*",
        help="file pattern when scanning directories (default: *)",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "build" / "announcement-learning-report.json"),
        help="json report path",
    )
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    targets = _iter_files(args.paths, recursive=args.recursive, pattern=args.pattern)
    if not targets:
        print("no candidate documents found")
        return 1
    parser_client = KordocDocumentParser()
    cases: list[dict] = []
    fail_files: list[dict] = []
    unresolved_counter: Counter[str] = Counter()

    for path in targets:
        try:
            case = _analyze_file(parser_client, path)
        except (DocumentParserError, KordocParserError) as exc:
            fail_files.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        cases.append(case)
        unresolved_counter.update(case["unresolved_labels_all"])

    total = len(targets)
    success = len(cases)
    failed = len(fail_files)
    flagged_unresolved = sum(1 for case in cases if case["unresolved_count"] > 0)
    flagged = [path for path in cases if "duties_missing" in path["review_flags"]]

    report = {
        "summary": {
            "total_files": total,
            "success": success,
            "failed": failed,
            "files_with_unresolved_labels": flagged_unresolved,
            "duties_missing": len(flagged),
            "total_unresolved_labels": sum(case["unresolved_count"] for case in cases),
            "unresolved_top": unresolved_counter.most_common(30),
            "top_codes": Counter(
                code
                for case in cases
                for code in case["review_flags"]
            ).most_common(20),
        },
        "cases": cases,
        "failed": fail_files,
    }

    output = Path(args.output)
    _to_json(report, output)
    _print_summary(report)
    print(f"report_saved={output}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
