from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ncs_jd.adapters.fake_ncs_source import FakeNcsSourceAdapter
from ncs_jd.application.ncs_source import (
    EvidenceElement,
    EvidenceKsa,
    EvidenceUnit,
    NcsSourcePort,
    OptionalReference,
    ReadinessStatus,
    ScopeCandidate,
    SourceAudit,
    UnitEvidenceBundle,
    UnitEvidenceNotFoundError,
)


UNIT_CODE = "0202020103_23v4"


def _bundle() -> UnitEvidenceBundle:
    return UnitEvidenceBundle(
        unit=EvidenceUnit(UNIT_CODE, "인력채용", "5", "조직에 필요한 인력을 채용하는 능력"),
        elements=(
            EvidenceElement(
                element_id="element-1",
                element_name="채용계획 수립하기",
                ksa=(EvidenceKsa("ksa-1", "knowledge", "채용 절차에 관한 지식"),),
            ),
        ),
        source_audit=SourceAudit("NCS_MCP", datetime(2026, 8, 13, tzinfo=UTC), UNIT_CODE),
    )


def _adapter() -> FakeNcsSourceAdapter:
    return FakeNcsSourceAdapter(
        candidates=(
            ScopeCandidate("인사", "인적자원 관리", "z-unit", "인사평가", "5", None),
            ScopeCandidate("인사", "인적자원 관리", UNIT_CODE, "인력채용", "5", "채용 능력"),
        ),
        unit_bundles={UNIT_CODE: _bundle()},
        optional_references=(
            OptionalReference("qualification-1", UNIT_CODE, "qualification", "참고 자격"),
            OptionalReference("career-1", UNIT_CODE, "career_path", "참고 경력경로"),
        ),
        readiness=ReadinessStatus(healthy=True, ready=True, message="ready"),
    )


def test_fake_conforms_to_port_protocol() -> None:
    assert isinstance(_adapter(), NcsSourcePort)


def test_search_is_filtered_limited_and_deterministic() -> None:
    adapter = _adapter()

    first = asyncio.run(adapter.search_scope_candidates("인사", limit=1))
    second = asyncio.run(adapter.search_scope_candidates("인사", limit=1))

    assert first == second
    assert [item.unit_code for item in first] == [UNIT_CODE]
    assert [call.method for call in adapter.calls] == ["search_scope_candidates", "search_scope_candidates"]


@pytest.mark.parametrize("limit", [0, 101])
def test_search_rejects_out_of_range_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        asyncio.run(_adapter().search_scope_candidates("인사", limit=limit))


def test_unit_detail_preserves_raw_ksa_without_review_status() -> None:
    bundle = asyncio.run(_adapter().load_unit_evidence(UNIT_CODE))
    ksa = bundle.elements[0].ksa[0]

    assert ksa.ksa_text_raw == "채용 절차에 관한 지식"
    assert not hasattr(ksa, "review_status")
    assert not hasattr(bundle.source_audit, "human_reviewed")


def test_missing_unit_is_structured_error() -> None:
    with pytest.raises(UnitEvidenceNotFoundError, match="missing-unit"):
        asyncio.run(_adapter().load_unit_evidence("missing-unit"))


def test_optional_references_are_filtered_and_reference_only() -> None:
    references = asyncio.run(_adapter().load_optional_references(UNIT_CODE, ("qualification",)))

    assert [item.reference_id for item in references] == ["qualification-1"]
    assert all(item.evidence_grade == "reference" for item in references)


def test_readiness_is_returned_without_side_effects() -> None:
    adapter = _adapter()

    status = asyncio.run(adapter.check_readiness())

    assert status == ReadinessStatus(healthy=True, ready=True, message="ready")
    assert adapter.calls[-1].method == "check_readiness"
