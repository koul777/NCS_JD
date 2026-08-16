from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import UUID

from ncs_jd.application.document_renderer import DRAFT_DISCLAIMER, job_profile_to_markdown
from ncs_jd.application.job_profile_assembler import (
    JobProfileAssemblyRequest,
    OrganizationInput,
    assemble_job_profile,
)
from ncs_jd.application.ncs_source import (
    EvidenceCriterion,
    EvidenceElement,
    EvidenceKsa,
    EvidenceUnit,
    SourceAudit,
    UnitEvidenceBundle,
)
from ncs_jd.domain.job_profile import (
    ClassificationPath,
    EvidenceGrade,
    IncludedUnit,
    JobProfile,
    Origin,
)


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "office_asset_management_synthetic.json"
NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def _load_profile() -> tuple[dict[str, object], JobProfile]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    included: list[IncludedUnit] = []
    bundles: list[UnitEvidenceBundle] = []
    for unit in payload["units"]:
        included.append(
            IncludedUnit(
                unit_code=unit["unit_code"],
                unit_name=unit["unit_name"],
                unit_level=unit["unit_level"],
                selection_reason="synthetic fixture user selection",
            )
        )
        bundles.append(
            UnitEvidenceBundle(
                unit=EvidenceUnit(
                    unit_code=unit["unit_code"],
                    unit_name=unit["unit_name"],
                    unit_level=unit["unit_level"],
                    unit_definition=unit["unit_definition"],
                    classification_path=unit["classification_path"],
                    duty_definition=unit["duty_definition"],
                ),
                elements=tuple(
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
                    for element in unit["elements"]
                ),
                source_audit=SourceAudit("NCS_MCP", NOW, unit["unit_code"]),
            )
        )

    concrete_duties = tuple(payload["duties"][:-1])
    request = JobProfileAssemblyRequest(
        document_id=UUID("af06e0e5-71ef-4b49-a0c5-7c65df64d93d"),
        created_at=NOW,
        retrieved_at=NOW,
        organization_job_title=payload["job_title"],
        classification_paths=tuple(
            ClassificationPath.model_validate(item) for item in payload["classification_paths"]
        ),
        included_units=tuple(included),
        unit_evidence_bundles=tuple(bundles),
        organization_input=OrganizationInput(
            purpose_supplement=payload["recruitment_reason"],
            responsibilities=tuple(payload["duties"]),
            qualification_requirements=tuple(payload["qualification_requirements"]),
            preference_requirements=tuple(payload["preference_requirements"]),
        ),
        scope_confirmation_required=True,
        scope_match_notes=(
            "합성 회귀 fixture이며 실행 시 읽기 전용 NCS MCP 근거로 교체해야 합니다.",
        ),
    )
    profile = assemble_job_profile(request)
    assert concrete_duties
    return payload, profile


def test_scenario_job_keeps_announcement_conditions_and_ncs_provenance_separate() -> None:
    fixture, profile = _load_profile()
    assert fixture["fixture_metadata"]["synthetic"] is True
    assert fixture["fixture_metadata"]["unit_codes_are_real_ncs_codes"] is False
    assert all(unit.unit_code.startswith("FIXTURE-NOT-NCS-") for unit in profile.scope_selection.included_units)

    generic_duty = fixture["duties"][-1]
    assert generic_duty in [item.text for item in profile.job_description.responsibilities]
    assert generic_duty not in " ".join(
        f"{item.title} {item.description}" for item in profile.job_description.tasks
    )

    specification = profile.person_specification
    assert [item.text for item in specification.qualification_requirements] == fixture[
        "qualification_requirements"
    ]
    assert [item.text for item in specification.preference_requirements] == fixture[
        "preference_requirements"
    ]
    references = {item.ref_id: item for item in profile.references}
    announcement_items = (
        *specification.qualification_requirements,
        *specification.preference_requirements,
    )
    assert all(item.origin == Origin.ORGANIZATION_INPUT for item in announcement_items)
    assert all(
        references[ref_id].evidence_grade == EvidenceGrade.ORGANIZATION_INPUT
        for item in announcement_items
        for ref_id in item.source_refs
    )

    ncs_items = (
        *profile.job_description.duties,
        *profile.job_description.tasks,
        *specification.knowledge,
        *specification.skills,
        *specification.attitudes,
    )
    assert all(item.origin == Origin.NCS_DIRECT and item.source_refs for item in ncs_items)
    assert JobProfile.model_validate(profile.model_dump(mode="json", by_alias=True)) == profile


def test_scenario_job_standard_document_contains_explicit_conditions_and_draft_notice() -> None:
    fixture, profile = _load_profile()
    markdown = job_profile_to_markdown(profile)

    assert fixture["recruitment_reason"] in markdown
    assert "공고 자격조건" in markdown
    assert fixture["qualification_requirements"][1] in markdown
    assert "공고 우대사항" in markdown
    assert fixture["preference_requirements"][0] in markdown
    assert DRAFT_DISCLAIMER in markdown
    assert "공식 승인" in markdown
