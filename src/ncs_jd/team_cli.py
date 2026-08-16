"""Command-line entry point for the local Codex + Claude rewrite team.

The command consumes an already validated JobProfile.  It never performs NCS
scope selection and never exposes raw provider prompts or output in its audit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from pydantic import ValidationError

from ncs_jd.adapters.cli_llm import CliLlmAdapter
from ncs_jd.application.llm_orchestrator import TeamRewriteOrchestrator, TeamRewritePort
from ncs_jd.application.llm_rewriter import (
    apply_rewrite_suggestions,
    LlmCliError,
    LlmProvider,
)
from ncs_jd.domain.job_profile import JobProfile


_MAX_PROFILE_BYTES = 5 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ncs-jd-team",
        description="검증된 JobProfile 문구를 Codex와 Claude가 상호 검토합니다.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="두 CLI의 안전하게 정규화된 준비 상태를 확인합니다.")

    deliberate = subparsers.add_parser(
        "deliberate",
        help="두 번의 제한된 라운드로 문구 재서술 합의를 계산합니다.",
    )
    deliberate.add_argument("profile", type=Path, help="입력 JobProfile JSON")
    deliberate.add_argument("--report", type=Path, help="해시 기반 팀 감사 보고서 JSON")
    deliberate.add_argument(
        "--output-profile",
        type=Path,
        help="합의 문구가 적용된 draft JobProfile JSON",
    )
    return parser


def run(argv: Sequence[str] | None = None, *, llm_cli: TeamRewritePort | None = None) -> int:
    args = _parser().parse_args(argv)
    adapter = llm_cli or CliLlmAdapter()
    try:
        if args.command == "status":
            statuses = adapter.list_provider_statuses()
            team_statuses = tuple(
                item
                for item in statuses
                if item.provider in (LlmProvider.CODEX, LlmProvider.CLAUDE)
            )
            by_provider = {item.provider: item for item in team_statuses}
            ready = all(
                provider in by_provider
                and by_provider[provider].installed
                and by_provider[provider].logged_in
                for provider in (LlmProvider.CODEX, LlmProvider.CLAUDE)
            )
            _emit(
                {
                    "status": "ready" if ready else "not_ready",
                    "code": "team_ready" if ready else "team_login_required",
                    "providers": [item.as_dict() for item in team_statuses],
                }
            )
            return 0 if ready else 2

        profile = _read_profile(args.profile)
        _require_ready(adapter)
        result = TeamRewriteOrchestrator(adapter).deliberate(profile)
        report = result.as_dict()
        if args.report is not None:
            _write_json(args.report, report)
        if args.output_profile is not None:
            output_profile = (
                apply_rewrite_suggestions(profile, result.selected_rewrites)
                if result.selected_rewrites is not None
                else profile
            )
            _write_json(
                args.output_profile,
                output_profile.model_dump(mode="json", by_alias=True),
            )
        _emit(report)
        return 0
    except (OSError, UnicodeError, ValidationError, ValueError) as exc:
        _emit_error("invalid_profile_or_path", str(exc))
        return 2
    except LlmCliError as exc:
        _emit_error(exc.code, "팀 재서술을 완료하지 못했습니다.", retryable=exc.retryable)
        return 3


def _read_profile(path: Path) -> JobProfile:
    content = path.read_bytes()
    if len(content) > _MAX_PROFILE_BYTES:
        raise ValueError("profile_too_large")
    return JobProfile.model_validate_json(content)


def _require_ready(adapter: TeamRewritePort) -> None:
    statuses = adapter.list_provider_statuses()
    by_provider = {item.provider: item for item in statuses}
    for provider in (LlmProvider.CODEX, LlmProvider.CLAUDE):
        status = by_provider.get(provider)
        if status is None or not status.installed:
            raise LlmCliError("provider_not_installed", retryable=True)
        if not status.logged_in:
            raise LlmCliError("llm_login_required", retryable=True)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _emit_error(code: str, message: str, *, retryable: bool = False) -> None:
    print(
        json.dumps(
            {"error": {"code": code, "message": message, "retryable": retryable}},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
