from __future__ import annotations

import json
from pathlib import Path

from ncs_jd.application.llm_orchestrator import (
    PeerReviewDecision,
    PeerReviewRequest,
    PeerReviewResult,
)
from ncs_jd.application.llm_rewriter import (
    LlmProvider,
    ProviderStatus,
    RewriteSuggestion,
    RewriteSuggestionResult,
)
from ncs_jd.domain.job_profile import JobProfile
from ncs_jd.team_cli import run


FIXTURE = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


class ReadyTeam:
    def list_provider_statuses(self):
        return tuple(
            ProviderStatus(provider, True, True, code="authenticated")
            for provider in (LlmProvider.CODEX, LlmProvider.CLAUDE)
        )

    def suggest_rewrites(self, profile: JobProfile, provider: LlmProvider):
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
        return RewriteSuggestionResult(
            provider,
            "suggestions_ready",
            "suggestions_ready",
            tuple(
                RewriteSuggestion(locator, original, original + " 정리", provider)
                for locator, original in fields.items()
            ),
        )

    def review_rewrite_proposal(self, request: PeerReviewRequest):
        return PeerReviewResult(
            request.reviewer,
            request.candidate_provider,
            "review_ready",
            "review_ready",
            tuple(
                PeerReviewDecision(item.field_locator, "accept", "meaning_preserved")
                for item in request.proposal.suggestions
            ),
        )


def test_status_is_nonzero_until_both_providers_are_authenticated(capsys) -> None:
    class NotReady(ReadyTeam):
        def list_provider_statuses(self):
            return (
                ProviderStatus(LlmProvider.CODEX, True, True, code="authenticated"),
                ProviderStatus(LlmProvider.CLAUDE, True, False, code="not_authenticated"),
            )

    assert run(["status"], llm_cli=NotReady()) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "team_login_required"


def test_deliberate_writes_hash_only_report_and_validated_profile(tmp_path, capsys) -> None:
    report = tmp_path / "audit.json"
    output = tmp_path / "profile.json"

    assert run(
        [
            "deliberate",
            str(FIXTURE),
            "--report",
            str(report),
            "--output-profile",
            str(output),
        ],
        llm_cli=ReadyTeam(),
    ) == 0

    result = json.loads(report.read_text(encoding="utf-8"))
    rendered = JobProfile.model_validate_json(output.read_text(encoding="utf-8"))
    assert result["code"] == "team_rewrite_resolved"
    assert set(result["audit"]) == {
        "packet_hash",
        "proposal_hashes",
        "review_hashes",
        "events",
    }
    assert rendered.job_description.job_purpose.text.endswith(" 정리")
    assert json.loads(capsys.readouterr().out)["rounds"] == 2
