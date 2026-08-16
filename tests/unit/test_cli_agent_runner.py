from __future__ import annotations

import io
import json
from typing import Any

import pytest

from ncs_jd.adapters.cli_agent_runner import (
    CliAgentDraftRunner,
    NcsMcpServerSpec,
    _ClaudeDialect,
    _CodexDialect,
    _decode_final_json,
    _ProgressEmitter,
    _RunStats,
)
from ncs_jd.application.agent_drafting import AgentDraftError, AgentDraftRequest, AgentProgress
from ncs_jd.application.document_renderer import SUPPORTED_TEMPLATE_LABELS
from ncs_jd.application.llm_rewriter import LlmProvider


def _server() -> NcsMcpServerSpec:
    return NcsMcpServerSpec(
        command="C:/tools/ncs-mcp.exe",
        database_path="C:/data/ncs_jd_serving.db",
    )


def _request() -> AgentDraftRequest:
    return AgentDraftRequest(
        job_title="통신설비 운영",
        duties=("구내 전화 설비 운영",),
        template_labels=("채용분야", "직무수행내용"),
    )


class _FakeProcess:
    def __init__(self, lines: list[str], *, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = io.StringIO("".join(line if line.endswith("\n") else line + "\n" for line in lines))
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def _result_line(payload: dict[str, object], *, duration_ms: int = 1500) -> str:
    return json.dumps(
        {
            "type": "result",
            "num_turns": 4,
            "duration_ms": duration_ms,
            "is_error": False,
            "result": json.dumps(payload, ensure_ascii=False),
        }
    )


def _tool_line(name: str, query: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": name,
                        "input": {"query": query, "limit": 20},
                    }
                ]
            },
        }
    )


def test_run_draft_streams_tool_calls_and_returns_validated_fields() -> None:
    captured: dict[str, Any] = {}
    events: list[AgentProgress] = []
    payload = {
        "fields": [
            {"label": "채용분야", "value": "통신설비 운영"},
            {"label": "직무수행내용", "value": "구내 전화 설비를 운영한다."},
        ],
        "unit_codes": ["2002010210_25v5"],
        "notes": ["대관 업무 근거 없음"],
    }

    def launcher(args: list[str], **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _FakeProcess(
            [
                _tool_line("mcp__ncs__ncs_search", "구내통신"),
                _result_line(payload),
            ]
        )

    runner = CliAgentDraftRunner(
        _server(),
        executable_resolver=lambda name: "C:/tools/claude.exe" if name == "claude" else None,
        launcher=launcher,
        environ={"PATH": "C:/Windows", "OPENAI_API_KEY": "secret-key"},
    )

    result = runner.run_draft(_request(), on_progress=events.append)

    assert result.field_values == (
        ("채용분야", "통신설비 운영"),
        ("직무수행내용", "구내 전화 설비를 운영한다."),
    )
    assert result.unit_codes == ("2002010210_25v5",)
    assert result.notes == ("대관 업무 근거 없음",)
    assert result.tool_calls == 1
    assert result.turns == 4
    assert [event.kind for event in events] == ["started", "tool_call", "composing", "completed"]
    assert events[1].detail == "구내통신 (limit 20)"
    command = captured["args"]
    assert "--strict-mcp-config" in command
    assert "--mcp-config" in command
    assert "mcp__ncs__ncs_search,mcp__ncs__ncs_unit_detail" in command
    assert captured["env"] == {"PATH": "C:/Windows"}


def test_missing_cli_is_a_normalized_error() -> None:
    runner = CliAgentDraftRunner(
        _server(),
        executable_resolver=lambda name: None,
        launcher=lambda *args, **kwargs: None,
    )
    with pytest.raises(AgentDraftError) as exc:
        runner.run_draft(_request())
    assert exc.value.code == "provider_not_installed"


def test_login_failure_is_classified_from_stderr() -> None:
    def launcher(args: list[str], **kwargs: Any) -> _FakeProcess:
        return _FakeProcess([], returncode=1, stderr="not logged in")

    runner = CliAgentDraftRunner(
        _server(),
        executable_resolver=lambda name: "C:/tools/claude.exe",
        launcher=launcher,
    )
    with pytest.raises(AgentDraftError) as exc:
        runner.run_draft(_request())
    assert exc.value.code == "llm_login_required"


def test_usage_limit_is_classified_from_stderr() -> None:
    def launcher(args: list[str], **kwargs: Any) -> _FakeProcess:
        return _FakeProcess([], returncode=1, stderr="usage limit reached")

    runner = CliAgentDraftRunner(
        _server(),
        executable_resolver=lambda name: "C:/tools/claude.exe",
        launcher=launcher,
    )
    with pytest.raises(AgentDraftError) as exc:
        runner.run_draft(_request())
    assert exc.value.code == "llm_usage_exhausted"


def test_rate_limit_that_truncates_the_result_reports_usage_exhausted() -> None:
    """A mid-run rate limit leaves no final JSON; say so, don't blame parsing."""

    def launcher(args: list[str], **kwargs: Any) -> _FakeProcess:
        # The provider throttles mid-run, then the stream ends cleanly (exit 0)
        # with no valid answer -- exactly the shape a truncated run leaves.
        return _FakeProcess(
            [
                _tool_line("mcp__ncs__ncs_search", "수변전설비"),
                json.dumps({"type": "rate_limit_event"}),
            ]
        )

    runner = CliAgentDraftRunner(
        _server(),
        executable_resolver=lambda name: "C:/tools/claude.exe",
        launcher=launcher,
    )
    with pytest.raises(AgentDraftError) as exc:
        runner.run_draft(_request())
    assert exc.value.code == "llm_usage_exhausted"


def test_json_inside_a_code_fence_is_accepted() -> None:
    fenced = '```json\n{"fields":[{"label":"채용분야","value":"가"}],"unit_codes":[],"notes":[]}\n```'
    payload = _decode_final_json(fenced)
    assert payload["fields"][0]["label"] == "채용분야"


def test_codex_runs_the_same_read_only_ncs_server() -> None:
    captured: dict[str, Any] = {}
    payload = {
        "fields": [{"label": "채용분야", "value": "통신설비 운영"}],
        "unit_codes": [],
        "notes": [],
    }

    def launcher(args: list[str], **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        return _FakeProcess(
            [
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": json.dumps(payload, ensure_ascii=False)},
                    }
                )
            ]
        )

    runner = CliAgentDraftRunner(
        _server(),
        provider=LlmProvider.CODEX,
        executable_resolver=lambda name: "C:/tools/codex.exe" if name == "codex" else None,
        launcher=launcher,
    )
    result = runner.run_draft(_request())

    assert result.field_values == (("채용분야", "통신설비 운영"),)
    command = " ".join(captured["args"])
    # Codex is configured entirely on the command line, so the read-only
    # guarantees have to be visible there rather than in a config file.
    assert "--ignore-user-config" in command
    assert "read-only" in command
    assert 'web_search="disabled"' in command
    assert "ncs_search" in command and "ncs_unit_detail" in command


def test_codex_usage_limit_on_stdout_is_classified() -> None:
    """Codex refuses a turn with a JSON event, not a stderr line or an exit code."""

    def launcher(args: list[str], **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            [
                json.dumps(
                    {
                        "type": "error",
                        "message": "You've hit your usage limit. Visit ... to purchase more credits.",
                    }
                ),
                json.dumps({"type": "turn.failed", "error": {"message": "usage limit"}}),
            ]
        )

    runner = CliAgentDraftRunner(
        _server(),
        provider=LlmProvider.CODEX,
        executable_resolver=lambda name: "C:/tools/codex.exe",
        launcher=launcher,
    )
    with pytest.raises(AgentDraftError) as exc:
        runner.run_draft(_request())
    assert exc.value.code == "llm_usage_exhausted"


def test_codex_login_failure_on_stdout_is_classified() -> None:
    def launcher(args: list[str], **kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            [json.dumps({"type": "error", "message": "You are not logged in."})]
        )

    runner = CliAgentDraftRunner(
        _server(),
        provider=LlmProvider.CODEX,
        executable_resolver=lambda name: "C:/tools/codex.exe",
        launcher=launcher,
    )
    with pytest.raises(AgentDraftError) as exc:
        runner.run_draft(_request())
    assert exc.value.code == "llm_login_required"


def test_unsupported_provider_is_refused() -> None:
    with pytest.raises(ValueError):
        CliAgentDraftRunner(_server(), provider=LlmProvider.OFF)


def test_progress_emitter_numbers_steps() -> None:
    seen: list[AgentProgress] = []
    emit = _ProgressEmitter(seen.append)
    emit.send("started", "시작")
    emit.send("tool_call", "NCS 검색", "구내통신")
    assert [item.step for item in seen] == [1, 2]
    assert seen[1].detail == "구내통신"


def test_claude_handle_event_counts_only_allow_listed_tools() -> None:
    stats = _RunStats()
    emit = _ProgressEmitter(None)
    _ClaudeDialect().handle_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "ToolSearch", "input": {}},
                    {
                        "type": "tool_use",
                        "name": "mcp__ncs__ncs_unit_detail",
                        "input": {"unit_code": "2002010210_25v5"},
                    },
                ]
            },
        },
        stats,
        emit,
    )
    assert stats.tool_calls == 1


def test_codex_handle_event_counts_only_allow_listed_tools() -> None:
    stats = _RunStats()
    emit = _ProgressEmitter(None)
    dialect = _CodexDialect()
    for tool in ("shell", "ncs_unit_detail"):
        dialect.handle_event(
            {
                "type": "item.started",
                "item": {"type": "mcp_tool_call", "tool": tool, "arguments": {}},
            },
            stats,
            emit,
        )
    assert stats.tool_calls == 1


def test_supported_template_labels_remain_the_agent_default() -> None:
    assert "비고/근거" in SUPPORTED_TEMPLATE_LABELS
    assert len(SUPPORTED_TEMPLATE_LABELS) == 13
