"""Run the official CLI as a tool-using agent against the bundled NCS MCP.

This is the counterpart to :class:`~ncs_jd.adapters.cli_llm.CliLlmAdapter`,
which deliberately runs with tools disabled.  Here the CLI is launched with the
NCS MCP attached and ``--strict-mcp-config`` so it sees that server and nothing
else from the user's own MCP configuration.  Only the two read-only NCS tools
are allow-listed, so the agent can search and read NCS evidence but cannot touch
the filesystem, the network, or any other server.

Output arrives as ``stream-json`` lines, which is what makes an indeterminate
progress display possible: each ``tool_use`` block is one observable step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ncs_jd.application.agent_drafting import (
    AgentDraftError,
    AgentDraftRequest,
    AgentDraftResult,
    AgentProgress,
    ProgressCallback,
    agent_draft_prompt,
    validate_agent_draft,
)
from ncs_jd.application.llm_rewriter import LlmProvider


DEFAULT_AGENT_TIMEOUT_SECONDS = 900.0
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_MCP_SERVER_NAME = "ncs"
_NCS_TOOLS = ("ncs_search", "ncs_unit_detail")
# Claude namespaces MCP tools by server; Codex reports the bare tool name.
_CLAUDE_ALLOWED_TOOLS = tuple(f"mcp__{_MCP_SERVER_NAME}__{name}" for name in _NCS_TOOLS)
_TOOL_LABELS = {
    "ncs_search": "NCS 검색",
    "ncs_unit_detail": "능력단위 상세 조회",
    "ToolSearch": "도구 준비",
}
_AUTH_FAILURE_MARKERS = (
    "not logged in", "login required", "authentication required", "unauthorized",
    "please login", "please log in", "not authenticated",
)
_USAGE_FAILURE_MARKERS = (
    "usage limit", "rate limit", "quota", "credit balance", "credits exhausted",
    "usage exhausted", "limit reached", "session limit",
)


@dataclass(frozen=True, slots=True)
class NcsMcpServerSpec:
    """How to launch the bundled NCS MCP sidecar over stdio."""

    command: str
    database_path: str
    args: tuple[str, ...] = ()
    extra_env: tuple[tuple[str, str], ...] = ()

    def as_config(self) -> dict[str, Any]:
        return {
            "mcpServers": {
                _MCP_SERVER_NAME: {
                    "command": self.command,
                    "args": list(self.args),
                    "env": {
                        **dict(self.extra_env),
                        "NCS_DB_PATH": self.database_path,
                        "NCS_MCP_READ_ONLY": "1",
                        "NCS_MCP_ENABLE_OPERATOR_TOOLS": "0",
                    },
                }
            }
        }


class _CliDialect(Protocol):
    """How one official CLI is launched and how its event stream reads.

    The two supported CLIs agree on nothing that matters here: Claude takes an
    MCP config file and emits ``stream-json`` messages, Codex takes ``-c`` TOML
    overrides and emits ``item.*`` events.  Keeping both behind this pair of
    methods is what lets the run loop, the limits and the failure classification
    stay single-copy.
    """

    executable: str

    def build_args(
        self,
        command: Sequence[str],
        server: NcsMcpServerSpec,
        prompt: str,
        root: Path,
    ) -> list[str]: ...

    def handle_event(
        self,
        event: Mapping[str, Any],
        stats: _RunStats,
        emit: _ProgressEmitter,
    ) -> str | None: ...


class _ClaudeDialect:
    executable = "claude"

    def build_args(
        self,
        command: Sequence[str],
        server: NcsMcpServerSpec,
        prompt: str,
        root: Path,
    ) -> list[str]:
        config_path = root / "ncs-mcp.json"
        config_path.write_text(
            json.dumps(server.as_config(), ensure_ascii=False),
            encoding="utf-8",
        )
        return [
            *command,
            "-p",
            prompt,
            "--mcp-config",
            str(config_path),
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(_CLAUDE_ALLOWED_TOOLS),
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def handle_event(
        self,
        event: Mapping[str, Any],
        stats: _RunStats,
        emit: _ProgressEmitter,
    ) -> str | None:
        event_type = event.get("type")
        if event_type == "rate_limit_event":
            emit.send("notice", "제공자 사용량 제한으로 대기 중입니다")
            return None
        if event_type == "result":
            stats.turns = int(event.get("num_turns") or 0)
            stats.duration_ms = int(event.get("duration_ms") or 0)
            stats.error = bool(event.get("is_error"))
            result = event.get("result")
            return result if isinstance(result, str) else None

        message = event.get("message")
        if not isinstance(message, Mapping):
            return None
        final_text: str | None = None
        for block in message.get("content") or ():
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                name = str(block.get("name") or "")
                if name in _CLAUDE_ALLOWED_TOOLS:
                    stats.tool_calls += 1
                emit.send("tool_call", _tool_label(name), _describe_input(block.get("input")))
            elif kind == "tool_result" and block.get("is_error"):
                emit.send("tool_result", "조회 실패 — 다른 검색어로 재시도합니다")
            elif kind == "text" and event_type == "assistant":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    final_text = text
        return final_text


class _CodexDialect:
    executable = "codex"

    def build_args(
        self,
        command: Sequence[str],
        server: NcsMcpServerSpec,
        prompt: str,
        root: Path,
    ) -> list[str]:
        del root  # Codex is configured entirely on the command line.
        overrides = [
            f"mcp_servers.{_MCP_SERVER_NAME}.command={_toml_path(server.command)}",
            f"mcp_servers.{_MCP_SERVER_NAME}.args=["
            + ", ".join(_toml_path(arg) for arg in server.args)
            + "]",
            f"mcp_servers.{_MCP_SERVER_NAME}.env.NCS_DB_PATH={_toml_path(server.database_path)}",
            f'mcp_servers.{_MCP_SERVER_NAME}.env.NCS_MCP_READ_ONLY="1"',
            f'mcp_servers.{_MCP_SERVER_NAME}.env.NCS_MCP_ENABLE_OPERATOR_TOOLS="0"',
            f"mcp_servers.{_MCP_SERVER_NAME}.enabled_tools=["
            + ", ".join(f'"{name}"' for name in _NCS_TOOLS)
            + "]",
            # Non-interactive runs have no one to approve a tool call, and an
            # unapproved call is reported as cancelled rather than failing loudly.
            f'mcp_servers.{_MCP_SERVER_NAME}.default_tools_approval_mode="approve"',
            f"mcp_servers.{_MCP_SERVER_NAME}.required=true",
            f"mcp_servers.{_MCP_SERVER_NAME}.startup_timeout_sec=60",
            'web_search="disabled"',
            'approval_policy="never"',
        ]
        overrides.extend(
            f"mcp_servers.{_MCP_SERVER_NAME}.env.{key}={_toml_path(value)}"
            for key, value in server.extra_env
        )
        args = [
            *command,
            "exec",
            "--json",
            # The user's own config may attach other MCP servers and relax
            # sandboxing, so it is ignored outright rather than merged.
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            "read-only",
            "--disable",
            "shell_tool",
        ]
        for override in overrides:
            args += ["-c", override]
        args.append(prompt)
        return args

    def handle_event(
        self,
        event: Mapping[str, Any],
        stats: _RunStats,
        emit: _ProgressEmitter,
    ) -> str | None:
        event_type = event.get("type")
        if event_type in {"error", "turn.failed"}:
            # `error` carries the message directly; `turn.failed` nests it.
            source = event.get("error") if isinstance(event.get("error"), Mapping) else event
            message = source.get("message")
            if isinstance(message, str) and message.strip():
                stats.error = True
                stats.failure_text = message
            return None
        item = event.get("item")
        if not isinstance(item, Mapping):
            return None
        item_type = item.get("type")
        if item_type == "mcp_tool_call":
            name = str(item.get("tool") or "")
            if event_type == "item.started":
                if name in _NCS_TOOLS:
                    stats.tool_calls += 1
                emit.send("tool_call", _tool_label(name), _describe_input(item.get("arguments")))
            elif event_type == "item.completed" and item.get("status") != "completed":
                emit.send("tool_result", "조회 실패 — 다른 검색어로 재시도합니다")
            return None
        if item_type == "agent_message" and event_type == "item.completed":
            stats.turns += 1
            text = item.get("text")
            return text if isinstance(text, str) and text.strip() else None
        return None


_DIALECTS: dict[LlmProvider, _CliDialect] = {
    LlmProvider.CLAUDE: _ClaudeDialect(),
    LlmProvider.CODEX: _CodexDialect(),
}


class CliAgentDraftRunner:
    """Drive the CLI agent loop and translate its stream into progress events."""

    def __init__(
        self,
        server: NcsMcpServerSpec,
        *,
        provider: LlmProvider = LlmProvider.CLAUDE,
        executable_resolver: Callable[[str], str | None] = shutil.which,
        launcher: Callable[..., Any] = subprocess.Popen,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
    ) -> None:
        dialect = _DIALECTS.get(provider)
        if dialect is None:
            raise ValueError("agent-loop drafting requires the Claude or Codex CLI")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._server = server
        self._provider = provider
        self._dialect = dialect
        self._resolve = executable_resolver
        self._launcher = launcher
        self._environ = dict(os.environ if environ is None else environ)
        self._timeout = timeout_seconds

    def run_draft(
        self,
        request: AgentDraftRequest,
        on_progress: ProgressCallback | None = None,
    ) -> AgentDraftResult:
        command = self._command()
        if command is None:
            raise AgentDraftError("provider_not_installed")

        emit = _ProgressEmitter(on_progress)
        emit.send("started", "NCS 근거 탐색을 시작합니다", self._server.database_path)

        with tempfile.TemporaryDirectory(prefix="ncs-jd-agent-") as temp_name:
            root = Path(temp_name)
            work = root / "work"
            work.mkdir()
            args = self._dialect.build_args(
                command,
                self._server,
                agent_draft_prompt(request),
                root,
            )
            payload, stats = self._stream(args, work, emit)

        emit.send("composing", "직무기술서 항목을 정리하는 중입니다")
        values, codes, notes = validate_agent_draft(payload, request)
        emit.send(
            "completed",
            f"완료 — 능력단위 {len(codes)}개, 도구 호출 {stats.tool_calls}회",
            f"{stats.duration_ms / 1000:.0f}초",
        )
        return AgentDraftResult(
            field_values=values,
            unit_codes=codes,
            notes=notes,
            turns=stats.turns,
            duration_ms=stats.duration_ms,
            tool_calls=stats.tool_calls,
        )

    def _stream(
        self,
        args: Sequence[str],
        cwd: Path,
        emit: _ProgressEmitter,
    ) -> tuple[Mapping[str, Any], _RunStats]:
        try:
            process = self._launcher(
                list(args),
                cwd=str(cwd),
                env=self._safe_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except OSError as exc:
            raise AgentDraftError("provider_unavailable") from exc

        stats = _RunStats()
        final_text: str | None = None
        produced = 0
        started = time.monotonic()
        try:
            for line in process.stdout or ():
                produced += len(line.encode("utf-8"))
                if produced > _MAX_OUTPUT_BYTES:
                    process.kill()
                    raise AgentDraftError("agent_output_too_large", retryable=False)
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = self._dialect.handle_event(event, stats, emit)
                if text is not None:
                    final_text = text
            return_code = process.wait(timeout=self._timeout)
        except AgentDraftError:
            raise
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise AgentDraftError("agent_timeout") from exc
        finally:
            if process.stdout:
                process.stdout.close()

        if not stats.duration_ms:
            # Codex reports token usage but no elapsed time, and the progress
            # line promises a duration either way.
            stats.duration_ms = int((time.monotonic() - started) * 1000)

        stderr = ""
        if process.stderr:
            stderr = process.stderr.read() or ""
            process.stderr.close()
        # A refused turn shows up differently per CLI -- a non-zero exit, a
        # stderr line, a result flagged as an error, or a stdout event -- so all
        # four are pooled before classifying rather than handled separately.
        if return_code != 0 or stats.error:
            raise _classify_failure(
                "\n".join(part for part in (final_text, stats.failure_text, stderr) if part)
            )
        if not final_text:
            raise AgentDraftError("agent_produced_no_result")

        payload = _decode_final_json(final_text)
        return payload, stats

    def _command(self) -> list[str] | None:
        resolved = self._resolve(self._dialect.executable)
        if not resolved:
            return None
        path = Path(resolved)
        if os.name != "nt" or path.suffix.casefold() not in {".cmd", ".bat"}:
            return [resolved]
        from ncs_jd.adapters.cli_llm import _resolve_windows_node_shim

        return _resolve_windows_node_shim(path, self._resolve)

    def _safe_environment(self) -> dict[str, str]:
        # Credentials stay in the CLI's own store; no ad-hoc token or key
        # variables are forwarded, and this mapping is never logged.
        allowed = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
            "LANG", "LC_ALL", "TERM", "COLORTERM",
        }
        return {k: v for k, v in self._environ.items() if k.upper() in allowed}


@dataclass(slots=True)
class _RunStats:
    turns: int = 0
    duration_ms: int = 0
    tool_calls: int = 0
    error: bool = False
    # Why the run failed, in the CLI's own words.  Codex reports a refused turn
    # as a JSON event on stdout rather than on stderr, so without carrying it
    # here a usage limit or an expired login would be classified as a generic
    # provider failure and the person would be told nothing actionable.
    failure_text: str = ""


class _ProgressEmitter:
    """Number the steps so the UI can show an ordered, growing list."""

    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback
        self._step = 0

    def send(self, kind: str, label: str, detail: str = "") -> None:
        if self._callback is None:
            return
        self._step += 1
        self._callback(AgentProgress(kind, self._step, label, detail))  # type: ignore[arg-type]


def _tool_label(name: str) -> str:
    """Name the step in Korean whether the CLI namespaced the tool or not."""

    return _TOOL_LABELS.get(name.rsplit("__", 1)[-1], name)


def _toml_path(value: str) -> str:
    """Quote a path as a TOML string for a Codex ``-c`` override.

    Forward slashes are used rather than escaped backslashes because Windows
    accepts them and a half-escaped Windows path is the kind of thing that fails
    only on someone else's machine.
    """

    return '"' + Path(value).as_posix().replace('"', '\\"') + '"'


def _describe_input(value: object) -> str:
    """Summarize a tool call so the progress line shows real arguments."""

    if not isinstance(value, Mapping):
        return ""
    for key in ("query", "unit_code"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            limit = value.get("limit")
            suffix = f" (limit {limit})" if isinstance(limit, int) else ""
            return f"{item.strip()}{suffix}"
    return ""


def _decode_final_json(text: str) -> Mapping[str, Any]:
    """Parse the agent's answer, tolerating a code fence or surrounding prose."""

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = [line for line in candidate.splitlines() if not line.strip().startswith("```")]
        candidate = "\n".join(lines).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise AgentDraftError("agent_result_not_json") from None
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            raise AgentDraftError("agent_result_not_json") from None
    if not isinstance(payload, Mapping):
        raise AgentDraftError("agent_result_not_json", retryable=False)
    return payload


def _classify_failure(text: str) -> AgentDraftError:
    folded = text.casefold()
    if any(marker in folded for marker in _USAGE_FAILURE_MARKERS):
        return AgentDraftError("llm_usage_exhausted")
    if any(marker in folded for marker in _AUTH_FAILURE_MARKERS):
        return AgentDraftError("llm_login_required")
    return AgentDraftError("agent_provider_failed")


__all__ = ["CliAgentDraftRunner", "NcsMcpServerSpec", "DEFAULT_AGENT_TIMEOUT_SECONDS"]
