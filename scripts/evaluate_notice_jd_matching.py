"""Evaluate notice-to-job-description matching with optional weight tuning."""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ncs_jd.application.notice_jd_case_library import (  # noqa: E402
    MatchScoringConfig,
    NoticeJDCase,
    load_case_library,
    match_cases_with_scores,
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣]+", re.UNICODE)


def _token_set(value: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall((value or "").lower()) if len(token) >= 2}


def _tokens_from_values(values: tuple[str, ...]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(_token_set(value))
    return tokens


def _join_query(values: tuple[str, ...]) -> str:
    return "\n".join(value.strip() for value in values if value and value.strip())


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_float(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(num):
        return num
    return None


def _load_relation_notices(relation_path: Path | None) -> tuple[dict[str, Any], ...]:
    if relation_path is None:
        return ()
    if not relation_path.is_file():
        raise FileNotFoundError(f"relation_file_not_found={relation_path}")
    payload = _read_json(relation_path)
    notices = payload.get("notices")
    if isinstance(notices, list):
        return tuple(notice for notice in notices if isinstance(notice, dict))
    if isinstance(notices, tuple):
        return tuple(notice for notice in notices if isinstance(notice, dict))
    return ()


def _build_notices_from_cases(
    cases: tuple[NoticeJDCase, ...],
) -> list[tuple[str, dict[str, Any]]]:
    by_notice: dict[str, NoticeJDCase] = {}
    for case in cases:
        if case.notice_idx not in by_notice:
            by_notice[case.notice_idx] = case
    items: list[tuple[str, dict[str, Any]]] = []
    for notice_idx, case in by_notice.items():
        items.append(
            (
                notice_idx,
                {
                    "idx": notice_idx,
                    "title": case.notice_title or case.job_title,
                    "notice_title": case.notice_title,
                    "source_url": case.notice_url,
                    "relation_type": "synthetic_case_only",
                    "relation_confidence": 0.0,
                    "relation_linked_job_description_files": (case.source_file,),
                    "relations": [],
                },
            )
        )
    return items


def _query_from_relation(notice: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    title = (notice.get("title") or notice.get("notice_title") or "").strip()
    query_bits: list[str] = []
    if title:
        query_bits.append(title)

    attachments = notice.get("attachments") if isinstance(notice.get("attachments"), list) else ()
    for rel in attachments:
        if not isinstance(rel, dict):
            continue
        label = (rel.get("label") or rel.get("attachment_label") or "").strip()
        if label and label not in query_bits:
            query_bits.append(label)

    relations = notice.get("relations") if isinstance(notice.get("relations"), list) else ()
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        label = (rel.get("attachment_label") or "").strip()
        if label and label not in query_bits:
            query_bits.append(label)
        path = (rel.get("match_top_path") or rel.get("attachment_file_no") or "").strip()
        if path and path not in query_bits:
            query_bits.append(path)

    # Keep a short, stable query input so matchers are comparable.
    query_bits = query_bits[:20]
    query_title = title or (query_bits[0] if query_bits else "")
    query_duties = tuple(query_bits[1:]) if query_bits else ()
    return query_title, query_duties


def _query_from_case_fallback(case: NoticeJDCase) -> tuple[str, tuple[str, ...]]:
    title = case.notice_title or case.job_title
    return title, (
        case.notice_title,
        case.job_title,
    )


def _field_f1(
    predicted: tuple[str, ...],
    truth: tuple[str, ...],
) -> float:
    pred_tokens = _tokens_from_values(predicted)
    truth_tokens = _tokens_from_values(truth)
    if not pred_tokens and not truth_tokens:
        return 1.0
    if not pred_tokens or not truth_tokens:
        return 0.0
    overlap = len(pred_tokens & truth_tokens)
    precision = overlap / len(pred_tokens)
    recall = overlap / len(truth_tokens)
    if precision <= 0.0 or recall <= 0.0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _best_truth_case(notice_cases: tuple[NoticeJDCase, ...]) -> NoticeJDCase:
    return max(
        notice_cases,
        key=lambda item: (item.relation_confidence, len(item.tokens), len(item.fields)),
    )


def _evaluate(
    cases: tuple[NoticeJDCase, ...],
    notices: tuple[dict[str, Any], ...],
    *,
    top_k: int,
    min_confidence: float,
    max_cases_per_notice: int,
    scoring: MatchScoringConfig,
) -> dict[str, Any]:
    cases_by_notice: dict[str, list[NoticeJDCase]] = defaultdict(list)
    for case in cases:
        cases_by_notice[case.notice_idx].append(case)

    metrics = Counter()
    rank_hist: list[int] = []
    score_gap: list[float] = []
    quality_f1: list[float] = []
    missing_reason_count: Counter[str] = Counter()
    misses: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    relation_counter: Counter[str] = Counter()

    total = len(notices)
    evaluated = 0
    for notice in notices:
        notice_idx = str(notice.get("idx") or "").strip()
        if not notice_idx:
            continue

        truth_cases = tuple(cases_by_notice.get(notice_idx, ()))
        if not truth_cases:
            continue
        evaluated += 1

        relation_type = str(notice.get("relation_type") or "unknown")
        relation_counter[relation_type] += 1

        if "notice" in notice:
            query_title, query_duties = _query_from_relation(notice)
        else:
            fallback_case = truth_cases[0]
            query_title, query_duties = _query_from_case_fallback(fallback_case)

        matches = match_cases_with_scores(
            query_title,
            query_duties,
            cases,
            top_k=top_k,
            min_confidence=min_confidence,
            max_cases_per_notice=max_cases_per_notice,
            scoring=scoring,
        )
        ranked: list[dict[str, Any]] = [
            {
                "rank": rank,
                "case_id": match.case.case_id,
                "notice_idx": match.case.notice_idx,
                "job_title": match.case.job_title,
                "score": match.score,
                "overlap_ratio": match.overlap_ratio,
                "title_overlap_ratio": match.title_overlap_ratio,
                "relation_confidence": match.relation_confidence,
                "relation_type": match.relation_type,
            }
            for rank, match in enumerate(matches, start=1)
        ]

        hits = [entry["rank"] for entry in ranked if entry["notice_idx"] == notice_idx]
        row: dict[str, Any] = {
            "notice_idx": notice_idx,
            "relation_type": relation_type,
            "query_title": query_title,
            "query_duties": query_duties[:5],
            "match_count": len(matches),
            "relation_confidence": notice.get("relation_confidence", 0.0),
            "best_match_rank": None,
            "hit1": False,
            "hit3": False,
            "hit5": False,
            "score_gap": None,
            "answer_f1": None,
            "top_candidate": None,
            "top3": ranked[:3],
        }

        if hits:
            best_rank = min(hits)
            row["best_match_rank"] = best_rank
            row["hit1"] = best_rank <= 1
            row["hit3"] = best_rank <= 3
            row["hit5"] = best_rank <= 5
            metrics["hit"] += 1
            metrics[f"hit@{top_k}"] += int(best_rank <= top_k)
            metrics["mrr_sum"] += 1.0 / best_rank
            rank_hist.append(best_rank)

            top_correct_case = _best_truth_case(truth_cases)
            if matches:
                top_case = matches[0].case
                top_truth_duties = top_correct_case.duties
                top_truth_quals = top_correct_case.qualifications
                top_truth_prefs = top_correct_case.preferences
                top_pred_duties = top_case.duties
                top_pred_quals = top_case.qualifications
                top_pred_prefs = top_case.preferences
                duty_f1 = _field_f1(top_pred_duties, top_truth_duties)
                qual_f1 = _field_f1(top_pred_quals, top_truth_quals)
                pref_f1 = _field_f1(top_pred_prefs, top_truth_prefs)
                answer_f1 = (duty_f1 * 0.7) + (qual_f1 * 0.2) + (pref_f1 * 0.1)
                row["answer_f1"] = round(answer_f1, 4)
                quality_f1.append(answer_f1)
            if best_rank > 1:
                first_correct = next(m for m in ranked if m["notice_idx"] == notice_idx)
                row["score_gap"] = round((matches[0].score - first_correct["score"]), 4)
                if matches:
                    score_gap.append(matches[0].score - first_correct["score"])
            row["top_candidate"] = ranked[0]["case_id"] if ranked else None
        else:
            metrics["miss"] += 1
            reason = "no_candidate" if not matches else "no_correct_candidate"
            missing_reason_count[reason] += 1
            if matches:
                row["score_gap"] = round(matches[0].score, 4)
                if len(matches) > 0:
                    score_gap.append(matches[0].score)

        rows.append(row)

    row_by_rank = [row for row in rows if row.get("best_match_rank") == 1]

    def _recall(rate: int) -> int:
        return int(row.get(f"hit{rate}") for row in rows)

    recall_at_1 = len([row for row in rows if row["hit1"]]) / len(rows) if rows else 0.0
    recall_at_3 = len([row for row in rows if row["hit3"]]) / len(rows) if rows else 0.0
    recall_at_5 = len([row for row in rows if row["hit5"]]) / len(rows) if rows else 0.0
    mrr = metrics["mrr_sum"] / evaluated if evaluated else 0.0

    for row in rows:
        if row["best_match_rank"] is None:
            misses.append(row)
            continue
        if row["best_match_rank"] > 3 and len(rows) > 0:
            misses.append(row)

    misses.sort(key=lambda item: (item["best_match_rank"] is None, item["best_match_rank"] or 999))

    summary = {
        "total_notices": total,
        "evaluated_notices": evaluated,
        "hit": metrics["hit"],
        "miss": metrics["miss"],
        "recall@1": round(recall_at_1, 4),
        "recall@3": round(recall_at_3, 4),
        "recall@5": round(recall_at_5, 4),
        "mrr": round(mrr, 4),
        "match_rate": round(metrics["hit"] / evaluated, 4) if evaluated else 0.0,
        "median_rank": int(median(rank_hist)) if rank_hist else None,
        "avg_score_gap": round(sum(score_gap) / len(score_gap), 4) if score_gap else None,
        "avg_answer_f1": round(sum(quality_f1) / len(quality_f1), 4) if quality_f1 else None,
        "relation_counter": dict(sorted(relation_counter.items())),
        "missing_reason_counter": dict(sorted(missing_reason_count.items())),
        "top_k": top_k,
        "min_confidence": min_confidence,
        "scoring": {
            "overlap_weight": scoring.overlap_weight,
            "title_weight": scoring.title_weight,
            "duty_weight": scoring.duty_weight,
            "coverage_weight": scoring.coverage_weight,
            "min_overlap_tokens": scoring.min_overlap_tokens,
        },
    }

    return {
        "summary": summary,
        "rows": rows,
        "misses": misses[:150],
        "examples": row_by_rank[:60],
    }


def _build_scoring_from_args(args: argparse.Namespace) -> MatchScoringConfig:
    return MatchScoringConfig(
        overlap_weight=args.overlap_weight,
        title_weight=args.title_weight,
        duty_weight=args.duty_weight,
        coverage_weight=args.coverage_weight,
        min_overlap_tokens=max(1, args.min_overlap_tokens),
    )


def _print_summary(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(
        f"evaluated={s['evaluated_notices']} "
        f"hit={s['hit']} miss={s['miss']} "
        f"recall@1={s['recall@1']:.4f} recall@3={s['recall@3']:.4f} "
        f"recall@5={s['recall@5']:.4f} mrr={s['mrr']:.4f}"
    )
    if s["avg_answer_f1"] is not None:
        print(f"avg_answer_f1={s['avg_answer_f1']:.4f} avg_score_gap={s['avg_score_gap']}")


def _write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate notice->JD matching and estimate recall/F1 by retrieval.",
    )
    parser.add_argument(
        "--case-library",
        default=str(Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_case_library.json"),
        help="case library JSON built from relation analysis",
    )
    parser.add_argument(
        "--relation-file",
        default="",
        help="relation analysis JSON output for notice-level query context",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="rank threshold used for recall and top candidate extraction",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.4,
        help="minimum relation confidence used to pre-filter case list",
    )
    parser.add_argument(
        "--max-cases-per-notice",
        type=int,
        default=1,
        help="max candidates returned per notice_id",
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "build" / "notice_jd_matching_report.json"),
        help="json report output",
    )
    parser.add_argument(
        "--overlap-weight",
        type=float,
        default=0.54,
    )
    parser.add_argument(
        "--title-weight",
        type=float,
        default=0.22,
    )
    parser.add_argument(
        "--duty-weight",
        type=float,
        default=0.16,
    )
    parser.add_argument(
        "--coverage-weight",
        type=float,
        default=0.08,
    )
    parser.add_argument(
        "--min-overlap-tokens",
        type=int,
        default=1,
    )
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    case_library_path = Path(args.case_library)
    cases = load_case_library(case_library_path)
    if not cases:
        print(f"case_library_empty={case_library_path}")
        return 1

    relation_path = Path(args.relation_file) if args.relation_file else None
    notices = _load_relation_notices(relation_path)
    if not notices:
        notices = tuple(item for _, item in _build_notices_from_cases(cases))
        if not notices:
            print("no_evaluation_source")
            return 1

    scoring = _build_scoring_from_args(args)
    report = _evaluate(
        cases=cases,
        notices=notices,
        top_k=args.top_k,
        min_confidence=args.min_confidence,
        max_cases_per_notice=args.max_cases_per_notice,
        scoring=scoring,
    )

    output_path = Path(args.output)
    _write_json(report, output_path)
    _print_summary(report)
    print(f"report={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
