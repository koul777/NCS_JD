"""Official Codex/Claude CLI adapter for validated prose rewrites."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ncs_jd.application.llm_rewriter import (
    LlmCliError,
    LlmProvider,
    LoginStartResult,
    ProviderStatus,
    RewriteSuggestion,
    RewriteSuggestionResult,
)
from ncs_jd.application.llm_orchestrator import (
    PeerReviewDecision,
    PeerReviewRequest,
    PeerReviewResult,
)
from ncs_jd.application.llm_scope_selection import (
    ScopeSelectionError,
    ScopeSelectionRequest,
    ScopeSelectionResult,
    scope_selection_prompt,
    scope_selection_schema,
    validate_scope_selection,
)
from ncs_jd.domain.job_profile import Duty, JobProfile


_STATUS_TIMEOUT_SECONDS = 10.0
_REWRITE_TIMEOUT_SECONDS = 180.0
_MAX_REWRITE_DUTIES = 8
_LOGIN_TIMEOUT_SECONDS = 10 * 60.0
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_PROMPT_BYTES = 512 * 1024
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_NUMBER_RE = re.compile(r"(?<!\d)\d+(?:[.,]\d+)*(?!\d)")
_IDENTIFIER_RE = re.compile(
    r"(?i)(?:\b(?:ref|task|duty|ksa)[-_][a-z0-9_.:-]+\b|\b\d{8,}_[a-z0-9]+\b)"
)
_SENSITIVE_ADDITION_TERMS = (
    "자격", "자격증", "학력", "학위", "경력", "근속", "법적", "법률", "법령",
    "의무", "license", "certification", "degree", "education", "experience",
    "years", "legal", "statutory", "mandatory",
)
_AUTH_FAILURE_MARKERS = (
    "not logged in", "login required", "authentication required", "unauthorized",
    "please login", "please log in", "not authenticated",
)
_USAGE_FAILURE_MARKERS = (
    "usage limit", "rate limit", "quota", "credit balance", "credits exhausted",
    "usage exhausted", "limit reached", "session limit", "session usage limit",
    "you've hit your limit", "you have hit your limit",
)


@dataclass(slots=True)
class _LoginProcess:
    process: Any
    temporary_directory: tempfile.TemporaryDirectory[str]
    started_at: float


class CliLlmAdapter:
    """Invoke only fixed, allow-listed official CLI commands.

    All subprocess collaborators are injectable.  The default implementation
    uses argument arrays and ``shell=False`` and never reads CLI credential
    files, tokens, cookies, identity fields, stdout, or stderr into a public
    status response.
    """

    def __init__(
        self,
        *,
        executable_resolver: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        launcher: Callable[..., Any] = subprocess.Popen,
        environ: Mapping[str, str] | None = None,
        status_timeout_seconds: float = _STATUS_TIMEOUT_SECONDS,
        rewrite_timeout_seconds: float = _REWRITE_TIMEOUT_SECONDS,
        login_timeout_seconds: float = _LOGIN_TIMEOUT_SECONDS,
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
        max_prompt_bytes: int = _MAX_PROMPT_BYTES,
    ) -> None:
        if min(
            status_timeout_seconds,
            rewrite_timeout_seconds,
            login_timeout_seconds,
            max_output_bytes,
            max_prompt_bytes,
        ) <= 0:
            raise ValueError("CLI limits must be positive")
        self._resolve = executable_resolver
        self._runner = runner
        self._launcher = launcher
        self._environ = dict(os.environ if environ is None else environ)
        self._status_timeout = status_timeout_seconds
        self._rewrite_timeout = rewrite_timeout_seconds
        self._login_timeout = login_timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._max_prompt_bytes = max_prompt_bytes
        self._login_processes: dict[LlmProvider, _LoginProcess] = {}
        self._login_results: dict[LlmProvider, str] = {}
        self._login_lock = threading.Lock()

    def list_provider_statuses(self) -> tuple[ProviderStatus, ...]:
        return (
            ProviderStatus(
                provider=LlmProvider.OFF,
                installed=True,
                logged_in=False,
                code="llm_off",
            ),
            self.provider_status(LlmProvider.CODEX),
            self.provider_status(LlmProvider.CLAUDE),
        )

    def provider_status(self, provider: LlmProvider) -> ProviderStatus:
        if provider == LlmProvider.OFF:
            return ProviderStatus(provider, True, False, code="llm_off")
        command = self._command(provider)
        in_progress, completed_code = self._login_state(provider)
        if command is None:
            return ProviderStatus(provider, False, False, in_progress, "not_installed")
        if in_progress:
            return ProviderStatus(provider, True, False, True, "login_in_progress")
        if completed_code in {"login_timed_out", "login_failed"}:
            # Keep the normalized result visible for one poll, then allow the
            # ordinary authentication check to become authoritative.
            with self._login_lock:
                self._login_results.pop(provider, None)
            return ProviderStatus(provider, True, False, False, completed_code)

        args = command + (
            ["login", "status"]
            if provider == LlmProvider.CODEX
            else ["auth", "status", "--json"]
        )
        try:
            result = self._run(args, timeout=self._status_timeout)
        except subprocess.TimeoutExpired:
            return ProviderStatus(provider, True, False, False, "status_timeout")
        except OSError:
            return ProviderStatus(provider, True, False, False, "status_unavailable")

        authenticated, auth_code = self._subscription_auth_status(provider, result)
        return ProviderStatus(
            provider,
            True,
            authenticated,
            False,
            auth_code,
        )

    def start_login(self, provider: LlmProvider) -> LoginStartResult:
        if provider == LlmProvider.OFF:
            raise LlmCliError("provider_off", retryable=False)
        command = self._command(provider)
        if command is None:
            raise LlmCliError("provider_not_installed", retryable=True)

        with self._login_lock:
            self._reap_login_locked(provider)
            if provider in self._login_processes:
                raise LlmCliError("login_already_running", retryable=True)

            temp_dir = tempfile.TemporaryDirectory(prefix="ncs-jd-login-")
            args = command + (
                ["login", "--device-auth"]
                if provider == LlmProvider.CODEX
                else ["auth", "login"]
            )
            kwargs: dict[str, Any] = {
                "cwd": str(Path(temp_dir.name)),
                "env": self._safe_environment(),
                "shell": False,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            try:
                process = self._launcher(args, **kwargs)
            except OSError as exc:
                temp_dir.cleanup()
                raise LlmCliError("login_launch_failed", retryable=True) from exc
            self._login_processes[provider] = _LoginProcess(process, temp_dir, time.monotonic())
            self._login_results.pop(provider, None)
        return LoginStartResult(provider, True, "login_started")

    def suggest_rewrites(
        self,
        profile: JobProfile,
        provider: LlmProvider,
    ) -> RewriteSuggestionResult:
        if provider == LlmProvider.OFF:
            return RewriteSuggestionResult(provider, "not_applied", "llm_off")
        command = self._command(provider)
        if command is None:
            raise LlmCliError("provider_not_installed", retryable=True)

        fields = _rewrite_fields(profile)
        schema = _output_schema(tuple(fields))
        prompt = _rewrite_prompt(fields)
        try:
            payload = self._invoke_structured(provider, command, prompt, schema, "suggestions")
            suggestions = _validate_suggestions(payload, fields, provider)
        except LlmCliError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise LlmCliError("invalid_rewrite_response", retryable=True) from exc
        return RewriteSuggestionResult(provider, "suggestions_ready", "suggestions_ready", suggestions)

    def review_rewrite_proposal(self, request: PeerReviewRequest) -> PeerReviewResult:
        """Review only an already validated allow-listed rewrite proposal."""

        reviewer = request.reviewer
        proposal = request.proposal
        candidate = proposal.provider
        if (
            reviewer not in {LlmProvider.CODEX, LlmProvider.CLAUDE}
            or candidate not in {LlmProvider.CODEX, LlmProvider.CLAUDE}
            or reviewer == candidate
        ):
            raise LlmCliError("invalid_review_request", retryable=False)
        command = self._command(reviewer)
        if command is None:
            raise LlmCliError("provider_not_installed", retryable=True)
        if (
            proposal.status != "suggestions_ready"
            or not proposal.suggestions
            or any(item.provider != candidate for item in proposal.suggestions)
        ):
            raise LlmCliError("invalid_review_request", retryable=False)

        locators = tuple(item.field_locator for item in proposal.suggestions)
        if len(set(locators)) != len(locators) or any(
            not _is_allowed_field_locator(locator) for locator in locators
        ):
            raise LlmCliError("invalid_review_request", retryable=False)
        schema = _review_output_schema(locators)
        prompt = _review_prompt(proposal)
        try:
            payload = self._invoke_structured(reviewer, command, prompt, schema, "reviews")
            decisions = _validate_review_decisions(payload, locators)
        except LlmCliError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise LlmCliError("invalid_review_response", retryable=True) from exc
        return PeerReviewResult(
            reviewer=reviewer,
            candidate_provider=candidate,
            status="review_ready",
            code="review_ready",
            decisions=decisions,
        )

    def select_ncs_scope(
        self,
        request: ScopeSelectionRequest,
        provider: LlmProvider,
    ) -> ScopeSelectionResult:
        """Choose competency units from the closed, MCP-sourced candidate pool.

        The output schema pins ``unit_code`` to an enum of the pool's codes and
        ``validate_scope_selection`` re-checks every returned code, so this call
        can only ever narrow NCS evidence the MCP already produced.
        """

        if provider == LlmProvider.OFF:
            raise ScopeSelectionError("llm_off", retryable=False)
        command = self._command(provider)
        if command is None:
            raise ScopeSelectionError("provider_not_installed")

        schema = scope_selection_schema(request.candidate_codes)
        prompt = scope_selection_prompt(request)
        try:
            payload = self._invoke_structured(provider, command, prompt, schema, "selections")
        except LlmCliError as exc:
            raise ScopeSelectionError(exc.code, retryable=exc.retryable) from exc
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise ScopeSelectionError("invalid_scope_selection_response") from exc
        return validate_scope_selection(payload, request)

    def complete_structured(
        self,
        provider: LlmProvider,
        prompt: str,
        schema: Mapping[str, Any],
        expected_key: str,
    ) -> dict[str, Any]:
        """Run a tools-off structured CLI completion and return the JSON object."""

        command = self._command(provider)
        if command is None:
            raise LlmCliError("provider_not_installed", retryable=True)
        return self._invoke_structured(provider, command, prompt, schema, expected_key)

    def _invoke_structured(
        self,
        provider: LlmProvider,
        command: list[str],
        prompt: str,
        schema: Mapping[str, Any],
        expected_key: str,
    ) -> dict[str, Any]:
        if len(prompt.encode("utf-8")) > self._max_prompt_bytes:
            raise LlmCliError("rewrite_input_too_large", retryable=False)

        with tempfile.TemporaryDirectory(prefix="ncs-jd-rewrite-") as temp_name:
            root = Path(temp_name)
            work = root / "work"
            work.mkdir()
            if provider == LlmProvider.CODEX:
                schema_path = root / "output-schema.json"
                schema_path.write_text(
                    json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                args = command + [
                    "exec",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                    "--output-schema",
                    str(schema_path),
                    "-",
                ]
            else:
                args = command + [
                    "-p",
                    "--output-format",
                    "json",
                    "--no-session-persistence",
                    "--safe-mode",
                    "--tools",
                    "",
                    "--permission-mode",
                    "dontAsk",
                    "--json-schema",
                    json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                ]
            try:
                result = self._run(
                    args,
                    input_text=prompt,
                    cwd=work,
                    timeout=self._rewrite_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise LlmCliError("rewrite_timeout", retryable=True) from exc
            except OSError as exc:
                raise LlmCliError("provider_unavailable", retryable=True) from exc

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.returncode != 0:
            raise _classify_cli_failure(f"{stdout}\n{stderr}")
        if len(stdout.encode("utf-8")) > self._max_output_bytes:
            raise LlmCliError("rewrite_output_too_large", retryable=False)
        return _decode_provider_payload(provider, stdout, expected_key=expected_key)

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        input_text: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "env": self._safe_environment(),
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "check": False,
            "shell": False,
        }
        if input_text is not None:
            kwargs["input"] = input_text
        return self._runner(list(args), **kwargs)

    def _command(self, provider: LlmProvider) -> list[str] | None:
        name = {LlmProvider.CODEX: "codex", LlmProvider.CLAUDE: "claude"}.get(provider)
        resolved = self._resolve(name) if name else None
        if not resolved:
            return None
        path = Path(resolved)
        if os.name != "nt" or path.suffix.casefold() not in {".cmd", ".bat"}:
            return [resolved]
        return _resolve_windows_node_shim(path, self._resolve)

    def _login_state(self, provider: LlmProvider) -> tuple[bool, str | None]:
        with self._login_lock:
            self._reap_login_locked(provider)
            return provider in self._login_processes, self._login_results.get(provider)

    def _reap_login_locked(self, provider: LlmProvider) -> None:
        record = self._login_processes.get(provider)
        if record is None:
            return
        if time.monotonic() - record.started_at > self._login_timeout:
            try:
                record.process.terminate()
            except OSError:
                pass
            self._finish_login_locked(provider, "login_timed_out")
            return
        return_code = record.process.poll()
        if return_code is not None:
            self._finish_login_locked(provider, "login_completed" if return_code == 0 else "login_failed")

    def _finish_login_locked(self, provider: LlmProvider, code: str) -> None:
        record = self._login_processes.pop(provider)
        try:
            record.temporary_directory.cleanup()
        except OSError:
            # The new Windows console may take a moment to release its cwd.
            # Credential state is still owned exclusively by the official CLI.
            pass
        self._login_results[provider] = code

    @staticmethod
    def _subscription_auth_status(
        provider: LlmProvider, result: subprocess.CompletedProcess[str]
    ) -> tuple[bool, str]:
        if result.returncode != 0:
            return False, "not_authenticated"
        if provider == LlmProvider.CODEX:
            text = f"{result.stdout or ''} {result.stderr or ''}".casefold()
            if "logged in using chatgpt" in text:
                return True, "authenticated"
            if any(marker in text for marker in ("api key", "access token", "api token")):
                return False, "unsupported_auth_method"
            return False, "not_authenticated"
        try:
            payload = json.loads(result.stdout or "{}")
        except (json.JSONDecodeError, TypeError):
            return False, "not_authenticated"
        if not isinstance(payload, dict):
            return False, "not_authenticated"
        logged_in = next(
            (
                bool(payload[key])
                for key in ("loggedIn", "logged_in", "authenticated")
                if isinstance(payload.get(key), bool)
            ),
            False,
        )
        if not logged_in:
            return False, "not_authenticated"
        auth_method = payload.get("authMethod", payload.get("auth_method"))
        api_provider = payload.get("apiProvider", payload.get("api_provider"))
        method = auth_method.strip().casefold() if isinstance(auth_method, str) else ""
        provider_name = api_provider.strip().casefold() if isinstance(api_provider, str) else ""
        allowed_methods = {"claude.ai", "claude_ai", "oauth", "subscription"}
        if method in allowed_methods and provider_name == "firstparty":
            return True, "authenticated"
        return False, "unsupported_auth_method"

    def _safe_environment(self) -> dict[str, str]:
        # Authentication remains in the official CLIs' own local stores.  We
        # intentionally do not forward ad-hoc token/key/password environment
        # variables, nor do we ever log this mapping.
        allowed = {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
            "LANG", "LC_ALL", "TERM", "COLORTERM",
        }
        return {key: value for key, value in self._environ.items() if key.upper() in allowed}


def _rewrite_fields(profile: JobProfile) -> dict[str, str]:
    fields = {"job_description.job_purpose.text": profile.job_description.job_purpose.text}
    fields.update(
        {
            f"job_description.duties[id={duty.id}].summary": duty.summary
            for duty in _representative_duties(profile)
        }
    )
    return fields


def _representative_duties(profile: JobProfile) -> tuple[Duty, ...]:
    duties = tuple(profile.job_description.duties)
    if len(duties) <= _MAX_REWRITE_DUTIES:
        return duties
    indexes = tuple(
        round(index * (len(duties) - 1) / (_MAX_REWRITE_DUTIES - 1))
        for index in range(_MAX_REWRITE_DUTIES)
    )
    return tuple(duties[index] for index in dict.fromkeys(indexes))


def _output_schema(locators: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["suggestions"],
        "properties": {
            "suggestions": {
                "type": "array",
                "minItems": len(locators),
                "maxItems": len(locators),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field_locator", "suggestion"],
                    "properties": {
                        "field_locator": {"type": "string", "enum": list(locators)},
                        "suggestion": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                },
            }
        },
    }


def _review_output_schema(locators: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reviews"],
        "properties": {
            "reviews": {
                "type": "array",
                "minItems": len(locators),
                "maxItems": len(locators),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field_locator", "verdict", "code"],
                    "properties": {
                        "field_locator": {"type": "string", "enum": list(locators)},
                        "verdict": {"type": "string", "enum": ["accept", "reject"]},
                        "code": {
                            "type": "string",
                            "enum": ["meaning_preserved", "meaning_not_preserved"],
                        },
                    },
                },
            }
        },
    }


def _rewrite_prompt(fields: Mapping[str, str]) -> str:
    payload = [{"field_locator": locator, "original": text} for locator, text in fields.items()]
    return (
        "당신은 확정된 사실을 추가하지 않는 한국어 문장 재서술기입니다. 아래 JSON의 모든 항목에 대해 "
        "문장 표현만 다듬으세요. 객체, 항목, field_locator, ID, source_refs, 숫자, URL을 추가·삭제·변경하지 "
        "마세요. 자격, 학력, 학위, 경력연수, 법률·법적 의무, KPI, 권한, 필수조건 또는 새로운 사실을 "
        "추론하거나 추가하지 마세요. 원문의 의미와 강도를 유지하세요. 설명이나 마크다운 없이 제공된 JSON "
        "스키마만 반환하세요.\nINPUT="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _review_prompt(proposal: RewriteSuggestionResult) -> str:
    payload = [
        {
            "field_locator": item.field_locator,
            "original": item.original,
            "candidate": item.suggestion,
        }
        for item in proposal.suggestions
    ]
    return (
        "당신은 한국어 문장 재서술의 동료 검토자입니다. 각 candidate가 original의 의미, 강도, 사실을 "
        "그대로 보존하면 accept/meaning_preserved, 아니면 reject/meaning_not_preserved를 선택하세요. "
        "문장을 수정하거나 대안을 제안하지 마세요. 자격, 학력, 학위, 경력연수, 법률·법적 의무, KPI, "
        "권한, 필수조건, 숫자, URL, 식별자가 추가·삭제·변경되면 reject입니다. 설명, 계정 정보, 마크다운 "
        "없이 제공된 JSON 스키마만 반환하세요.\nINPUT="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _decode_provider_payload(
    provider: LlmProvider,
    stdout: str,
    *,
    expected_key: str = "suggestions",
) -> dict[str, Any]:
    payload = json.loads(stdout)
    if not isinstance(payload, dict):
        raise TypeError("provider result must be an object")
    if expected_key in payload:
        return payload
    if provider == LlmProvider.CLAUDE:
        structured = payload.get("structured_output")
        if isinstance(structured, dict) and expected_key in structured:
            return structured
        result = payload.get("result")
        if isinstance(result, str):
            nested = json.loads(result)
            if isinstance(nested, dict) and expected_key in nested:
                return nested
    raise KeyError(expected_key)


def _validate_suggestions(
    payload: Mapping[str, Any],
    originals: Mapping[str, str],
    provider: LlmProvider,
) -> tuple[RewriteSuggestion, ...]:
    if set(payload) != {"suggestions"} or not isinstance(payload["suggestions"], list):
        raise ValueError("unexpected rewrite envelope")
    by_locator: dict[str, str] = {}
    for item in payload["suggestions"]:
        if not isinstance(item, dict) or set(item) != {"field_locator", "suggestion"}:
            raise ValueError("unexpected rewrite item")
        locator, suggestion = item["field_locator"], item["suggestion"]
        if not isinstance(locator, str) or locator not in originals or locator in by_locator:
            raise ValueError("unknown or duplicate field locator")
        if not isinstance(suggestion, str) or not suggestion.strip() or len(suggestion) > 4000:
            raise ValueError("invalid suggestion text")
        original = originals[locator]
        suggestion = suggestion.strip()
        _validate_text_change(original, suggestion)
        by_locator[locator] = suggestion
    if set(by_locator) != set(originals):
        raise ValueError("rewrite response omitted an eligible field")
    return tuple(
        RewriteSuggestion(locator, original, by_locator[locator], provider)
        for locator, original in originals.items()
    )


def _validate_review_decisions(
    payload: Mapping[str, Any],
    locators: tuple[str, ...],
) -> tuple[PeerReviewDecision, ...]:
    if set(payload) != {"reviews"} or not isinstance(payload["reviews"], list):
        raise ValueError("unexpected review envelope")
    by_locator: dict[str, PeerReviewDecision] = {}
    expected = set(locators)
    for item in payload["reviews"]:
        if not isinstance(item, dict) or set(item) != {"field_locator", "verdict", "code"}:
            raise ValueError("unexpected review item")
        locator, verdict, code = item["field_locator"], item["verdict"], item["code"]
        if not isinstance(locator, str) or locator not in expected or locator in by_locator:
            raise ValueError("unknown or duplicate review locator")
        if verdict not in {"accept", "reject"} or code not in {
            "meaning_preserved",
            "meaning_not_preserved",
        }:
            raise ValueError("unknown review decision")
        if (verdict == "accept") != (code == "meaning_preserved"):
            raise ValueError("inconsistent review decision")
        by_locator[locator] = PeerReviewDecision(locator, verdict, code)
    if set(by_locator) != expected:
        raise ValueError("review response omitted an eligible field")
    return tuple(by_locator[locator] for locator in locators)


def _is_allowed_field_locator(locator: str) -> bool:
    return locator == "job_description.job_purpose.text" or bool(
        re.fullmatch(
            r"job_description\.(?:duties\[id=[^\]\r\n]+\]\.summary|"
            r"tasks\[id=[^\]\r\n]+\]\.description)",
            locator,
        )
    )


def _validate_text_change(original: str, suggestion: str) -> None:
    if not _counters_equal(_NUMBER_RE.findall(suggestion), _NUMBER_RE.findall(original)):
        raise ValueError("suggestion changed a number")
    if not _counters_equal(_URL_RE.findall(suggestion), _URL_RE.findall(original)):
        raise ValueError("suggestion changed a URL")
    if not _counters_equal(_IDENTIFIER_RE.findall(suggestion), _IDENTIFIER_RE.findall(original)):
        raise ValueError("suggestion changed an identifier")
    original_folded = original.casefold()
    suggestion_folded = suggestion.casefold()
    for term in _SENSITIVE_ADDITION_TERMS:
        normalized_term = term.casefold()
        if suggestion_folded.count(normalized_term) != original_folded.count(normalized_term):
            raise ValueError("suggestion changed a protected requirement concept")


def _counters_equal(candidate: Sequence[str], original: Sequence[str]) -> bool:
    candidate_counts = Counter(value.casefold() for value in candidate)
    original_counts = Counter(value.casefold() for value in original)
    return candidate_counts == original_counts


def _classify_cli_failure(text: str) -> LlmCliError:
    folded = text.casefold()
    if any(marker in folded for marker in _USAGE_FAILURE_MARKERS) or all(
        marker in folded for marker in ("limit", "session", "reset")
    ):
        return LlmCliError("llm_usage_exhausted", retryable=True)
    if any(marker in folded for marker in _AUTH_FAILURE_MARKERS):
        return LlmCliError("llm_login_required", retryable=True)
    return LlmCliError("rewrite_provider_failed", retryable=True)


def _resolve_windows_node_shim(
    shim: Path,
    resolver: Callable[[str], str | None],
) -> list[str] | None:
    """Resolve a trusted npm shim to a native or Node package entry point.

    Windows ``CreateProcess`` does not execute ``.cmd`` with ``shell=False``.
    Calling ``cmd /c`` would make schema arguments containing profile IDs part
    of command-language parsing.  Instead, inspect the fixed npm shim only to
    locate its package entry point, validate that it remains beneath the shim
    directory, and invoke it with an ordinary argument array.  Newer Claude
    shims point directly at a packaged ``claude.exe``; JS/CJS/MJS entries are
    invoked through Node.
    """

    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r'(?i)["\']([^"\']+\.(?:exe|js|cjs|mjs))["\']', text)
    candidates: list[Path] = []
    for match in matches:
        normalized = match.replace("%dp0%", str(shim.parent) + os.sep).replace(
            "%~dp0", str(shim.parent) + os.sep
        )
        candidate = Path(normalized)
        if not candidate.is_absolute():
            candidate = shim.parent / candidate
        try:
            candidate = candidate.resolve(strict=True)
            candidate.relative_to(shim.parent.resolve())
        except (OSError, ValueError):
            continue
        candidates.append(candidate)
    if not candidates:
        return None
    script_entries = [
        candidate for candidate in candidates if candidate.suffix.casefold() in {".js", ".cjs", ".mjs"}
    ]
    entry = script_entries[0] if script_entries else candidates[0]
    if entry.suffix.casefold() == ".exe":
        return [str(entry)]
    sibling_node = shim.parent / "node.exe"
    node = str(sibling_node) if sibling_node.is_file() else resolver("node")
    return [node, str(entry)] if node else None


class CliScopeSelector:
    """Bind one CLI provider to the planner's scope-selection port."""

    def __init__(self, adapter: CliLlmAdapter, provider: LlmProvider) -> None:
        if provider == LlmProvider.OFF:
            raise ValueError("scope selection requires an installed CLI provider")
        self._adapter = adapter
        self._provider = provider

    @property
    def provider(self) -> LlmProvider:
        return self._provider

    def select_scope(self, request: ScopeSelectionRequest) -> ScopeSelectionResult:
        return self._adapter.select_ncs_scope(request, self._provider)


__all__ = ["CliLlmAdapter", "CliScopeSelector"]
