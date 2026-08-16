"""Safe application contracts for CLI-backed rewrite suggestions.

The deterministic :class:`~ncs_jd.domain.job_profile.JobProfile` is always the
input to this boundary. Validated suggestions may only replace the three
allow-listed prose fields while IDs, evidence links, and protected facts remain
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections import Counter
import re
from typing import Protocol

from ncs_jd.domain.job_profile import JobProfile, ReviewFlagCode


_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_NUMBER_RE = re.compile(r"(?<!\d)\d+(?:[.,]\d+)*(?!\d)")
_IDENTIFIER_RE = re.compile(
    r"(?i)(?:\b(?:ref|task|duty|ksa)[-_][a-z0-9_.:-]+\b|\b\d{8,}_[a-z0-9]+\b)"
)
_PROTECTED_TERMS = (
    "자격", "자격증", "학력", "학위", "경력", "근속", "법적", "법률", "법령", "의무",
    "license", "certification", "degree", "education", "experience", "years", "legal",
    "statutory", "mandatory",
)


class LlmProvider(str, Enum):
    OFF = "off"
    CODEX = "codex"
    CLAUDE = "claude"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider: LlmProvider
    installed: bool
    logged_in: bool
    login_in_progress: bool = False
    code: str = "not_authenticated"

    def as_dict(self) -> dict[str, object]:
        """Return only the deliberately sanitized public status."""

        return {
            "provider": self.provider.value,
            "installed": self.installed,
            "logged_in": self.logged_in,
            "login_in_progress": self.login_in_progress,
            "code": self.code,
        }


@dataclass(frozen=True, slots=True)
class LoginStartResult:
    provider: LlmProvider
    started: bool
    code: str

    def as_dict(self) -> dict[str, object]:
        return {"provider": self.provider.value, "started": self.started, "code": self.code}


@dataclass(frozen=True, slots=True)
class RewriteSuggestion:
    field_locator: str
    original: str
    suggestion: str
    provider: LlmProvider

    def as_dict(self) -> dict[str, str]:
        return {
            "field_locator": self.field_locator,
            "original": self.original,
            "suggestion": self.suggestion,
            "provider": self.provider.value,
        }


@dataclass(frozen=True, slots=True)
class RewriteSuggestionResult:
    provider: LlmProvider
    status: str
    code: str
    suggestions: tuple[RewriteSuggestion, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider.value,
            "status": self.status,
            "code": self.code,
            # Applying a suggestion is intentionally never implied by this flag.
            "applied": False,
            "suggestions": [item.as_dict() for item in self.suggestions],
        }


class LlmCliError(RuntimeError):
    """Normalized provider failure with no raw CLI or identity information."""

    def __init__(self, code: str, *, retryable: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class LlmRewriteApplicationError(ValueError):
    """Raised when suggestions cannot be safely applied to their exact source fields."""


def apply_rewrite_suggestions(
    profile: JobProfile,
    result: RewriteSuggestionResult,
) -> JobProfile:
    """Return a validated copy with only allow-listed prose replacements applied."""

    required: dict[str, str] = {
        "job_description.job_purpose.text": profile.job_description.job_purpose.text,
    }
    optional: dict[str, str] = {
        **{
            f"job_description.duties[id={item.id}].summary": item.summary
            for item in profile.job_description.duties
        },
        **{
            f"job_description.tasks[id={item.id}].description": item.description
            for item in profile.job_description.tasks
        },
    }
    expected = {**required, **optional}
    suggestions = {item.field_locator: item for item in result.suggestions}
    if (
        len(suggestions) != len(result.suggestions)
        or not set(required).issubset(suggestions)
        or not set(suggestions).issubset(expected)
    ):
        raise LlmRewriteApplicationError("rewrite suggestions do not match the allow-listed fields")
    for locator, item in suggestions.items():
        original = expected[locator]
        if item.provider != result.provider or item.original != original:
            raise LlmRewriteApplicationError("rewrite suggestion source mismatch")
        _validate_applied_text(original, item.suggestion)

    purpose = profile.job_description.job_purpose.model_copy(
        update={"text": suggestions["job_description.job_purpose.text"].suggestion}
    )
    duties = []
    for item in profile.job_description.duties:
        locator = f"job_description.duties[id={item.id}].summary"
        suggestion = suggestions.get(locator)
        duties.append(
            item.model_copy(update={"summary": suggestion.suggestion})
            if suggestion is not None
            else item
        )
    tasks = []
    for item in profile.job_description.tasks:
        locator = f"job_description.tasks[id={item.id}].description"
        suggestion = suggestions.get(locator)
        tasks.append(
            item.model_copy(update={"description": suggestion.suggestion})
            if suggestion is not None
            else item
        )
    job_description = profile.job_description.model_copy(
        update={"job_purpose": purpose, "duties": duties, "tasks": tasks}
    )
    review_flags = [
        flag
        for flag in profile.review_flags
        if flag.code != ReviewFlagCode.LLM_REWRITE_NOT_APPLIED
    ]
    updated = profile.model_copy(
        update={"job_description": job_description, "review_flags": review_flags}
    )
    return JobProfile.model_validate(updated.model_dump(mode="python", by_alias=True))


def _validate_applied_text(original: str, suggestion: str) -> None:
    cleaned = suggestion.strip()
    if not cleaned or len(cleaned) > 4000:
        raise LlmRewriteApplicationError("rewrite suggestion text is invalid")
    for pattern in (_URL_RE, _NUMBER_RE, _IDENTIFIER_RE):
        if Counter(pattern.findall(original)) != Counter(pattern.findall(cleaned)):
            raise LlmRewriteApplicationError("rewrite suggestion changed a protected fact")
    original_folded = original.casefold()
    suggestion_folded = cleaned.casefold()
    for term in _PROTECTED_TERMS:
        if suggestion_folded.count(term) != original_folded.count(term):
            raise LlmRewriteApplicationError("rewrite suggestion changed a protected requirement")


class LlmCliPort(Protocol):
    def list_provider_statuses(self) -> tuple[ProviderStatus, ...]: ...

    def start_login(self, provider: LlmProvider) -> LoginStartResult: ...

    def suggest_rewrites(
        self, profile: JobProfile, provider: LlmProvider
    ) -> RewriteSuggestionResult: ...


__all__ = [
    "apply_rewrite_suggestions",
    "LlmCliError",
    "LlmCliPort",
    "LlmProvider",
    "LlmRewriteApplicationError",
    "LoginStartResult",
    "ProviderStatus",
    "RewriteSuggestion",
    "RewriteSuggestionResult",
]
