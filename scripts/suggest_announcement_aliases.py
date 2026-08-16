"""Derive alias candidates from unresolved labels in an extraction learning report."""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "role_title": ("직무명", "직무", "근무분야", "채용직무", "담당직무", "직무명칭", "직책"),
    "recruitment_reason": ("채용개요", "채용배경", "모집사유", "채용사유", "모집배경", "채용목적"),
    "duty": ("담당업무", "수행업무", "직무내용", "업무내용", "업무", "주요업무", "과업", "책무"),
    "qualification": ("자격", "자격요건", "학력", "경력", "보유", "면허", "자격증", "자격조건"),
    "preference": ("우대", "가점", "우대조건", "우대사항", "우대요건"),
    "ncs_subcategory": ("능력단위", "세분류", "직무분류", "ncs"),
}

REVERSE_FIELD_LABEL = {
    "role_title": "채용직무/직무명",
    "recruitment_reason": "채용개요/모집사유",
    "duty": "담당업무/직무수행",
    "qualification": "자격요건",
    "preference": "우대사항",
    "ncs_subcategory": "NCS 표기",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _guess_field(label: str, value: str | None = None) -> tuple[str | None, float, tuple[str, ...]]:
    normalized_label = _normalize(label)
    normalized_value = _normalize(value or "")
    best: tuple[str | None, float, tuple[str, ...]] = (None, 0.0, ())

    for field, keywords in FIELD_KEYWORDS.items():
        hits = tuple(
            keyword
            for keyword in keywords
            if keyword in normalized_label or keyword in normalized_value
        )
        if not hits:
            continue
        confidence = min(0.95, 0.36 + 0.14 * len(hits))
        if confidence <= best[1]:
            continue
        if any(word in normalized_label and word in normalized_value for word in hits):
            confidence = min(0.99, confidence + 0.05)
        best = (field, confidence, hits)

    # Tie-breakers: if both recruitment_reason and duty both light-match, keep duty only
    # when duty appears.
    if best[0] == "recruitment_reason" and "업무" in normalized_label:
        return ("duty", best[1], best[2])
    return best


def _build_candidates(report: dict, min_count: int) -> tuple[list[dict], list[tuple[str, int]]]:
    cases = report.get("cases", [])
    count_by_label: Counter[str] = Counter()
    sample_by_label: defaultdict[str, list[str]] = defaultdict(list)

    for case in cases:
        lines = case.get("unresolved_lines") or []
        if lines:
            for raw in lines:
                label = (raw.get("label") or "").strip()
                value = (raw.get("value") or "").strip()
                if not label:
                    continue
                count_by_label[label] += 1
                if value and len(sample_by_label[label]) < 3:
                    sample_by_label[label].append(value)
            continue
        for label in case.get("unresolved_labels", ()):
            if label:
                count_by_label[label] += 1

    candidates = []
    for label, count in count_by_label.most_common():
        if count < min_count:
            break
        field, confidence, hits = _guess_field(label, sample_by_label[label][0] if sample_by_label[label] else None)
        if field is None:
            continue
        candidates.append(
            {
                "label": label,
                "count": count,
                "predicted_field": field,
                "field_alias": REVERSE_FIELD_LABEL[field],
                "confidence": confidence,
                "matching_keywords": list(hits),
                "samples": sample_by_label[label][:3],
            }
        )

    return candidates, count_by_label.most_common(40)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Summarize unresolved label candidates from announcement learning reports.",
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=str(
            Path(__file__).resolve().parents[1]
            / "build"
            / "announcement-learning-report.json"
        ),
        help="report produced by scripts/batch_extract_cases.py",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=2,
        help="minimum unresolved occurrences required to suggest an alias",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "build" / "announcement-alias-suggestions.json"),
        help="output path for suggestion JSON",
    )
    parser.add_argument(
        "--markdown",
        default="",
        help="optional markdown output path for human review",
    )
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    report_path = Path(args.report)
    if not report_path.is_file():
        print(f"report not found: {report_path}")
        return 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    candidates, top_labels = _build_candidates(report, min_count=max(1, args.min_count))

    result = {
        "candidates": candidates,
        "top_unresolved_labels": [
            {"label": label, "count": count} for label, count in top_labels
        ],
        "count": len(candidates),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"suggestions={len(candidates)} output={output}")

    if args.markdown:
        md_path = Path(args.markdown)
        lines = ["# 미인식 라벨 학습 후보"]
        lines.append("")
        lines.append("아래 라벨은 다음 필드에 alias로 반영할 가능성이 높습니다.")
        lines.append("")
        for item in candidates:
            lines.append(f"- `{item['label']}` ({item['count']}건)")
            lines.append(f"  - 예측 필드: {item['field_alias']}")
            lines.append(f"  - 확신도: {item['confidence']:.2f}")
            if item["samples"]:
                lines.append(f"  - 예시값: {', '.join(item['samples'])}")
            lines.append("")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"markdown={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
