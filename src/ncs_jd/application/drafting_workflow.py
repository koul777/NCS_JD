"""Application orchestration for the deterministic, LLM-off draft path.

This module joins the parser, announcement extractor, NCS source, and pure
``JobProfile`` assembler without letting adapter-specific payloads cross the
application boundary.  UUIDs and timestamps are always supplied by the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from ncs_jd.application.announcement_extraction import AnnouncementExtraction, extract_announcement
from ncs_jd.application.document_parser import DocumentParserPort
from ncs_jd.application.job_profile_assembler import (
    JobProfileAssembler,
    JobProfileAssemblyRequest,
    OrganizationInput,
)
from ncs_jd.application.ncs_source import (
    NcsSourceContractError,
    NcsSourceError,
    NcsSourcePort,
    OptionalReference,
    UnitEvidenceBundle,
)
from ncs_jd.domain.job_profile import ClassificationPath, IncludedUnit, JobProfile


UnitEvidenceStatus = Literal["loaded", "failed"]


@dataclass(frozen=True, slots=True)
class ConfirmedDraftRequest:
    """Human-confirmed scope and organization facts needed to create a draft.

    ``classification_paths`` and ``included_units`` are deliberately domain
    objects rather than search candidates.  Constructing this request is the
    human-confirmation gate; this workflow never promotes search results on its
    own.
    """

    document_id: UUID
    created_at: datetime
    retrieved_at: datetime
    organization_job_title: str
    classification_paths: tuple[ClassificationPath, ...]
    included_units: tuple[IncludedUnit, ...]
    organization_input: OrganizationInput = OrganizationInput()
    excluded_unit_codes: tuple[str, ...] = ()
    excluded_task_terms: tuple[str, ...] = ()
    selected_references: tuple[OptionalReference, ...] = ()
    target_level_input: str | None = None
    mcp_url_label: str = "local-ncs-mcp"
    scope_confirmation_required: bool = False
    scope_match_notes: tuple[str, ...] = ()


# A short alias is useful at web/API boundaries while retaining the explicit
# confirmation semantics in the canonical class name.
DraftingRequest = ConfirmedDraftRequest


@dataclass(frozen=True, slots=True)
class UnitEvidenceDiagnostic:
    """Safe, structured outcome of loading one selected unit."""

    unit_code: str
    status: UnitEvidenceStatus
    code: str
    message: str
    retryable: bool = False
    source_warning_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_code": self.unit_code,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "source_warning_codes": list(self.source_warning_codes),
        }


@dataclass(frozen=True, slots=True)
class DraftingDiagnostics:
    """Ordered unit-load diagnostics suitable for a recovery UI."""

    unit_evidence: tuple[UnitEvidenceDiagnostic, ...] = ()

    @property
    def loaded_unit_codes(self) -> tuple[str, ...]:
        return tuple(item.unit_code for item in self.unit_evidence if item.status == "loaded")

    @property
    def failed_unit_codes(self) -> tuple[str, ...]:
        return tuple(item.unit_code for item in self.unit_evidence if item.status == "failed")

    def as_dict(self) -> dict[str, object]:
        return {
            "loaded_unit_codes": list(self.loaded_unit_codes),
            "failed_unit_codes": list(self.failed_unit_codes),
            "unit_evidence": [item.as_dict() for item in self.unit_evidence],
        }


@dataclass(frozen=True, slots=True)
class DraftGenerationResult:
    job_profile: JobProfile
    diagnostics: DraftingDiagnostics


DraftingResult = DraftGenerationResult


class DraftingWorkflowError(RuntimeError):
    """Base error for failures that prevent a deterministic draft."""

    default_code = "drafting_workflow_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        diagnostics: DraftingDiagnostics = DraftingDiagnostics(),
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.diagnostics = diagnostics

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "diagnostics": self.diagnostics.as_dict(),
        }


class NoUsableDetailedEvidenceError(DraftingWorkflowError):
    """Raised when none of the active selected units can support a draft."""

    default_code = "no_usable_detailed_evidence"


# Backwards-friendly concise name for callers that do not need the distinction
# between detailed and optional reference evidence.
NoUsableEvidenceError = NoUsableDetailedEvidenceError


class DraftingWorkflow:
    """Coordinate parser/source ports and the pure deterministic assembler."""

    def __init__(
        self,
        document_parser: DocumentParserPort,
        ncs_source: NcsSourcePort,
        assembler: JobProfileAssembler | None = None,
    ) -> None:
        self._document_parser = document_parser
        self._ncs_source = ncs_source
        self._assembler = assembler or JobProfileAssembler()

    async def parse_announcement(
        self,
        source_name: str | Path,
        content: bytes,
    ) -> AnnouncementExtraction:
        """Parse an upload at the Kordoc boundary and extract review candidates."""

        parsed = await asyncio.to_thread(self._document_parser.parse, source_name, content)
        return extract_announcement(parsed)

    async def generate_draft(self, request: ConfirmedDraftRequest) -> DraftGenerationResult:
        """Load only the confirmed active units and assemble a deterministic draft.

        A failure for one unit is represented in diagnostics while successful
        bundles are retained.  Omitting the failed bundle from the assembly
        request lets the assembler create its standard ``partial_unit_evidence``
        review flag for that selected unit.
        """

        excluded_codes = {
            code.strip() for code in request.excluded_unit_codes if code.strip()
        }
        active_units = tuple(
            unit for unit in request.included_units if unit.unit_code not in excluded_codes
        )

        diagnostics: list[UnitEvidenceDiagnostic] = []
        bundles: list[UnitEvidenceBundle] = []
        for unit in active_units:
            try:
                bundle = await self._ncs_source.load_unit_evidence(unit.unit_code)
                _validate_loaded_bundle(unit.unit_code, bundle)
            except NcsSourceError as exc:
                diagnostics.append(
                    UnitEvidenceDiagnostic(
                        unit_code=unit.unit_code,
                        status="failed",
                        code=exc.code,
                        message=str(exc) or "NCS 상세 근거를 불러오지 못했습니다.",
                        retryable=exc.retryable,
                    )
                )
            except Exception as exc:  # A port implementation must not collapse the whole selection.
                diagnostics.append(
                    UnitEvidenceDiagnostic(
                        unit_code=unit.unit_code,
                        status="failed",
                        code="unexpected_ncs_source_error",
                        message=(
                            "NCS 상세 근거 포트가 예기치 않은 오류를 반환했습니다: "
                            f"{type(exc).__name__}"
                        ),
                    )
                )
            else:
                bundles.append(bundle)
                warning_codes = tuple(warning.code for warning in bundle.warnings)
                diagnostics.append(
                    UnitEvidenceDiagnostic(
                        unit_code=unit.unit_code,
                        status="loaded",
                        code="unit_evidence_loaded",
                        message=(
                            "NCS 상세 근거를 불러왔습니다."
                            if not warning_codes
                            else "NCS 상세 근거를 일부 경고와 함께 불러왔습니다."
                        ),
                        source_warning_codes=warning_codes,
                    )
                )

        structured_diagnostics = DraftingDiagnostics(tuple(diagnostics))
        if not bundles:
            raise NoUsableDetailedEvidenceError(
                "선택된 능력단위에서 사용할 수 있는 NCS 상세 근거를 하나도 불러오지 못했습니다.",
                diagnostics=structured_diagnostics,
            )

        assembly_request = JobProfileAssemblyRequest(
            document_id=request.document_id,
            created_at=request.created_at,
            retrieved_at=request.retrieved_at,
            organization_job_title=request.organization_job_title,
            classification_paths=request.classification_paths,
            included_units=request.included_units,
            unit_evidence_bundles=tuple(bundles),
            organization_input=request.organization_input,
            excluded_unit_codes=request.excluded_unit_codes,
            excluded_task_terms=request.excluded_task_terms,
            selected_references=request.selected_references,
            target_level_input=request.target_level_input,
            mcp_url_label=request.mcp_url_label,
            scope_confirmation_required=request.scope_confirmation_required,
            scope_match_notes=request.scope_match_notes,
        )
        return DraftGenerationResult(
            job_profile=self._assembler.assemble(assembly_request),
            diagnostics=structured_diagnostics,
        )


def _validate_loaded_bundle(unit_code: str, bundle: UnitEvidenceBundle) -> None:
    """Reject a malformed success as a per-unit source contract failure."""

    if bundle.unit.unit_code != unit_code:
        raise NcsSourceContractError(
            f"unit evidence code mismatch for {unit_code}",
            operation="load_unit_evidence",
        )
    if bundle.source_audit.unit_code != unit_code:
        raise NcsSourceContractError(
            f"source audit code mismatch for {unit_code}",
            operation="load_unit_evidence",
        )
    if not bundle.unit.unit_name.strip():
        raise NcsSourceContractError(
            f"unit evidence name is empty for {unit_code}",
            operation="load_unit_evidence",
        )


__all__ = [
    "ConfirmedDraftRequest",
    "DraftGenerationResult",
    "DraftingDiagnostics",
    "DraftingRequest",
    "DraftingResult",
    "DraftingWorkflow",
    "DraftingWorkflowError",
    "NoUsableDetailedEvidenceError",
    "NoUsableEvidenceError",
    "UnitEvidenceDiagnostic",
    "UnitEvidenceStatus",
]
