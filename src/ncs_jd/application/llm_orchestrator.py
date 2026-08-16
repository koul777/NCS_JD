"""Two-round, two-provider orchestration for safe prose rewrites.

The team mode deliberately sits above :class:`LlmProvider`: Codex and Claude
remain the only executable providers.  Round one asks both providers for a
proposal and round two asks each provider to review the other's proposal.  A
local resolver then applies a deliberately narrow consensus rule; it never
asks a model to arbitrate a disagreement.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from ncs_jd.application.llm_rewriter import (
    apply_rewrite_suggestions,
    LlmCliError,
    LlmProvider,
    LlmRewriteApplicationError,
    ProviderStatus,
    RewriteSuggestion,
    RewriteSuggestionResult,
)
from ncs_jd.domain.job_profile import JobProfile


_TEAM_PROVIDERS = (LlmProvider.CODEX, LlmProvider.CLAUDE)
_REVIEW_VERDICTS = frozenset({"accept", "reject"})
_REVIEW_CODES = frozenset({"meaning_preserved", "meaning_not_preserved"})


@dataclass(frozen=True, slots=True)
class PeerReviewRequest:
    """A validated proposal to be reviewed by the other provider."""

    reviewer: LlmProvider
    proposal: RewriteSuggestionResult

    @property
    def candidate_provider(self) -> LlmProvider:
        return self.proposal.provider


@dataclass(frozen=True, slots=True)
class PeerReviewDecision:
    """Normalized, explanation-free review of one proposed field."""

    field_locator: str
    verdict: str
    code: str

    def as_dict(self) -> dict[str, str]:
        return {
            "field_locator": self.field_locator,
            "verdict": self.verdict,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class PeerReviewResult:
    """Normalized review result; raw provider output is intentionally absent."""

    reviewer: LlmProvider
    candidate_provider: LlmProvider
    status: str
    code: str
    decisions: tuple[PeerReviewDecision, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "reviewer": self.reviewer.value,
            "candidate_provider": self.candidate_provider.value,
            "status": self.status,
            "code": self.code,
            "decisions": [decision.as_dict() for decision in self.decisions],
        }


class TeamRewritePort(Protocol):
    """Provider boundary required by :class:`TeamRewriteOrchestrator`."""

    def list_provider_statuses(self) -> tuple[ProviderStatus, ...]: ...

    def suggest_rewrites(
        self, profile: JobProfile, provider: LlmProvider
    ) -> RewriteSuggestionResult: ...

    def review_rewrite_proposal(self, request: PeerReviewRequest) -> PeerReviewResult: ...


@dataclass(frozen=True, slots=True)
class ProviderArtifactHash:
    provider: LlmProvider
    sha256: str

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider.value, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class TeamAuditEvent:
    """Safe event metadata without prompts, output, identity, or source payloads."""

    provider: LlmProvider
    status: str
    code: str

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider.value,
            "status": self.status,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class TeamRewriteAudit:
    packet_hash: str
    proposal_hashes: tuple[ProviderArtifactHash, ...]
    review_hashes: tuple[ProviderArtifactHash, ...]
    events: tuple[TeamAuditEvent, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "packet_hash": self.packet_hash,
            "proposal_hashes": [item.as_dict() for item in self.proposal_hashes],
            "review_hashes": [item.as_dict() for item in self.review_hashes],
            "events": [event.as_dict() for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class TeamRewriteResult:
    """Deterministic local resolution of the two provider rounds."""

    status: str
    code: str
    selected_rewrites: RewriteSuggestionResult | None
    audit: TeamRewriteAudit

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "code": self.code,
            "rounds": 2,
            "selected_rewrites": (
                self.selected_rewrites.as_dict() if self.selected_rewrites is not None else None
            ),
            "audit": self.audit.as_dict(),
        }


class TeamRewriteOrchestrator:
    """Run exactly two concurrent provider rounds and resolve locally."""

    def __init__(self, llm_cli: TeamRewritePort) -> None:
        self._llm_cli = llm_cli

    def deliberate(self, profile: JobProfile) -> TeamRewriteResult:
        """Return consensus rewrites, preserving exact originals on disagreement."""

        proposals = self._proposal_round(profile)
        self._validate_proposals(profile, proposals)
        reviews = self._review_round(proposals)
        self._validate_reviews(proposals, reviews)

        selected = self._resolve(proposals, reviews)
        if selected is not None:
            # Validate the locally composed result through the same application
            # boundary used for single-provider suggestions before returning it.
            try:
                apply_rewrite_suggestions(profile, selected)
            except LlmRewriteApplicationError as exc:
                raise LlmCliError("invalid_rewrite_response", retryable=True) from exc

        audit = self._audit(proposals, reviews)
        return TeamRewriteResult(
            status="resolved",
            code="team_rewrite_resolved" if selected is not None else "team_no_changes",
            selected_rewrites=selected,
            audit=audit,
        )

    def _proposal_round(
        self, profile: JobProfile
    ) -> dict[LlmProvider, RewriteSuggestionResult]:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ncs-jd-propose") as executor:
            futures = {
                provider: executor.submit(self._llm_cli.suggest_rewrites, profile, provider)
                for provider in _TEAM_PROVIDERS
            }
            return {
                provider: self._provider_result(futures[provider])
                for provider in _TEAM_PROVIDERS
            }

    def _review_round(
        self,
        proposals: dict[LlmProvider, RewriteSuggestionResult],
    ) -> dict[LlmProvider, PeerReviewResult]:
        requests = {
            LlmProvider.CODEX: PeerReviewRequest(
                reviewer=LlmProvider.CODEX,
                proposal=proposals[LlmProvider.CLAUDE],
            ),
            LlmProvider.CLAUDE: PeerReviewRequest(
                reviewer=LlmProvider.CLAUDE,
                proposal=proposals[LlmProvider.CODEX],
            ),
        }
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ncs-jd-review") as executor:
            futures = {
                provider: executor.submit(self._llm_cli.review_rewrite_proposal, request)
                for provider, request in requests.items()
            }
            return {
                provider: self._provider_result(futures[provider])
                for provider in _TEAM_PROVIDERS
            }

    @staticmethod
    def _provider_result(future: Future[object]):  # type: ignore[no-untyped-def]
        try:
            return future.result()
        except LlmCliError:
            raise
        except Exception as exc:
            raise LlmCliError("rewrite_provider_failed", retryable=True) from exc

    @staticmethod
    def _validate_proposals(
        profile: JobProfile,
        proposals: dict[LlmProvider, RewriteSuggestionResult],
    ) -> None:
        locator_sets: list[set[str]] = []
        for provider in _TEAM_PROVIDERS:
            result = proposals[provider]
            if (
                result.provider != provider
                or result.status != "suggestions_ready"
                or result.code != "suggestions_ready"
            ):
                raise LlmCliError("invalid_rewrite_response", retryable=True)
            try:
                apply_rewrite_suggestions(profile, result)
            except LlmRewriteApplicationError as exc:
                raise LlmCliError("invalid_rewrite_response", retryable=True) from exc
            locator_sets.append({item.field_locator for item in result.suggestions})
        if not locator_sets[0] or locator_sets[0] != locator_sets[1]:
            raise LlmCliError("invalid_rewrite_response", retryable=True)

    @staticmethod
    def _validate_reviews(
        proposals: dict[LlmProvider, RewriteSuggestionResult],
        reviews: dict[LlmProvider, PeerReviewResult],
    ) -> None:
        for reviewer in _TEAM_PROVIDERS:
            result = reviews[reviewer]
            expected_candidate = (
                LlmProvider.CLAUDE if reviewer == LlmProvider.CODEX else LlmProvider.CODEX
            )
            expected_locators = {
                item.field_locator for item in proposals[expected_candidate].suggestions
            }
            decisions = {item.field_locator: item for item in result.decisions}
            if (
                result.reviewer != reviewer
                or result.candidate_provider != expected_candidate
                or result.status != "review_ready"
                or result.code != "review_ready"
                or len(decisions) != len(result.decisions)
                or set(decisions) != expected_locators
                or any(item.verdict not in _REVIEW_VERDICTS for item in result.decisions)
                or any(item.code not in _REVIEW_CODES for item in result.decisions)
                or any(
                    (item.verdict == "accept") != (item.code == "meaning_preserved")
                    for item in result.decisions
                )
            ):
                raise LlmCliError("invalid_review_response", retryable=True)

    @staticmethod
    def _resolve(
        proposals: dict[LlmProvider, RewriteSuggestionResult],
        reviews: dict[LlmProvider, PeerReviewResult],
    ) -> RewriteSuggestionResult | None:
        codex = {item.field_locator: item for item in proposals[LlmProvider.CODEX].suggestions}
        claude = {item.field_locator: item for item in proposals[LlmProvider.CLAUDE].suggestions}
        codex_reviews_claude = {
            item.field_locator: item for item in reviews[LlmProvider.CODEX].decisions
        }
        claude_reviews_codex = {
            item.field_locator: item for item in reviews[LlmProvider.CLAUDE].decisions
        }

        # RewriteSuggestionResult has a single-provider invariant in the
        # existing application boundary.  CODEX is used only as a stable local
        # carrier for the merged text; provenance remains in the hash-only
        # audit rather than being represented as a third provider enum value.
        resolved: list[RewriteSuggestion] = []
        any_change = False
        for locator in codex:
            codex_item = codex[locator]
            claude_item = claude[locator]
            if codex_item.suggestion == claude_item.suggestion:
                text = codex_item.suggestion
            else:
                codex_has_peer_consensus = (
                    claude_reviews_codex[locator].verdict == "accept"
                )
                claude_has_peer_consensus = (
                    codex_reviews_claude[locator].verdict == "accept"
                )
                if codex_has_peer_consensus != claude_has_peer_consensus:
                    text = (
                        codex_item.suggestion
                        if codex_has_peer_consensus
                        else claude_item.suggestion
                    )
                else:
                    text = codex_item.original
            any_change = any_change or text != codex_item.original
            resolved.append(
                RewriteSuggestion(
                    field_locator=locator,
                    original=codex_item.original,
                    suggestion=text,
                    provider=LlmProvider.CODEX,
                )
            )
        if not any_change:
            return None
        return RewriteSuggestionResult(
            provider=LlmProvider.CODEX,
            status="suggestions_ready",
            code="suggestions_ready",
            suggestions=tuple(resolved),
        )

    @staticmethod
    def _audit(
        proposals: dict[LlmProvider, RewriteSuggestionResult],
        reviews: dict[LlmProvider, PeerReviewResult],
    ) -> TeamRewriteAudit:
        packet = [
            {
                "field_locator": item.field_locator,
                "original": item.original,
            }
            for item in proposals[LlmProvider.CODEX].suggestions
        ]
        proposal_hashes = tuple(
            ProviderArtifactHash(provider, _hash_json(proposals[provider].as_dict()))
            for provider in _TEAM_PROVIDERS
        )
        review_hashes = tuple(
            ProviderArtifactHash(provider, _hash_json(reviews[provider].as_dict()))
            for provider in _TEAM_PROVIDERS
        )
        events = tuple(
            [
                TeamAuditEvent(provider, proposals[provider].status, proposals[provider].code)
                for provider in _TEAM_PROVIDERS
            ]
            + [
                TeamAuditEvent(provider, reviews[provider].status, reviews[provider].code)
                for provider in _TEAM_PROVIDERS
            ]
        )
        return TeamRewriteAudit(
            packet_hash=_hash_json(packet),
            proposal_hashes=proposal_hashes,
            review_hashes=review_hashes,
            events=events,
        )


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "PeerReviewDecision",
    "PeerReviewRequest",
    "PeerReviewResult",
    "ProviderArtifactHash",
    "TeamAuditEvent",
    "TeamRewriteAudit",
    "TeamRewriteOrchestrator",
    "TeamRewritePort",
    "TeamRewriteResult",
]
