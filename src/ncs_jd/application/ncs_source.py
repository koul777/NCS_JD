"""Technology-neutral NCS source port and normalized application DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable


KsaKind = Literal["knowledge", "skill", "attitude"]
OptionalReferenceKind = Literal["career_path", "qualification", "job_base"]
SourceWarningCode = Literal[
    "partial_unit_evidence",
    "unsupported_ksa_type",
]


class NcsSourceError(RuntimeError):
    """Base error exposed by NCS source adapters without leaking SDK details."""

    code = "ncs_source_error"
    retryable = False

    def __init__(self, message: str, *, operation: str | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class UnitEvidenceNotFoundError(NcsSourceError):
    """Raised when a selected unit has no evidence in the configured source."""

    code = "unit_evidence_not_found"


class NcsSourceContractError(NcsSourceError):
    """Raised when an MCP response cannot satisfy the application contract."""

    code = "ncs_source_contract_error"


class NcsSourceTimeoutError(NcsSourceError):
    """Raised after the bounded timeout retry has been exhausted."""

    code = "ncs_source_timeout"
    retryable = True


class NcsSourceUnavailableError(NcsSourceError):
    """Raised when the configured MCP transport cannot be reached or loaded."""

    code = "ncs_source_unavailable"
    retryable = True


class NcsSourceToolError(NcsSourceError):
    """Raised when an allowed MCP tool returns a structured failure."""

    code = "ncs_source_tool_error"


@dataclass(frozen=True, slots=True)
class ScopeCandidate:
    classification_path: str
    duty_definition: str | None
    unit_code: str
    unit_name: str
    unit_level: str | None
    unit_definition: str | None
    major_code: str | None = None
    major_name: str | None = None
    middle_code: str | None = None
    middle_name: str | None = None
    small_code: str | None = None
    small_name: str | None = None
    sub_code: str | None = None
    sub_name: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceUnit:
    unit_code: str
    unit_name: str
    unit_level: str | None
    unit_definition: str | None
    classification_path: str | None = None
    duty_definition: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceCriterion:
    criteria_id: str
    criteria_text_raw: str


@dataclass(frozen=True, slots=True)
class EvidenceKsa:
    ksa_id: str
    ksa_type: KsaKind
    ksa_text_raw: str


@dataclass(frozen=True, slots=True)
class EvidenceElement:
    element_id: str
    element_name: str
    criteria: tuple[EvidenceCriterion, ...] = ()
    ksa: tuple[EvidenceKsa, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceAudit:
    source_system: Literal["NCS_MCP"]
    retrieved_at: datetime
    unit_code: str
    tool_name: Literal["ncs_unit_detail"] = "ncs_unit_detail"
    data_sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NcsSourceWarning:
    """Non-fatal evidence loss represented without retaining the source payload."""

    code: SourceWarningCode
    message: str
    location: str


@dataclass(frozen=True, slots=True)
class UnitEvidenceBundle:
    unit: EvidenceUnit
    elements: tuple[EvidenceElement, ...]
    source_audit: SourceAudit
    warnings: tuple[NcsSourceWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class OptionalReference:
    reference_id: str
    unit_code: str
    kind: OptionalReferenceKind
    text_raw: str
    evidence_grade: Literal["reference"] = "reference"


@dataclass(frozen=True, slots=True)
class ReadinessStatus:
    healthy: bool
    ready: bool
    message: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceCall:
    method: str
    arguments: tuple[object, ...] = field(default_factory=tuple)


@runtime_checkable
class NcsSourcePort(Protocol):
    async def search_scope_candidates(self, query: str, limit: int = 20) -> list[ScopeCandidate]: ...

    async def load_unit_evidence(self, unit_code: str) -> UnitEvidenceBundle: ...

    async def load_optional_references(
        self,
        unit_code: str,
        kinds: tuple[OptionalReferenceKind, ...],
    ) -> list[OptionalReference]: ...

    async def check_readiness(self) -> ReadinessStatus: ...


__all__ = [
    "EvidenceCriterion",
    "EvidenceElement",
    "EvidenceKsa",
    "EvidenceUnit",
    "KsaKind",
    "NcsSourceContractError",
    "NcsSourceError",
    "NcsSourcePort",
    "NcsSourceTimeoutError",
    "NcsSourceToolError",
    "NcsSourceUnavailableError",
    "NcsSourceWarning",
    "OptionalReference",
    "OptionalReferenceKind",
    "ReadinessStatus",
    "ScopeCandidate",
    "SourceAudit",
    "SourceCall",
    "SourceWarningCode",
    "UnitEvidenceBundle",
    "UnitEvidenceNotFoundError",
]
