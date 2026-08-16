from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from ncs_jd.adapters.cli_llm import (
    CliLlmAdapter,
    _resolve_windows_node_shim,
    _validate_text_change,
)
from ncs_jd.application.llm_rewriter import (
    apply_rewrite_suggestions,
    LlmCliError,
    LlmProvider,
    LlmRewriteApplicationError,
    RewriteSuggestion,
    RewriteSuggestionResult,
)
from ncs_jd.application.llm_orchestrator import PeerReviewRequest
from ncs_jd.domain.job_profile import JobProfile


FIXTURE = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


def _profile() -> JobProfile:
    return JobProfile.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _resolver(name: str) -> str | None:
    return {"codex": "C:/tools/codex.exe", "claude": "C:/tools/claude.exe"}.get(name)


def test_status_is_sanitized_and_discards_claude_identity_fields() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "claude.exe" in args[0]:
            stdout = json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "pro",
                    "email": "private@example.test",
                    "orgId": "secret-org-id",
                    "orgName": "Private Org",
                    "token": "never-return-this",
                }
            )
        else:
            stdout = "Logged in using ChatGPT"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    statuses = CliLlmAdapter(executable_resolver=_resolver, runner=runner).list_provider_statuses()
    payload = {item.provider: item.as_dict() for item in statuses}

    assert payload[LlmProvider.OFF] == {
        "provider": "off",
        "installed": True,
        "logged_in": False,
        "login_in_progress": False,
        "code": "llm_off",
    }
    assert payload[LlmProvider.CODEX]["logged_in"] is True
    assert payload[LlmProvider.CLAUDE]["logged_in"] is True
    serialized = json.dumps(list(payload.values()))
    assert "private@example.test" not in serialized
    assert "secret-org-id" not in serialized
    assert calls[0][1:] == ["login", "status"]
    assert calls[1][1:] == ["auth", "status", "--json"]


@pytest.mark.parametrize(
    ("provider", "stdout"),
    [
        (LlmProvider.CODEX, "Logged in using API key"),
        (LlmProvider.CODEX, "Authenticated with access token"),
        (
            LlmProvider.CLAUDE,
            json.dumps({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}),
        ),
        (
            LlmProvider.CLAUDE,
            json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "thirdParty"}),
        ),
    ],
)
def test_status_rejects_non_subscription_auth_without_exposing_method(
    provider: LlmProvider, stdout: str
) -> None:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout, "")

    status = CliLlmAdapter(executable_resolver=_resolver, runner=runner).provider_status(provider)

    assert status.logged_in is False
    assert status.code == "unsupported_auth_method"
    serialized = json.dumps(status.as_dict())
    assert "api_key" not in serialized
    assert "thirdParty" not in serialized
    assert "access token" not in serialized


def test_login_uses_fixed_argument_array_new_console_and_rejects_duplicate() -> None:
    launched: list[tuple[list[str], dict[str, Any]]] = []

    class Process:
        def poll(self) -> None:
            return None

    def launcher(args: list[str], **kwargs: Any) -> Process:
        launched.append((args, kwargs))
        return Process()

    adapter = CliLlmAdapter(
        executable_resolver=_resolver,
        launcher=launcher,
        environ={"PATH": "C:/tools", "OPENAI_API_KEY": "must-not-be-forwarded"},
    )
    result = adapter.start_login(LlmProvider.CODEX)

    assert result.as_dict() == {"provider": "codex", "started": True, "code": "login_started"}
    assert launched[0][0] == ["C:/tools/codex.exe", "login", "--device-auth"]
    assert launched[0][1]["shell"] is False
    assert isinstance(launched[0][1]["cwd"], str)
    assert "OPENAI_API_KEY" not in launched[0][1]["env"]
    with pytest.raises(LlmCliError, match="login_already_running"):
        adapter.start_login(LlmProvider.CODEX)


def test_windows_npm_shim_resolves_to_node_argument_array(tmp_path: Path) -> None:
    package_bin = tmp_path / "node_modules" / "@openai" / "codex" / "bin"
    package_bin.mkdir(parents=True)
    entry = package_bin / "codex.js"
    entry.write_text("", encoding="utf-8")
    shim = tmp_path / "codex.cmd"
    shim.write_text(
        '"%~dp0\\node.exe" "%~dp0\\node_modules\\@openai\\codex\\bin\\codex.js" %*',
        encoding="utf-8",
    )
    node = tmp_path / "node.exe"
    node.write_bytes(b"")

    command = _resolve_windows_node_shim(shim, lambda name: None)

    assert command == [str(node), str(entry)]
    assert all("cmd.exe" not in item.casefold() for item in command)


def test_windows_claude_shim_resolves_to_packaged_executable(tmp_path: Path) -> None:
    package_bin = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
    package_bin.mkdir(parents=True)
    entry = package_bin / "claude.exe"
    entry.write_bytes(b"")
    shim = tmp_path / "claude.cmd"
    shim.write_text(
        '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe" %*',
        encoding="utf-8",
    )

    command = _resolve_windows_node_shim(shim, lambda name: None)

    assert command == [str(entry)]


def test_windows_shim_rejects_entry_outside_its_directory(tmp_path: Path) -> None:
    shim_root = tmp_path / "npm"
    shim_root.mkdir()
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"")
    shim = shim_root / "unsafe.cmd"
    shim.write_text('"%~dp0\\..\\outside.exe" %*', encoding="utf-8")

    assert _resolve_windows_node_shim(shim, lambda name: None) is None


def test_codex_rewrite_returns_separate_suggestions_and_keeps_profile_unchanged() -> None:
    captured: dict[str, Any] = {}

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(args=args, kwargs=kwargs)
        captured["cwd_contents"] = list(Path(kwargs["cwd"]).iterdir())
        input_payload = json.loads(kwargs["input"].split("INPUT=", 1)[1])
        output = {
            "suggestions": [
                {
                    "field_locator": item["field_locator"],
                    "suggestion": item["original"] + " 정돈",
                }
                for item in input_payload
            ]
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(output, ensure_ascii=False), "")

    profile = _profile()
    before = profile.model_dump_json(by_alias=True)
    result = CliLlmAdapter(executable_resolver=_resolver, runner=runner).suggest_rewrites(
        profile, LlmProvider.CODEX
    )

    assert result.status == "suggestions_ready"
    assert result.suggestions
    assert len(result.suggestions) == 1 + len(profile.job_description.duties)
    assert all("tasks[" not in item.field_locator for item in result.suggestions)
    assert all(item.provider == LlmProvider.CODEX for item in result.suggestions)
    assert profile.model_dump_json(by_alias=True) == before
    args = captured["args"]
    assert args[:2] == ["C:/tools/codex.exe", "exec"]
    for flag in (
        "--ephemeral",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--sandbox",
        "--output-schema",
    ):
        assert flag in args
    assert args[-1] == "-"
    assert captured["kwargs"]["shell"] is False
    assert captured["cwd_contents"] == []


def test_claude_rewrite_uses_safe_nonpersistent_flags_and_wrapped_json() -> None:
    captured: dict[str, Any] = {}

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        items = json.loads(kwargs["input"].split("INPUT=", 1)[1])
        structured = {
            "suggestions": [
                {"field_locator": item["field_locator"], "suggestion": item["original"]}
                for item in items
            ]
        }
        wrapped = {"result": json.dumps(structured, ensure_ascii=False), "email": "hidden@test"}
        return subprocess.CompletedProcess(args, 0, json.dumps(wrapped, ensure_ascii=False), "")

    result = CliLlmAdapter(executable_resolver=_resolver, runner=runner).suggest_rewrites(
        _profile(), LlmProvider.CLAUDE
    )

    assert result.code == "suggestions_ready"
    args = captured["args"]
    assert args[:2] == ["C:/tools/claude.exe", "-p"]
    assert "--no-session-persistence" in args
    assert "--safe-mode" in args
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "--json-schema" in args


def test_rewrite_rejects_new_number_and_normalizes_usage_failure() -> None:
    profile = _profile()

    def unsafe_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        items = json.loads(kwargs["input"].split("INPUT=", 1)[1])
        output = {
            "suggestions": [
                {"field_locator": item["field_locator"], "suggestion": item["original"] + " 999년"}
                for item in items
            ]
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(output, ensure_ascii=False), "")

    with pytest.raises(LlmCliError, match="invalid_rewrite_response"):
        CliLlmAdapter(executable_resolver=_resolver, runner=unsafe_runner).suggest_rewrites(
            profile, LlmProvider.CODEX
        )

    def exhausted(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "", "usage limit reached for private account")

    with pytest.raises(LlmCliError, match="llm_usage_exhausted"):
        CliLlmAdapter(executable_resolver=_resolver, runner=exhausted).suggest_rewrites(
            profile, LlmProvider.CODEX
        )


@pytest.mark.parametrize("reviewer", [LlmProvider.CODEX, LlmProvider.CLAUDE])
def test_peer_review_uses_safe_strict_json_call_and_returns_normalized_decisions(
    reviewer: LlmProvider,
) -> None:
    profile = _profile()
    candidate = LlmProvider.CLAUDE if reviewer == LlmProvider.CODEX else LlmProvider.CODEX
    proposal = RewriteSuggestionResult(
        candidate,
        "suggestions_ready",
        "suggestions_ready",
        (
            RewriteSuggestion(
                "job_description.job_purpose.text",
                profile.job_description.job_purpose.text,
                profile.job_description.job_purpose.text + " 정돈",
                candidate,
            ),
        ),
    )
    captured: dict[str, Any] = {}

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(args=args, kwargs=kwargs)
        items = json.loads(kwargs["input"].split("INPUT=", 1)[1])
        output = {
            "reviews": [
                {
                    "field_locator": item["field_locator"],
                    "verdict": "accept",
                    "code": "meaning_preserved",
                }
                for item in items
            ]
        }
        stdout = json.dumps(output, ensure_ascii=False)
        if reviewer == LlmProvider.CLAUDE:
            stdout = json.dumps(
                {"result": stdout, "email": "private@example.test"},
                ensure_ascii=False,
            )
        return subprocess.CompletedProcess(args, 0, stdout, "")

    result = CliLlmAdapter(executable_resolver=_resolver, runner=runner).review_rewrite_proposal(
        PeerReviewRequest(reviewer, proposal)
    )

    assert result.status == "review_ready"
    assert result.code == "review_ready"
    assert result.reviewer == reviewer
    assert result.candidate_provider == candidate
    assert result.decisions[0].as_dict() == {
        "field_locator": "job_description.job_purpose.text",
        "verdict": "accept",
        "code": "meaning_preserved",
    }
    serialized = json.dumps(result.as_dict())
    assert "private@example.test" not in serialized
    args = captured["args"]
    assert captured["kwargs"]["shell"] is False
    assert Path(captured["kwargs"]["cwd"]).name == "work"
    if reviewer == LlmProvider.CODEX:
        assert args[:2] == ["C:/tools/codex.exe", "exec"]
        assert "--ephemeral" in args
        assert "--ignore-user-config" in args
        assert "--output-schema" in args
    else:
        assert args[:2] == ["C:/tools/claude.exe", "-p"]
        assert "--no-session-persistence" in args
        assert "--safe-mode" in args
        assert args[args.index("--tools") + 1] == ""
        assert "--json-schema" in args


def test_peer_review_rejects_explanations_and_does_not_expose_raw_output() -> None:
    profile = _profile()
    proposal = RewriteSuggestionResult(
        LlmProvider.CLAUDE,
        "suggestions_ready",
        "suggestions_ready",
        (
            RewriteSuggestion(
                "job_description.job_purpose.text",
                profile.job_description.job_purpose.text,
                profile.job_description.job_purpose.text + " 정돈",
                LlmProvider.CLAUDE,
            ),
        ),
    )

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = {
            "reviews": [
                {
                    "field_locator": "job_description.job_purpose.text",
                    "verdict": "accept",
                    "code": "meaning_preserved",
                    "rationale": "private@example.test raw identity",
                }
            ]
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(output), "")

    with pytest.raises(LlmCliError) as raised:
        CliLlmAdapter(executable_resolver=_resolver, runner=runner).review_rewrite_proposal(
            PeerReviewRequest(LlmProvider.CODEX, proposal)
        )

    assert raised.value.code == "invalid_review_response"
    assert str(raised.value) == "invalid_review_response"
    assert "private@example.test" not in str(raised.value)


def test_claude_session_reset_error_is_normalized_without_exposing_raw_result() -> None:
    private_result = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "Session limit reached for private@example.test; reset in 2 hours.",
    }

    def exhausted(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, json.dumps(private_result), "")

    with pytest.raises(LlmCliError) as raised:
        CliLlmAdapter(executable_resolver=_resolver, runner=exhausted).suggest_rewrites(
            _profile(), LlmProvider.CLAUDE
        )

    assert raised.value.code == "llm_usage_exhausted"
    assert str(raised.value) == "llm_usage_exhausted"
    assert "private@example.test" not in str(raised.value)


@pytest.mark.parametrize(
    ("original", "suggestion"),
    [
        ("경력 3년 기준", "경력 기준"),
        ("참조 https://example.test/a", "참조"),
        ("task-123 항목", "항목"),
        ("관련 자격 검토", "관련 검토"),
    ],
)
def test_rewrite_rejects_deleting_protected_facts(original: str, suggestion: str) -> None:
    with pytest.raises(ValueError, match="changed"):
        _validate_text_change(original, suggestion)


def test_off_never_invokes_a_process() -> None:
    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("OFF must not invoke a provider")

    result = CliLlmAdapter(executable_resolver=_resolver, runner=runner).suggest_rewrites(
        _profile(), LlmProvider.OFF
    )
    assert result.as_dict() == {
        "provider": "off",
        "status": "not_applied",
        "code": "llm_off",
        "applied": False,
        "suggestions": [],
    }


def test_validated_rewrites_apply_only_to_allow_listed_prose() -> None:
    profile = _profile()
    fields = {
        "job_description.job_purpose.text": profile.job_description.job_purpose.text,
        **{
            f"job_description.duties[id={item.id}].summary": item.summary
            for item in profile.job_description.duties
        },
        **{
            f"job_description.tasks[id={item.id}].description": item.description
            for item in profile.job_description.tasks
        },
    }
    result = RewriteSuggestionResult(
        LlmProvider.CODEX,
        "suggestions_ready",
        "suggestions_ready",
        tuple(
            RewriteSuggestion(locator, original, f"{original} 수행", LlmProvider.CODEX)
            for locator, original in fields.items()
        ),
    )

    updated = apply_rewrite_suggestions(profile, result)

    assert updated.job_description.job_purpose.text.endswith(" 수행")
    assert updated.job_description.duties[0].source_refs == profile.job_description.duties[0].source_refs
    assert updated.job_description.tasks[0].id == profile.job_description.tasks[0].id
    assert updated.references == profile.references
    assert "llm_rewrite_not_applied" not in {flag.code.value for flag in updated.review_flags}


def test_validated_rewrites_may_leave_task_descriptions_unchanged() -> None:
    profile = _profile()
    fields = {
        "job_description.job_purpose.text": profile.job_description.job_purpose.text,
        **{
            f"job_description.duties[id={item.id}].summary": item.summary
            for item in profile.job_description.duties
        },
    }
    result = RewriteSuggestionResult(
        LlmProvider.CODEX,
        "suggestions_ready",
        "suggestions_ready",
        tuple(
            RewriteSuggestion(locator, original, f"{original} 수행", LlmProvider.CODEX)
            for locator, original in fields.items()
        ),
    )

    updated = apply_rewrite_suggestions(profile, result)

    assert updated.job_description.job_purpose.text.endswith(" 수행")
    assert updated.job_description.tasks == profile.job_description.tasks


def test_validated_rewrites_may_leave_duties_unchanged() -> None:
    profile = _profile()
    locator = "job_description.job_purpose.text"
    result = RewriteSuggestionResult(
        LlmProvider.CODEX,
        "suggestions_ready",
        "suggestions_ready",
        (
            RewriteSuggestion(
                locator,
                profile.job_description.job_purpose.text,
                profile.job_description.job_purpose.text + " 수행",
                LlmProvider.CODEX,
            ),
        ),
    )

    updated = apply_rewrite_suggestions(profile, result)

    assert updated.job_description.job_purpose.text.endswith(" 수행")
    assert updated.job_description.duties == profile.job_description.duties


def test_rewrite_application_rejects_changed_protected_facts() -> None:
    profile = _profile()
    result = RewriteSuggestionResult(
        LlmProvider.CODEX,
        "suggestions_ready",
        "suggestions_ready",
        (
            RewriteSuggestion(
                "job_description.job_purpose.text",
                profile.job_description.job_purpose.text,
                profile.job_description.job_purpose.text + " 경력 3년",
                LlmProvider.CODEX,
            ),
        ),
    )
    with pytest.raises(LlmRewriteApplicationError):
        apply_rewrite_suggestions(profile, result)
