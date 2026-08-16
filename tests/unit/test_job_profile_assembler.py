from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

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
    NcsSourceWarning,
    OptionalReference,
    SourceAudit,
    UnitEvidenceBundle,
)
from ncs_jd.domain.job_profile import (
    ClassificationPath,
    EvidenceGrade,
    IncludedUnit,
    JobProfile,
    Origin,
    Requiredness,
    ReviewFlagCode,
)


NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")
UNIT_CODE = "FIXTURE-UNIT-INCLUDED"
EXCLUDED_UNIT_CODE = "FIXTURE-UNIT-EXCLUDED"


def _bundle(
    *,
    unit_code: str = UNIT_CODE,
    unit_name: str = "시설 점검",
    unit_definition: str | None = None,
    duty_definition: str | None = None,
    warnings: tuple[NcsSourceWarning, ...] = (),
) -> UnitEvidenceBundle:
    return UnitEvidenceBundle(
        unit=EvidenceUnit(
            unit_code=unit_code,
            unit_name=unit_name,
            unit_level="4",
            unit_definition=unit_definition or f"{unit_name} 능력",
            classification_path="fixture classification",
            duty_definition=duty_definition or "시설 상태를 확인한다.",
        ),
        elements=(
            EvidenceElement(
                element_id=f"{unit_code}-E1",
                element_name="점검 계획 수립하기",
                criteria=(
                    EvidenceCriterion(f"{unit_code}-C1", "점검 순서를 확인할 수 있다."),
                ),
                ksa=(
                    EvidenceKsa(f"{unit_code}-K1", "knowledge", "점검   절차 지식"),
                    EvidenceKsa(f"{unit_code}-K2", "knowledge", "점검 절차 지식"),
                    EvidenceKsa(f"{unit_code}-S1", "skill", "점검 도구 사용 기술"),
                    EvidenceKsa(f"{unit_code}-A1", "attitude", "안전 확인 태도"),
                ),
            ),
            EvidenceElement(
                element_id=f"{unit_code}-E2",
                element_name="교육 운영하기",
                criteria=(EvidenceCriterion(f"{unit_code}-C2", "교육을 운영할 수 있다."),),
                ksa=(EvidenceKsa(f"{unit_code}-K3", "knowledge", "교육 운영 지식"),),
            ),
        ),
        source_audit=SourceAudit("NCS_MCP", NOW, unit_code),
        warnings=warnings,
    )


def _request(
    *,
    organization_input: OrganizationInput = OrganizationInput(),
    selected_references: tuple[OptionalReference, ...] = (),
) -> JobProfileAssemblyRequest:
    return JobProfileAssemblyRequest(
        document_id=DOCUMENT_ID,
        created_at=NOW,
        retrieved_at=NOW,
        organization_job_title="시설 유지보수 담당자",
        classification_paths=(
            ClassificationPath(
                major_code="FIXTURE-MAJOR",
                middle_code="FIXTURE-MIDDLE",
                small_code="FIXTURE-SMALL",
                sub_code="FIXTURE-SUB",
                label="합성 테스트 분류",
            ),
        ),
        included_units=(
            IncludedUnit(
                unit_code=UNIT_CODE,
                unit_name="시설 점검",
                unit_level="4",
                selection_reason="사용자 선택",
            ),
            IncludedUnit(
                unit_code=EXCLUDED_UNIT_CODE,
                unit_name="제외 업무",
                unit_level="3",
                selection_reason="사용자 선택 후 제외",
            ),
        ),
        excluded_unit_codes=(EXCLUDED_UNIT_CODE,),
        excluded_task_terms=("교육",),
        unit_evidence_bundles=(
            _bundle(
                warnings=(
                    NcsSourceWarning(
                        "partial_unit_evidence",
                        "일부 근거가 누락된 synthetic bundle입니다.",
                        UNIT_CODE,
                    ),
                )
            ),
            _bundle(unit_code=EXCLUDED_UNIT_CODE, unit_name="제외 업무"),
        ),
        organization_input=organization_input,
        selected_references=selected_references,
        target_level_input="조직 4단계",
    )


def _all_direct_items(profile: JobProfile):
    yield profile.job_description.job_purpose
    yield from profile.job_description.duties
    yield from profile.job_description.tasks
    yield from profile.person_specification.knowledge
    yield from profile.person_specification.skills
    yield from profile.person_specification.attitudes


def test_roundtrip_validation_and_same_input_dump_are_deterministic() -> None:
    first = assemble_job_profile(_request())
    second = assemble_job_profile(_request())
    first_dump = first.model_dump(mode="json", by_alias=True)

    assert first_dump == second.model_dump(mode="json", by_alias=True)
    assert JobProfile.model_validate(first_dump) == first
    assert JobProfile.model_validate_json(first.model_dump_json(by_alias=True)) == first
    assert first.updated_at == first.created_at == NOW


def test_automatic_scope_is_disclosed_with_matching_rationale() -> None:
    profile = assemble_job_profile(
        replace(
            _request(),
            scope_confirmation_required=True,
            scope_match_notes=("세분류 01. 내선공사를 공고 업무와의 일치로 선정했습니다.",),
        )
    )
    flags = {flag.code: flag.message for flag in profile.review_flags}

    assert ReviewFlagCode.SCOPE_CONFIRMATION_REQUIRED in flags
    assert ReviewFlagCode.AUTOMATIC_MATCHING_RATIONALE in flags
    assert "내선공사" in flags[ReviewFlagCode.AUTOMATIC_MATCHING_RATIONALE]


def test_every_ncs_direct_item_has_existing_direct_references() -> None:
    profile = assemble_job_profile(_request())
    references = {reference.ref_id: reference for reference in profile.references}

    for item in _all_direct_items(profile):
        if item.origin != Origin.NCS_DIRECT:
            continue
        assert item.source_refs
        assert all(ref_id in references for ref_id in item.source_refs)
        assert all(references[ref_id].evidence_grade == EvidenceGrade.DIRECT for ref_id in item.source_refs)

    task = profile.job_description.tasks[0]
    assert any(references[ref_id].source_type.value == "performance_criterion" for ref_id in task.source_refs)
    assert "점검 순서를 확인할 수 있다" not in task.description


def test_raw_ksa_is_deduplicated_by_type_and_normalized_text() -> None:
    profile = assemble_job_profile(_request())

    assert [item.text for item in profile.person_specification.knowledge] == ["점검   절차 지식"]
    assert len(profile.person_specification.knowledge[0].source_refs) == 2
    assert profile.person_specification.knowledge[0].requiredness == Requiredness.REVIEW_REQUIRED
    assert [item.text for item in profile.person_specification.skills] == ["점검 도구 사용 기술"]
    assert [item.text for item in profile.person_specification.attitudes] == ["안전 확인 태도"]


def test_excluded_unit_and_task_slice_do_not_reappear_in_generated_content() -> None:
    profile = assemble_job_profile(_request())

    assert [unit.unit_code for unit in profile.scope_selection.included_units] == [UNIT_CODE]
    assert all(duty.title != "제외 업무" for duty in profile.job_description.duties)
    assert all("교육" not in f"{task.title} {task.description}" for task in profile.job_description.tasks)
    assert all("교육" not in item.text for item in profile.person_specification.knowledge)
    assert all(reference.unit_code != EXCLUDED_UNIT_CODE for reference in profile.references)
    assert all("교육" not in reference.text_preview for reference in profile.references)


def test_excluded_term_is_filtered_from_unit_and_duty_definitions() -> None:
    request = _request()
    unsafe_bundle = _bundle(
        unit_name="설비 점검",
        unit_definition="설비 점검과 교육 운영을 수행한다.",
        duty_definition="교육 운영을 포함한 설비 업무를 수행한다.",
    )
    profile = assemble_job_profile(
        replace(
            request,
            unit_evidence_bundles=(unsafe_bundle, request.unit_evidence_bundles[1]),
        )
    )

    assert [duty.summary for duty in profile.job_description.duties] == ["설비 점검"]
    assert "교육" not in profile.job_description.job_purpose.text
    assert all("교육" not in reference.text_preview for reference in profile.references)
    filtered_sections = {
        flag.section
        for flag in profile.review_flags
        if flag.code == ReviewFlagCode.WEAK_OR_MISSING_SOURCE_REFERENCE
    }
    assert "job_description.duties" in filtered_sections
    assert "job_description.job_purpose" in filtered_sections


def test_no_duty_or_tasks_are_created_when_all_duty_evidence_is_excluded() -> None:
    request = _request(
        organization_input=OrganizationInput(purpose_supplement="조직이 확인한 안전 목적")
    )
    unsafe_bundle = _bundle(
        unit_name="교육 운영",
        unit_definition="교육 운영 능력",
        duty_definition="교육 운영을 수행한다.",
    )
    profile = assemble_job_profile(
        replace(
            request,
            included_units=(
                IncludedUnit(
                    unit_code=UNIT_CODE,
                    unit_name="교육 운영",
                    unit_level="4",
                    selection_reason="사용자 선택",
                ),
            ),
            excluded_unit_codes=(),
            unit_evidence_bundles=(unsafe_bundle,),
        )
    )

    assert profile.job_description.duties == []
    assert profile.job_description.tasks == []
    assert profile.person_specification.knowledge == []
    assert profile.job_description.job_purpose.text == "조직이 확인한 안전 목적"
    assert any(
        flag.code == ReviewFlagCode.WEAK_OR_MISSING_SOURCE_REFERENCE
        and flag.section == "job_description.duties"
        for flag in profile.review_flags
    )


def test_missing_search_unit_level_is_filled_from_unit_detail_evidence() -> None:
    request = replace(
        _request(),
        included_units=(
            IncludedUnit(
                unit_code=UNIT_CODE,
                unit_name="시설 점검",
                unit_level=None,
                selection_reason="사용자 선택",
            ),
        ),
        excluded_unit_codes=(),
        unit_evidence_bundles=(_bundle(),),
    )

    profile = assemble_job_profile(request)

    assert profile.scope_selection.included_units[0].unit_level == "4"


def test_missing_organization_fields_and_partial_evidence_are_flagged() -> None:
    profile = assemble_job_profile(_request())
    codes = [flag.code for flag in profile.review_flags]

    assert ReviewFlagCode.ORGANIZATION_PURPOSE_MISSING in codes
    assert ReviewFlagCode.ORGANIZATION_RESPONSIBILITY_MISSING in codes
    assert ReviewFlagCode.ORGANIZATION_KPI_MISSING in codes
    assert ReviewFlagCode.AUTHORITY_MISSING in codes
    assert ReviewFlagCode.REPORTING_RELATIONSHIP_MISSING in codes
    assert ReviewFlagCode.PARTIAL_UNIT_EVIDENCE in codes
    assert ReviewFlagCode.QUALIFICATION_EVIDENCE_INCOMPLETE in codes
    assert ReviewFlagCode.JOB_BASE_EVIDENCE_REFERENCE_ONLY in codes


def test_no_education_experience_qualification_or_job_base_is_invented() -> None:
    profile = assemble_job_profile(_request())
    specification = profile.person_specification

    assert specification.education_requirements == []
    assert specification.experience_requirements == []
    assert specification.qualification_references == []
    assert specification.job_base_references == []
    assert specification.target_level.mapping_status == "not_equated"


def test_only_actual_organization_input_populates_organization_fields() -> None:
    organization = OrganizationInput(
        purpose_supplement="기관 설비의 운영 연속성을 지원한다.",
        responsibilities=("점검 결과를 기록한다.",),
        decision_authority=("점검 순서를 조정한다.",),
        kpis=("점검 계획 이행률",),
        collaboration=("시설 운영 부서",),
        reporting_relationships=("시설팀장에게 보고",),
        experience_requirements=("사용자가 제공한 현장 경험",),
        education_requirements=("사용자가 제공한 교육 이수",),
        qualification_requirements=("공고에 명시된 상담 자격 조건",),
        preference_requirements=("공고에 명시된 외국어 우대",),
    )
    profile = assemble_job_profile(_request(organization_input=organization))

    assert profile.job_description.job_purpose.origin == Origin.MIXED
    assert profile.job_description.responsibilities[0].text == organization.responsibilities[0]
    assert profile.job_description.kpis[0].origin == Origin.ORGANIZATION_INPUT
    assert profile.person_specification.experience_requirements[0].text == organization.experience_requirements[0]
    assert profile.person_specification.education_requirements[0].text == organization.education_requirements[0]
    assert profile.person_specification.qualification_requirements[0].text == organization.qualification_requirements[0]
    assert profile.person_specification.preference_requirements[0].text == organization.preference_requirements[0]
    references = {item.ref_id: item for item in profile.references}
    assert all(
        references[ref_id].evidence_grade == EvidenceGrade.ORGANIZATION_INPUT
        for item in (
            *profile.person_specification.qualification_requirements,
            *profile.person_specification.preference_requirements,
        )
        for ref_id in item.source_refs
    )


def test_selected_advisory_material_stays_reference_only_and_career_is_not_experience() -> None:
    selected = (
        OptionalReference("Q-FIXTURE", UNIT_CODE, "qualification", "합성 자격 참고자료"),
        OptionalReference("JB-FIXTURE", UNIT_CODE, "job_base", "합성 직업기초능력 참고자료"),
        OptionalReference("CP-FIXTURE", UNIT_CODE, "career_path", "합성 경력경로 참고자료"),
    )
    profile = assemble_job_profile(_request(selected_references=selected))
    specification = profile.person_specification
    references = {item.ref_id: item for item in profile.references}

    assert [item.requiredness for item in specification.qualification_references] == ["reference_only"]
    assert [item.requiredness for item in specification.job_base_references] == ["reference_only"]
    assert specification.experience_requirements == []
    advisory = [*specification.qualification_references, *specification.job_base_references]
    assert all(references[ref_id].evidence_grade == EvidenceGrade.REFERENCE for item in advisory for ref_id in item.source_refs)
    assert "합성 경력경로 참고자료" not in profile.model_dump_json()


def test_assembler_never_emits_an_approved_or_ncs_review_status() -> None:
    profile = assemble_job_profile(_request())
    dumped = profile.model_dump(mode="json", by_alias=True)

    assert profile.document_status == "draft"

    def walk(value: object):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)
        else:
            yield value

    forbidden = {"reviewed", "accepted", "human_reviewed", "published"}
    assert forbidden.isdisjoint(walk(dumped))
