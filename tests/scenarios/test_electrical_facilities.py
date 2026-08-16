from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import UUID

from ncs_jd.application.job_profile_assembler import JobProfileAssemblyRequest, assemble_job_profile
from ncs_jd.application.ncs_source import (
    EvidenceCriterion,
    EvidenceElement,
    EvidenceKsa,
    EvidenceUnit,
    SourceAudit,
    UnitEvidenceBundle,
)
from ncs_jd.domain.job_profile import ClassificationPath, EvidenceGrade, IncludedUnit, JobProfile, Origin


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "electrical_facilities_synthetic.json"
NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


def _load_request() -> tuple[dict[str, object], JobProfileAssemblyRequest]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    paths = tuple(ClassificationPath.model_validate(item) for item in payload["classification_paths"])
    bundles: list[UnitEvidenceBundle] = []
    included: list[IncludedUnit] = []
    for unit_payload in payload["units"]:
        included.append(
            IncludedUnit(
                unit_code=unit_payload["unit_code"],
                unit_name=unit_payload["unit_name"],
                unit_level=unit_payload["unit_level"],
                selection_reason="synthetic fixture user selection",
            )
        )
        elements = tuple(
            EvidenceElement(
                element_id=element["element_id"],
                element_name=element["element_name"],
                criteria=tuple(
                    EvidenceCriterion(item["criteria_id"], item["criteria_text_raw"])
                    for item in element["criteria"]
                ),
                ksa=tuple(
                    EvidenceKsa(item["ksa_id"], item["ksa_type"], item["ksa_text_raw"])
                    for item in element["ksa"]
                ),
            )
            for element in unit_payload["elements"]
        )
        bundles.append(
            UnitEvidenceBundle(
                unit=EvidenceUnit(
                    unit_code=unit_payload["unit_code"],
                    unit_name=unit_payload["unit_name"],
                    unit_level=unit_payload["unit_level"],
                    unit_definition=unit_payload["unit_definition"],
                    classification_path=unit_payload["classification_path"],
                    duty_definition=unit_payload["duty_definition"],
                ),
                elements=elements,
                # Production supplies these DTOs through the NCS MCP adapter;
                # this deliberately synthetic fixture exercises assembly only.
                source_audit=SourceAudit("NCS_MCP", NOW, unit_payload["unit_code"]),
            )
        )

    return payload, JobProfileAssemblyRequest(
        document_id=UUID("87654321-4321-6789-4321-678943216789"),
        created_at=NOW,
        retrieved_at=NOW,
        organization_job_title="전기시설 유지보수",
        classification_paths=paths,
        included_units=tuple(included),
        unit_evidence_bundles=tuple(bundles),
    )


def test_three_electrical_facility_paths_units_and_ksa_provenance_are_retained() -> None:
    fixture, request = _load_request()
    metadata = fixture["fixture_metadata"]

    assert metadata["synthetic"] is True
    assert metadata["unit_codes_are_real_ncs_codes"] is False
    assert "NCS MCP" in metadata["runtime_source_note"]
    assert all(unit.unit_code.startswith("FIXTURE-NOT-NCS-") for unit in request.included_units)

    profile = assemble_job_profile(request)
    round_trip = JobProfile.model_validate(profile.model_dump(mode="json", by_alias=True))
    expected_paths = [
        ("19", "01", "07", "01"),
        ("15", "05", "01", "07"),
        ("05", "02", "01", "04"),
    ]

    assert round_trip == profile
    assert [
        (path.major_code, path.middle_code, path.small_code, path.sub_code)
        for path in profile.scope_selection.classification_paths
    ] == expected_paths
    assert [unit.unit_code for unit in profile.scope_selection.included_units] == [
        item["unit_code"] for item in fixture["units"]
    ]
    assert [bundle.unit.classification_path for bundle in request.unit_evidence_bundles] == [
        "19 > 01 > 07 > 01",
        "15 > 05 > 01 > 07",
        "05 > 02 > 01 > 04",
    ]
    assert len(profile.job_description.duties) == 3
    assert len(profile.job_description.tasks) == 3

    references = {reference.ref_id: reference for reference in profile.references}
    all_ksa = [
        *profile.person_specification.knowledge,
        *profile.person_specification.skills,
        *profile.person_specification.attitudes,
    ]
    assert len(profile.person_specification.knowledge) == 3
    assert len(profile.person_specification.skills) == 3
    assert len(profile.person_specification.attitudes) == 3
    assert all(item.origin == Origin.NCS_DIRECT and item.source_refs for item in all_ksa)
    assert all(
        references[ref_id].source_type.value == "ksa_item"
        and references[ref_id].evidence_grade == EvidenceGrade.DIRECT
        for item in all_ksa
        for ref_id in item.source_refs
    )

    expected_raw_ksa = {
        ksa["ksa_text_raw"]
        for unit in fixture["units"]
        for element in unit["elements"]
        for ksa in element["ksa"]
    }
    assert {item.text for item in all_ksa} == expected_raw_ksa
