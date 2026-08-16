from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ncs_jd.application.llm_rewriter import (
    LlmCliError,
    LlmProvider,
    LoginStartResult,
    ProviderStatus,
    RewriteSuggestionResult,
)
from ncs_jd.domain.job_profile import JobProfile
from ncs_jd.web.api import create_llm_api_router


FIXTURE = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


class FakeLlmCli:
    failure: str | None = None

    def list_provider_statuses(self):
        return (
            ProviderStatus(LlmProvider.OFF, True, False, code="llm_off"),
            ProviderStatus(LlmProvider.CODEX, True, True, code="authenticated"),
            ProviderStatus(LlmProvider.CLAUDE, False, False, code="not_installed"),
        )

    def start_login(self, provider: LlmProvider):
        if self.failure:
            raise LlmCliError(self.failure)
        return LoginStartResult(provider, True, "login_started")

    def suggest_rewrites(self, profile: JobProfile, provider: LlmProvider):
        if self.failure:
            raise LlmCliError(self.failure)
        return RewriteSuggestionResult(provider, "not_applied", "llm_off")


def _client(fake: FakeLlmCli) -> TestClient:
    app = FastAPI()
    app.include_router(create_llm_api_router(llm_cli=fake))
    return TestClient(app)


def test_provider_list_and_login_contract() -> None:
    fake = FakeLlmCli()
    client = _client(fake)

    response = client.get("/api/llm/providers")
    assert response.status_code == 200
    assert response.json()["default_provider"] == "off"
    assert response.json()["providers"][1] == {
        "provider": "codex",
        "installed": True,
        "logged_in": True,
        "login_in_progress": False,
        "code": "authenticated",
    }
    response = client.post(
        "/api/llm/providers/codex/login",
        headers={"X-NCS-JD-Local-Action": "login"},
    )
    assert response.status_code == 202
    assert response.json() == {"provider": "codex", "started": True, "code": "login_started"}


def test_rewrite_off_and_recoverable_error_contracts() -> None:
    fake = FakeLlmCli()
    client = _client(fake)
    profile = JobProfile.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    response = client.post(
        "/api/llm/rewrite-suggestions",
        headers={"X-NCS-JD-Local-Action": "rewrite"},
        json={"job_profile": profile.model_dump(mode="json", by_alias=True)},
    )
    assert response.status_code == 200
    assert response.json() == {
        "provider": "off",
        "status": "not_applied",
        "code": "llm_off",
        "applied": False,
        "suggestions": [],
    }

    fake.failure = "llm_usage_exhausted"
    response = client.post(
        "/api/llm/rewrite-suggestions",
        headers={"X-NCS-JD-Local-Action": "rewrite"},
        json={"provider": "codex", "job_profile": profile.model_dump(mode="json", by_alias=True)},
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "llm_usage_exhausted"
    assert response.json()["error"]["retryable"] is True
    assert "private" not in response.text


def test_post_actions_require_local_custom_header() -> None:
    client = _client(FakeLlmCli())
    profile = JobProfile.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    assert client.post("/api/llm/providers/codex/login").status_code == 403
    response = client.post(
        "/api/llm/rewrite-suggestions",
        json={"provider": "off", "job_profile": profile.model_dump(mode="json", by_alias=True)},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "local_action_required"
