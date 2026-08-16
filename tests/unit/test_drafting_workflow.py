from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ncs_jd.adapters.fake_ncs_source import FakeNcsSourceAdapter
from ncs_jd.application.document_parser import (
    DocumentMetadata,
    ParseQualitySummary,
    ParsedBlock,
    ParsedDocument,
    SourceLocator,
)
from ncs_jd.application.drafting_workflow import (
    ConfirmedDraftRequest,
    DraftingWorkflow,
    NoUsableDetailedEvidenceError,
)
from ncs_jd.application.ncs_source import (
    EvidenceCriterion,
    EvidenceElement,
    EvidenceKsa,
    EvidenceUnit,
    SourceAudit,
    UnitEvidenceBundle,
)
from ncs_jd.domain.job_profile import ClassificationPath, IncludedUnit, ReviewFlagCode


NOW = datetime(2026, 8, 13, 12, 15, tzinfo=UTC)
DOCUMENT_ID = UUID("de305d54-75b4-431b-adb2-eb6b9e546014")
UNIT_A = "UNIT-A"
UNIT_B = "UNIT-B"
UNIT_EXCLUDED = "UNIT-X"


class FakeDocumentParser:
    def __init__(self, document: ParsedDocument) -> None:
        self.document = document
        self.calls: list[tuple[str | Path, bytes]] = []

    def parse(self, source_name: str | Path, content: bytes) -> ParsedDocument:
        self.calls.append((source_name, content))
        return self.document


def _document() -> ParsedDocument:
    block = ParsedBlock(
        locator=SourceLocator("block-0001", 0, 1),
        block_type="table",
        table_rows=(
            ("채용분야", "담당업무", "NCS 세분류"),
            ("시설관리", "시설 점검\n교육 운영", "시설관리"),
        ),
    )
    return ParsedDocument(
        source_name="공고.pdf",
        document_format="pdf",
        markdown="채용분야 | 담당업무 | NCS 세분류",
        blocks=(block,),
        metadata=DocumentMetadata(page_count=1),
        quality=ParseQualitySummary(1, 0, 1, 1, 21, 0, "good"),
    )


def _bundle(unit_code: str, unit_name: str) -> UnitEvidenceBundle:
    return UnitEvidenceBundle(
        unit=EvidenceUnit(
            unit_code=unit_code,
            unit_name=unit_name,
            unit_level="4",
            unit_definition=f"{unit_name} 업무를 수행한다.",
            classification_path="시설관리",
            duty_definition="시설의 상태와 운영을 관리한다.",
        ),
        elements=(
            EvidenceElement(
                element_id=f"{unit_code}-E1",
                element_name="시설 점검하기",
                criteria=(EvidenceCriterion(f"{unit_code}-C1", "시설 상태를 확인할 수 있다."),),
                ksa=(EvidenceKsa(f"{unit_code}-K1", "knowledge", "시설 점검 지식"),),
            ),
            EvidenceElement(
                element_id=f"{unit_code}-E2",
                element_name="교육 운영하기",
                criteria=(EvidenceCriterion(f"{unit_code}-C2", "교육을 운영할 수 있다."),),
                ksa=(EvidenceKsa(f"{unit_code}-K2", "knowledge", "교육 운영 지식"),),
            ),
        ),
        source_audit=SourceAudit("NCS_MCP", NOW, unit_code),
    )


def _included(unit_code: str, unit_name: str) -> IncludedUnit:
    return IncludedUnit(
        unit_code=unit_code,
        unit_name=unit_name,
        unit_level="4",
        selection_reason="사람이 확인하여 포함",
    )


def _request(
    *units: IncludedUnit,
    excluded_unit_codes: tuple[str, ...] = (),
    excluded_task_terms: tuple[str, ...] = (),
) -> ConfirmedDraftRequest:
    return ConfirmedDraftRequest(
        document_id=DOCUMENT_ID,
        created_at=NOW,
        retrieved_at=NOW,
        organization_job_title="시설관리 담당자",
        classification_paths=(
            ClassificationPath(
                major_code="14",
                middle_code="01",
                small_code="01",
                sub_code="01",
                label="건설 > 시설관리",
            ),
        ),
        included_units=units,
        excluded_unit_codes=excluded_unit_codes,
        excluded_task_terms=excluded_task_terms,
    )


def test_parse_announcement_uses_parser_boundary_and_deterministic_extractor() -> None:
    parser = FakeDocumentParser(_document())
    workflow = DraftingWorkflow(parser, FakeNcsSourceAdapter())

    first = asyncio.run(workflow.parse_announcement("업로드.pdf", b"fixture"))
    second = asyncio.run(workflow.parse_announcement("업로드.pdf", b"fixture"))

    assert first == second
    assert parser.calls == [("업로드.pdf", b"fixture"), ("업로드.pdf", b"fixture")]
    assert first.source_name == "공고.pdf"
    assert first.role_candidates[0].role_title is not None
    assert first.role_candidates[0].role_title.text == "시설관리"
    assert [item.text for item in first.role_candidates[0].duties] == ["시설 점검", "교육 운영"]
    assert all(item.review_required is True for item in first.role_candidates[0].duties)


def test_generate_draft_loads_only_active_confirmed_units_and_preserves_exclusions() -> None:
    parser = FakeDocumentParser(_document())
    source = FakeNcsSourceAdapter(
        unit_bundles={
            UNIT_A: _bundle(UNIT_A, "시설 점검"),
            UNIT_EXCLUDED: _bundle(UNIT_EXCLUDED, "제외 단위"),
        }
    )
    workflow = DraftingWorkflow(parser, source)
    request = _request(
        _included(UNIT_A, "시설 점검"),
        _included(UNIT_EXCLUDED, "제외 단위"),
        excluded_unit_codes=(UNIT_EXCLUDED,),
        excluded_task_terms=("교육",),
    )

    result = asyncio.run(workflow.generate_draft(request))
    profile = result.job_profile

    assert [(call.method, call.arguments) for call in source.calls] == [
        ("load_unit_evidence", (UNIT_A,))
    ]
    assert result.diagnostics.loaded_unit_codes == (UNIT_A,)
    assert result.diagnostics.failed_unit_codes == ()
    assert profile.document_id == DOCUMENT_ID
    assert profile.created_at == profile.updated_at == NOW
    assert [unit.unit_code for unit in profile.scope_selection.included_units] == [UNIT_A]
    assert profile.scope_selection.excluded_unit_codes == [UNIT_EXCLUDED]
    assert [task.title for task in profile.job_description.tasks] == ["시설 점검하기"]
    assert all(reference.unit_code != UNIT_EXCLUDED for reference in profile.references)
    assert all("교육" not in item.text for item in profile.person_specification.knowledge)
    assert all("교육" not in reference.text_preview for reference in profile.references)


def test_partial_source_failure_keeps_success_and_assembler_flags_missing_bundle() -> None:
    source = FakeNcsSourceAdapter(unit_bundles={UNIT_A: _bundle(UNIT_A, "시설 점검")})
    workflow = DraftingWorkflow(FakeDocumentParser(_document()), source)
    request = _request(
        _included(UNIT_A, "시설 점검"),
        _included(UNIT_B, "설비 운영"),
    )

    result = asyncio.run(workflow.generate_draft(request))

    assert result.diagnostics.loaded_unit_codes == (UNIT_A,)
    assert result.diagnostics.failed_unit_codes == (UNIT_B,)
    failed = result.diagnostics.unit_evidence[1]
    assert failed.code == "unit_evidence_not_found"
    assert failed.retryable is False
    assert [duty.title for duty in result.job_profile.job_description.duties] == ["시설 점검"]
    assert [unit.unit_code for unit in result.job_profile.scope_selection.included_units] == [
        UNIT_A,
        UNIT_B,
    ]
    assert any(
        flag.code == ReviewFlagCode.PARTIAL_UNIT_EVIDENCE and UNIT_B in flag.message
        for flag in result.job_profile.review_flags
    )


def test_all_selected_unit_failures_raise_explicit_error_with_diagnostics() -> None:
    source = FakeNcsSourceAdapter()
    workflow = DraftingWorkflow(FakeDocumentParser(_document()), source)
    request = _request(
        _included(UNIT_A, "시설 점검"),
        _included(UNIT_B, "설비 운영"),
    )

    with pytest.raises(NoUsableDetailedEvidenceError) as captured:
        asyncio.run(workflow.generate_draft(request))

    error = captured.value
    assert error.code == "no_usable_detailed_evidence"
    assert error.diagnostics.loaded_unit_codes == ()
    assert error.diagnostics.failed_unit_codes == (UNIT_A, UNIT_B)
    assert error.as_dict()["diagnostics"] == error.diagnostics.as_dict()
    assert [call.arguments for call in source.calls] == [(UNIT_A,), (UNIT_B,)]


def test_same_caller_values_produce_identical_profile_and_diagnostics() -> None:
    request = _request(
        _included(UNIT_A, "시설 점검"),
        _included(UNIT_B, "설비 운영"),
        excluded_task_terms=("교육",),
    )

    def run_once():
        source = FakeNcsSourceAdapter(unit_bundles={UNIT_A: _bundle(UNIT_A, "시설 점검")})
        workflow = DraftingWorkflow(FakeDocumentParser(_document()), source)
        return asyncio.run(workflow.generate_draft(request))

    first = run_once()
    second = run_once()

    assert first.diagnostics == second.diagnostics
    assert first.job_profile.model_dump(mode="json", by_alias=True) == second.job_profile.model_dump(
        mode="json", by_alias=True
    )
