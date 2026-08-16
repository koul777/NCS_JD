from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

from ncs_jd.application.llm_orchestrator import (
    PeerReviewDecision,
    PeerReviewRequest,
    PeerReviewResult,
    TeamRewriteOrchestrator,
)
from ncs_jd.application.llm_rewriter import (
    LlmCliError,
    LlmProvider,
    RewriteSuggestion,
    RewriteSuggestionResult,
)
from ncs_jd.domain.job_profile import JobProfile


FIXTURE = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


def _profile() -> JobProfile:
    return JobProfile.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _fields(profile: JobProfile) -> dict[str, str]:
    duty = profile.job_description.duties[0]
    return {
        "job_description.job_purpose.text": profile.job_description.job_purpose.text,
        f"job_description.duties[id={duty.id}].summary": duty.summary,
    }


def _proposal(
    profile: JobProfile,
    provider: LlmProvider,
    suffixes: tuple[str, str],
) -> RewriteSuggestionResult:
    return RewriteSuggestionResult(
        provider=provider,
        status="suggestions_ready",
        code="suggestions_ready",
        suggestions=tuple(
            RewriteSuggestion(locator, original, original + suffix, provider)
            for (locator, original), suffix in zip(_fields(profile).items(), suffixes, strict=True)
        ),
    )


class FakeTeamPort:
    def __init__(
        self,
        profile: JobProfile,
        *,
        codex_suffixes: tuple[str, str] = (" 코드", " 코드"),
        claude_suffixes: tuple[str, str] = (" 클로드", " 클로드"),
        review_verdicts: dict[LlmProvider, tuple[str, str]] | None = None,
    ) -> None:
        self.proposals = {
            LlmProvider.CODEX: _proposal(profile, LlmProvider.CODEX, codex_suffixes),
            LlmProvider.CLAUDE: _proposal(profile, LlmProvider.CLAUDE, claude_suffixes),
        }
        self.review_verdicts = review_verdicts or {
            LlmProvider.CODEX: ("accept", "reject"),
            LlmProvider.CLAUDE: ("reject", "accept"),
        }
        self.requests: list[PeerReviewRequest] = []

    def suggest_rewrites(
        self, profile: JobProfile, provider: LlmProvider
    ) -> RewriteSuggestionResult:
        return self.proposals[provider]

    def review_rewrite_proposal(self, request: PeerReviewRequest) -> PeerReviewResult:
        self.requests.append(request)
        verdicts = self.review_verdicts[request.reviewer]
        return PeerReviewResult(
            reviewer=request.reviewer,
            candidate_provider=request.candidate_provider,
            status="review_ready",
            code="review_ready",
            decisions=tuple(
                PeerReviewDecision(
                    item.field_locator,
                    verdict,
                    "meaning_preserved" if verdict == "accept" else "meaning_not_preserved",
                )
                for item, verdict in zip(request.proposal.suggestions, verdicts, strict=True)
            ),
        )


def test_two_round_resolution_selects_only_single_peer_consensus() -> None:
    profile = _profile()
    port = FakeTeamPort(profile)

    result = TeamRewriteOrchestrator(port).deliberate(profile)

    assert result.status == "resolved"
    assert result.code == "team_rewrite_resolved"
    assert result.selected_rewrites is not None
    selected = {item.field_locator: item for item in result.selected_rewrites.suggestions}
    locators = list(_fields(profile))
    # Codex accepts Claude for the first field; Claude accepts Codex for the second.
    assert selected[locators[0]].suggestion.endswith(" 클로드")
    assert selected[locators[1]].suggestion.endswith(" 코드")
    assert {request.reviewer for request in port.requests} == {
        LlmProvider.CODEX,
        LlmProvider.CLAUDE,
    }
    assert all(request.reviewer != request.candidate_provider for request in port.requests)
    assert result.as_dict()["rounds"] == 2


def test_same_proposals_are_consensus_and_ambiguous_disagreement_uses_exact_original() -> None:
    profile = _profile()
    port = FakeTeamPort(
        profile,
        codex_suffixes=(" 동일", " 코드"),
        claude_suffixes=(" 동일", " 클로드"),
        review_verdicts={
            LlmProvider.CODEX: ("reject", "accept"),
            LlmProvider.CLAUDE: ("reject", "accept"),
        },
    )

    result = TeamRewriteOrchestrator(port).deliberate(profile)

    assert result.selected_rewrites is not None
    selected = result.selected_rewrites.suggestions
    assert selected[0].suggestion.endswith(" 동일")
    assert selected[1].suggestion == selected[1].original


def test_all_unresolved_originals_return_no_change() -> None:
    profile = _profile()
    port = FakeTeamPort(
        profile,
        review_verdicts={
            LlmProvider.CODEX: ("reject", "reject"),
            LlmProvider.CLAUDE: ("reject", "reject"),
        },
    )

    result = TeamRewriteOrchestrator(port).deliberate(profile)

    assert result.code == "team_no_changes"
    assert result.selected_rewrites is None
    assert result.as_dict()["selected_rewrites"] is None


def test_proposals_and_reviews_each_run_concurrently() -> None:
    profile = _profile()
    proposal_barrier = threading.Barrier(2, timeout=2)
    review_barrier = threading.Barrier(2, timeout=2)

    class ConcurrentPort(FakeTeamPort):
        def suggest_rewrites(
            self, profile: JobProfile, provider: LlmProvider
        ) -> RewriteSuggestionResult:
            proposal_barrier.wait()
            return super().suggest_rewrites(profile, provider)

        def review_rewrite_proposal(self, request: PeerReviewRequest) -> PeerReviewResult:
            review_barrier.wait()
            return super().review_rewrite_proposal(request)

    result = TeamRewriteOrchestrator(ConcurrentPort(profile)).deliberate(profile)

    assert result.status == "resolved"
    assert proposal_barrier.n_waiting == 0
    assert review_barrier.n_waiting == 0


def test_invalid_proposal_is_rejected_before_peer_review() -> None:
    profile = _profile()
    port = FakeTeamPort(profile)
    unsafe = port.proposals[LlmProvider.CLAUDE]
    first, *rest = unsafe.suggestions
    port.proposals[LlmProvider.CLAUDE] = RewriteSuggestionResult(
        unsafe.provider,
        unsafe.status,
        unsafe.code,
        (
            RewriteSuggestion(
                first.field_locator,
                first.original,
                first.suggestion + " 999년",
                first.provider,
            ),
            *rest,
        ),
    )

    with pytest.raises(LlmCliError) as raised:
        TeamRewriteOrchestrator(port).deliberate(profile)

    assert raised.value.code == "invalid_rewrite_response"
    assert port.requests == []


def test_one_provider_failure_remains_a_normalized_cli_error() -> None:
    profile = _profile()

    class FailingPort(FakeTeamPort):
        def suggest_rewrites(
            self, profile: JobProfile, provider: LlmProvider
        ) -> RewriteSuggestionResult:
            if provider == LlmProvider.CLAUDE:
                raise LlmCliError("llm_usage_exhausted", retryable=True)
            return super().suggest_rewrites(profile, provider)

    with pytest.raises(LlmCliError) as raised:
        TeamRewriteOrchestrator(FailingPort(profile)).deliberate(profile)

    assert raised.value.code == "llm_usage_exhausted"
    assert str(raised.value) == "llm_usage_exhausted"


def test_audit_contains_only_hashes_and_normalized_provider_events() -> None:
    profile = _profile()
    result = TeamRewriteOrchestrator(FakeTeamPort(profile)).deliberate(profile)

    audit = result.audit.as_dict()
    serialized = json.dumps(audit, ensure_ascii=False)

    assert set(audit) == {"packet_hash", "proposal_hashes", "review_hashes", "events"}
    assert len(audit["packet_hash"]) == 64
    assert all(len(item["sha256"]) == 64 for item in audit["proposal_hashes"])
    assert all(len(item["sha256"]) == 64 for item in audit["review_hashes"])
    assert all(set(item) == {"provider", "status", "code"} for item in audit["events"])
    assert profile.job_description.job_purpose.text not in serialized
    assert "private@example.test" not in serialized
    assert "stdout" not in serialized and "stderr" not in serialized
    assert "source_refs" not in serialized and "references" not in serialized
