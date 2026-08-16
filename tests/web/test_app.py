from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from ncs_jd.web.app import (
    DEFAULT_HOST,
    KordocReadiness,
    _check_kordoc_readiness,
    create_app,
    create_connected_app,
)


def _client(*, connected: bool = False, readiness: KordocReadiness | None = None) -> TestClient:
    if not connected:
        return TestClient(create_app())
    dependency = object()
    return TestClient(
        create_app(
            workflow=dependency,  # type: ignore[arg-type]
            ncs_source=dependency,  # type: ignore[arg-type]
            renderer=dependency,  # type: ignore[arg-type]
            kordoc_readiness=readiness,
        )
    )


def test_workspace_is_a_single_automatic_generator() -> None:
    html = _client(connected=True).get("/").text

    assert "공고문을 올리면" in html
    assert "NCS 직무기술서가 나옵니다" in html
    assert html.count('class="upload-card') == 2
    assert 'id="announcement-file"' in html
    assert 'id="template-file"' in html
    assert "NCS 직무기술서 만들기" in html
    assert "실행 방식 선택" in html
    assert "로컬 전용" in html
    # No AI engine is wired here, so the AI branch of the choice never appears
    # and neither does any login affordance.
    assert "AI 정밀 탐색" not in html
    assert "Codex" not in html and "Claude Code" not in html
    assert "data-login-provider" not in html
    assert "data-engine-picker" not in html
    assert "/api/llm/" not in html
    for removed in (
        "추출값 검토",
        "NCS 범위 선택",
        "능력단위 포함·제외",
        "조직의 KPI",
        "AI 문장 다듬기",
        "JSON 내보내기",
    ):
        assert removed not in html


def test_the_app_never_asks_a_person_for_a_key_or_token() -> None:
    """Every AI path authenticates through an official CLI's own login store."""

    client = _client(connected=True)
    html = client.get("/").text
    script = client.get("/static/app.js").text

    assert 'type="password"' not in html
    assert "api-key" not in html and "api_key" not in html
    assert "OpenAI" not in html
    assert "api_key" not in script and "apiKey" not in script


def _cli_client() -> TestClient:
    dependency = object()
    return TestClient(
        create_app(
            workflow=dependency,  # type: ignore[arg-type]
            ncs_source=dependency,  # type: ignore[arg-type]
            renderer=dependency,  # type: ignore[arg-type]
            scope_selectors={"claude": dependency, "codex": dependency},  # type: ignore[dict-item]
            agent_runners={"claude": dependency, "codex": dependency},  # type: ignore[dict-item]
        )
    )


def test_run_mode_is_chosen_above_the_engine_and_login() -> None:
    """The local-vs-AI decision comes first; picking an engine is downstream."""

    client = _cli_client()
    html = client.get("/").text
    login_script = client.get("/static/provider-login.js").text

    assert html.index('name="run_mode"') < html.index("data-engine-picker")
    assert html.index("data-engine-picker") < html.index('data-provider-card="claude"')
    assert html.index('data-provider-card="claude"') < html.index('data-provider-card="codex"')
    # The whole decision comes before the announcement upload, and the engines
    # stay on screen throughout -- inert, not hidden, so the feature does not
    # look deleted while the local path is selected.
    assert html.index("data-engine-picker") < html.index('id="announcement-file"')
    assert 'class="engine-picker is-idle"' in html
    assert "data-engine-picker data-cli-login hidden" not in html
    assert 'data-login-provider="claude"' in html
    assert 'data-login-provider="codex"' in html
    assert "provider-login.js" in html
    assert 'fetch("/api/llm/providers"' in login_script
    assert '"X-NCS-JD-Local-Action": "login"' in login_script


def test_engine_is_a_deliberate_choice_not_an_auto_detected_state() -> None:
    client = _cli_client()
    html = client.get("/").text
    login_script = client.get("/static/provider-login.js").text

    # Each engine card carries its own selection control, disabled until that
    # provider reports a working subscription login.
    assert html.count('name="engine"') == 2
    assert 'value="claude" disabled' in html and 'value="codex" disabled' in html
    # Status is fetched when the AI path is chosen, never on page load, so an
    # engine cannot read as connected before anyone picked it.
    assert 'data-state="idle">대기' in html
    assert "ncs-jd:engine-picker-shown" in login_script
    assert "if (!everChecked) void refreshStatuses();" in login_script


def test_production_connected_app_exposes_cli_login_ui_and_routes() -> None:
    client = TestClient(create_connected_app(kordoc_readiness=KordocReadiness(True)))
    html = client.get("/").text

    assert html.index('name="run_mode"') < html.index("data-engine-picker")
    assert 'data-login-provider="claude"' in html
    assert 'data-login-provider="codex"' in html
    assert client.get("/api/llm/providers").status_code == 200


def test_input_contract_supports_file_or_pasted_announcement_and_optional_template() -> None:
    html = _client().get("/").text

    assert 'accept=".pdf,.hwp,.hwpx,.docx,.txt"' in html
    assert 'accept=".pdf,.hwp,.hwpx"' in html
    assert "기관 양식 PDF · HWP · HWPX" in html
    assert "없으면 표준 양식 사용" in html
    assert 'id="announcement-text"' in html
    assert 'id="job-title"' in html


def test_reviewed_draft_can_be_saved_printed_and_reopened() -> None:
    client = _client(connected=True)
    html = client.get("/").text
    script = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert "data-agent-save" in html
    assert "data-agent-print" in html
    assert "data-agent-restore" in html
    assert 'accept=".json,application/json"' in html

    # An agent run costs minutes, so the reviewed draft has to survive a closed window.
    assert '"ncs_jd.agent_draft/v1"' in script
    assert "window.print()" in script
    assert "collectAgentFields()" in script

    # Textareas clip on paper and the draft-only disclaimer has to stay printed.
    assert "@media print" in styles
    assert ".agent-print-value { display: block; }" in styles
    assert ".notice { display: none" not in styles


def test_saved_draft_stays_local_and_keeps_no_server_state() -> None:
    client = _client(connected=True)
    script = client.get("/static/app.js").text

    assert "localStorage" not in script
    assert "sessionStorage" not in script
    # Reopening a draft must not need the announcement or another agent run.
    assert 'fetch("/api/agent-draft/export/hwpx"' in script
    for endpoint in ('fetch("/api/drafts/', "/api/drafts\""):
        assert endpoint not in script


def test_simple_script_calls_only_the_automatic_local_pipeline() -> None:
    script = _client().get("/static/app.js").text

    assert 'fetch("/api/generate-job-description"' in script
    assert 'fetch("/api/llm/providers"' not in script
    assert "X-NCS-JD-Local-Action" not in script
    assert 'body.append("announcement"' in script
    assert 'body.append("announcement_text"' in script
    assert 'body.append("template"' in script
    assert 'body.append("provider"' in script
    assert "localStorage" not in script
    for removed in ("confirm-extraction", "confirm-scope", "/api/ncs/search", "/api/drafts\""):
        assert removed not in script


def test_default_app_does_not_mount_llm_login_routes() -> None:
    client = _client(connected=True)

    assert client.get("/api/llm/providers").status_code == 404
    assert client.post("/api/llm/providers/codex/login").status_code == 404


def test_disconnected_generator_is_disabled_and_health_is_safe() -> None:
    client = _client()
    html = client.get("/").text
    assert '<button class="generate-button" type="submit" disabled>' in html
    health = client.get("/health").json()
    assert health == {
        "status": "ok",
        "service": "ncs-jd-web",
        "bind_host": DEFAULT_HOST,
        "backend_connected": False,
    }


def test_ui_contract_matches_the_small_surface() -> None:
    payload = _client().get("/api/ui-contract").json()

    assert payload["version"] == "simple-generator-v2"
    assert [item["id"] for item in payload["inputs"]] == [
        "announcement",
        "announcement_text",
        "job_title",
        "template",
    ]
    assert payload["generation_mode"] == "deterministic_with_optional_template_mapping"
    assert payload["external_ai_required"] is False
    # Contract-level proof that no input is a credential.
    assert payload["secret_inputs"] == []
    assert payload["providers"] == ["off"]
    assert payload["output"] == "hwpx"
    assert "steps" not in payload
    assert "example_scenario" not in payload


def test_failed_kordoc_readiness_disables_connected_routes() -> None:
    client = _client(connected=True, readiness=KordocReadiness(False, "node_unavailable"))
    assert client.get("/health").status_code == 503
    assert client.get("/health").json()["backend_readiness"] == "node_unavailable"
    assert client.post("/api/generate-job-description").status_code == 404


@pytest.mark.parametrize(
    ("return_code", "expected"),
    [(0, KordocReadiness(True)), (2, KordocReadiness(False, "kordoc_version_mismatch")), (3, KordocReadiness(False, "kordoc_capability_missing"))],
)
def test_kordoc_self_check_codes(return_code: int, expected: KordocReadiness) -> None:
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], return_code, "", "")

    assert _check_kordoc_readiness(runner=runner) == expected


def test_factory_rejects_partially_injected_backends() -> None:
    with pytest.raises(ValueError):
        create_app(workflow=object())  # type: ignore[arg-type]
