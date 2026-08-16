"""Pure, deterministic assembly of a :class:`~ncs_jd.domain.job_profile.JobProfile`.

The assembler consumes only normalized application DTOs.  It does not call the
NCS source, a language model, a clock, or a UUID generator.  All values that
would otherwise make a draft vary between runs are supplied by the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import unicodedata
from uuid import UUID

from ncs_jd.application.ncs_source import OptionalReference, UnitEvidenceBundle
from ncs_jd.domain.job_profile import (
    AdvisoryReferenceItem,
    ClassificationPath,
    Duty,
    EvidenceGrade,
    IncludedUnit,
    JobDescription,
    JobProfile,
    KsaItem,
    KsaType,
    OrganizationStatement,
    Origin,
    PersonSpecification,
    ReferenceRecord,
    Requiredness,
    ReviewFlag,
    ReviewFlagCode,
    ReviewSeverity,
    ScopeSelection,
    SourceSnapshot,
    SourceType,
    SourcedText,
    TargetLevel,
    Task,
)


@dataclass(frozen=True, slots=True)
class OrganizationInput:
    """Organization-owned facts supplied by a person, never inferred from NCS."""

    purpose_supplement: str | None = None
    responsibilities: tuple[str, ...] = ()
    decision_authority: tuple[str, ...] = ()
    kpis: tuple[str, ...] = ()
    collaboration: tuple[str, ...] = ()
    reporting_relationships: tuple[str, ...] = ()
    experience_requirements: tuple[str, ...] = ()
    education_requirements: tuple[str, ...] = ()
    qualification_requirements: tuple[str, ...] = ()
    preference_requirements: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JobProfileAssemblyRequest:
    """Complete deterministic input to :func:`assemble_job_profile`."""

    document_id: UUID
    created_at: datetime
    retrieved_at: datetime
    organization_job_title: str
    classification_paths: tuple[ClassificationPath, ...]
    included_units: tuple[IncludedUnit, ...]
    unit_evidence_bundles: tuple[UnitEvidenceBundle, ...]
    organization_input: OrganizationInput = OrganizationInput()
    excluded_unit_codes: tuple[str, ...] = ()
    excluded_task_terms: tuple[str, ...] = ()
    selected_references: tuple[OptionalReference, ...] = ()
    target_level_input: str | None = None
    mcp_url_label: str = "local-ncs-mcp"
    scope_confirmation_required: bool = False
    scope_match_notes: tuple[str, ...] = ()


# Short aliases make the use-case convenient without expanding application.__init__.
AssemblyRequest = JobProfileAssemblyRequest
OrganizationInputs = OrganizationInput


@dataclass(slots=True)
class _KsaAccumulator:
    text: str
    ksa_type: KsaType
    source_refs: list[str]


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _unit_with_evidence_level(
    unit: IncludedUnit, bundle: UnitEvidenceBundle | None
) -> IncludedUnit:
    if unit.unit_level or bundle is None:
        return unit
    level = _clean_optional_text(bundle.unit.unit_level)
    if level is None:
        return unit
    return unit.model_copy(update={"unit_level": level})


def _clean_texts(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(cleaned for value in values if (cleaned := value.strip()))


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    digest = sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _source_identifier(value: object, *, label: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if len(text) <= 200:
        return text
    return f"{text[:170]}~{sha256(text.encode('utf-8')).hexdigest()[:24]}"


def _preview(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("source evidence text must not be empty")
    return cleaned[:500]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _contains_excluded_term(text: str, normalized_terms: tuple[str, ...]) -> bool:
    normalized = _normalized_text(text)
    return any(term in normalized for term in normalized_terms)


class JobProfileAssembler:
    """Stateless rules engine for the LLM-off draft path."""

    def assemble(self, request: JobProfileAssemblyRequest) -> JobProfile:
        excluded_codes = tuple(dict.fromkeys(code.strip() for code in request.excluded_unit_codes if code.strip()))
        excluded_code_set = set(excluded_codes)
        excluded_term_labels: dict[str, str] = {}
        for term in request.excluded_task_terms:
            cleaned = term.strip()
            normalized = _normalized_text(cleaned)
            if normalized and normalized not in excluded_term_labels:
                excluded_term_labels[normalized] = cleaned
        excluded_terms = tuple(excluded_term_labels)

        included_units = tuple(
            unit for unit in request.included_units if unit.unit_code not in excluded_code_set
        )
        if not included_units:
            raise ValueError("at least one included unit must remain after exclusions")
        included_codes = tuple(unit.unit_code for unit in included_units)
        if len(set(included_codes)) != len(included_codes):
            raise ValueError("included unit codes must be unique")

        bundles_by_code: dict[str, UnitEvidenceBundle] = {}
        for bundle in request.unit_evidence_bundles:
            code = bundle.unit.unit_code
            if code in bundles_by_code:
                raise ValueError(f"duplicate evidence bundle for unit {code}")
            bundles_by_code[code] = bundle

        # ncs_search rows do not carry unit_level; ncs_unit_detail does.
        # Fill the gap from loaded evidence so the document does not print L?.
        included_units = tuple(
            _unit_with_evidence_level(unit, bundles_by_code.get(unit.unit_code))
            for unit in included_units
        )

        references: dict[str, ReferenceRecord] = {}

        def add_reference(reference: ReferenceRecord) -> str:
            existing = references.get(reference.ref_id)
            if existing is not None and existing != reference:
                raise ValueError(f"stable reference id collision: {reference.ref_id}")
            references[reference.ref_id] = reference
            return reference.ref_id

        # Classification paths are selected NCS scope evidence, even though the
        # v1 scope object itself does not carry source_refs.
        for path in request.classification_paths:
            source_id = ".".join((path.major_code, path.middle_code, path.small_code, path.sub_code))
            ref_id = _stable_id("ref-classification", source_id, path.label)
            add_reference(
                ReferenceRecord(
                    ref_id=ref_id,
                    source_system="NCS_MCP",
                    source_type=SourceType.CLASSIFICATION,
                    source_id=source_id,
                    field="classification_path",
                    evidence_grade=EvidenceGrade.DIRECT,
                    text_preview=_preview(path.label),
                    retrieved_at=request.retrieved_at,
                )
            )

        def add_organization_reference(field: str, index: int, text: str) -> str:
            ref_id = _stable_id("ref-organization", field, index, text)
            return add_reference(
                ReferenceRecord(
                    ref_id=ref_id,
                    source_system="organization_input",
                    source_type=SourceType.ORGANIZATION_INPUT,
                    source_id=f"organization:{field}:{index}",
                    field=field,
                    evidence_grade=EvidenceGrade.ORGANIZATION_INPUT,
                    text_preview=_preview(text),
                    retrieved_at=request.retrieved_at,
                )
            )

        job_title = request.organization_job_title.strip()
        job_title_ref = add_organization_reference("organization_job_title", 0, job_title)

        duties: list[Duty] = []
        tasks: list[Task] = []
        purpose_parts: list[str] = []
        purpose_ncs_refs: list[str] = []
        ksa_by_key: dict[tuple[KsaType, str], _KsaAccumulator] = {}
        review_flags: list[ReviewFlag] = []
        if request.scope_confirmation_required:
            review_flags.append(
                ReviewFlag(
                    code=ReviewFlagCode.SCOPE_CONFIRMATION_REQUIRED,
                    severity=ReviewSeverity.WARNING,
                    section="scope_selection",
                    message="공고문 직무수행내역으로 자동 선택한 NCS 범위이므로 최종 검토가 필요합니다.",
                )
            )
        for note in _clean_texts(request.scope_match_notes):
            review_flags.append(
                ReviewFlag(
                    code=ReviewFlagCode.AUTOMATIC_MATCHING_RATIONALE,
                    severity=ReviewSeverity.INFO,
                    section="scope_selection",
                    message=note,
                )
            )

        for included_index, included in enumerate(included_units, start=1):
            bundle = bundles_by_code.get(included.unit_code)
            if bundle is None:
                review_flags.append(
                    ReviewFlag(
                        code=ReviewFlagCode.PARTIAL_UNIT_EVIDENCE,
                        severity=ReviewSeverity.WARNING,
                        section=f"scope_selection.included_units[{included_index - 1}]",
                        message=f"선택 능력단위 {included.unit_code}의 상세 근거가 없어 본문 항목을 생성하지 않았습니다.",
                    )
                )
                continue
            if bundle.unit.unit_code != included.unit_code:
                raise ValueError(f"bundle unit code does not match included unit {included.unit_code}")

            unit = bundle.unit
            unit_source_id = _source_identifier(unit.unit_code, label="unit_code")
            unit_name_is_safe = not _contains_excluded_term(unit.unit_name, excluded_terms)
            unit_definition_is_safe = bool(unit.unit_definition) and not _contains_excluded_term(
                unit.unit_definition or "", excluded_terms
            )
            if unit_definition_is_safe:
                unit_evidence_text = unit.unit_definition
                unit_evidence_field = "unit_definition"
            elif unit_name_is_safe:
                # The unit name is direct NCS evidence and is the smallest safe
                # fallback when the fuller definition contains an excluded task.
                unit_evidence_text = unit.unit_name
                unit_evidence_field = "unit_name"
            else:
                unit_evidence_text = None
                unit_evidence_field = None

            unit_ref: str | None = None
            if unit_evidence_text is not None and unit_evidence_field is not None:
                unit_ref = add_reference(
                    ReferenceRecord(
                        ref_id=_stable_id("ref-unit", unit.unit_code, unit_evidence_field),
                        source_system=bundle.source_audit.source_system,
                        source_type=SourceType.COMPETENCY_UNIT,
                        source_id=unit_source_id,
                        unit_code=unit.unit_code,
                        field=unit_evidence_field,
                        evidence_grade=EvidenceGrade.DIRECT,
                        text_preview=_preview(unit_evidence_text),
                        retrieved_at=request.retrieved_at,
                    )
                )

            if unit.unit_definition and not unit_definition_is_safe:
                review_flags.append(
                    ReviewFlag(
                        code=ReviewFlagCode.WEAK_OR_MISSING_SOURCE_REFERENCE,
                        severity=ReviewSeverity.WARNING,
                        section="job_description.duties",
                        message=(
                            f"능력단위 {unit.unit_code} 정의가 사용자 제외어와 일치하여 "
                            "직무 요약 근거에서 제외되었습니다."
                        ),
                        source_refs=[unit_ref] if unit_ref is not None else [],
                    )
                )

            duty_id: str | None = None
            if unit_ref is not None and unit_name_is_safe:
                duty_id = _stable_id("duty", unit.unit_code)
                duties.append(
                    Duty(
                        id=duty_id,
                        title=unit.unit_name,
                        summary=unit_evidence_text,
                        origin=Origin.NCS_DIRECT,
                        source_refs=[unit_ref],
                    )
                )
            else:
                review_flags.append(
                    ReviewFlag(
                        code=ReviewFlagCode.WEAK_OR_MISSING_SOURCE_REFERENCE,
                        severity=ReviewSeverity.WARNING,
                        section="job_description.duties",
                        message=(
                            f"능력단위 {unit.unit_code}의 직무 요약에 사용할 수 있는 직접 근거가 "
                            "남지 않아 해당 직무와 하위 과업을 생성하지 않았습니다."
                        ),
                        source_refs=[unit_ref] if unit_ref is not None else [],
                    )
                )

            duty_definition_is_safe = bool(unit.duty_definition) and not _contains_excluded_term(
                unit.duty_definition or "", excluded_terms
            )
            if duty_definition_is_safe:
                purpose_ref = add_reference(
                    ReferenceRecord(
                        ref_id=_stable_id("ref-duty-definition", unit.unit_code, unit.duty_definition),
                        source_system=bundle.source_audit.source_system,
                        source_type=SourceType.CLASSIFICATION,
                        source_id=_source_identifier(
                            f"{unit.unit_code}:duty_definition", label="duty definition source id"
                        ),
                        unit_code=unit.unit_code,
                        field="duty_definition",
                        evidence_grade=EvidenceGrade.DIRECT,
                        text_preview=_preview(unit.duty_definition),
                        retrieved_at=request.retrieved_at,
                    )
                )
                purpose_text = unit.duty_definition
            elif unit_ref is not None:
                purpose_ref = unit_ref
                purpose_text = unit_evidence_text
            else:
                purpose_ref = None
                purpose_text = None

            if unit.duty_definition and not duty_definition_is_safe:
                review_flags.append(
                    ReviewFlag(
                        code=ReviewFlagCode.WEAK_OR_MISSING_SOURCE_REFERENCE,
                        severity=ReviewSeverity.WARNING,
                        section="job_description.job_purpose",
                        message=(
                            f"능력단위 {unit.unit_code}의 직무 정의가 사용자 제외어와 일치하여 "
                            "직무 목적 근거에서 제외되었습니다."
                        ),
                        source_refs=[unit_ref] if unit_ref is not None else [],
                    )
                )

            if purpose_text is not None and purpose_ref is not None:
                if _normalized_text(purpose_text) not in {
                    _normalized_text(item) for item in purpose_parts
                }:
                    purpose_parts.append(purpose_text)
                purpose_ncs_refs.append(purpose_ref)
            else:
                review_flags.append(
                    ReviewFlag(
                        code=ReviewFlagCode.WEAK_OR_MISSING_SOURCE_REFERENCE,
                        severity=ReviewSeverity.WARNING,
                        section="job_description.job_purpose",
                        message=(
                            f"능력단위 {unit.unit_code}의 직무 목적에 사용할 수 있는 직접 근거가 "
                            "남지 않아 해당 목적 문장을 생성하지 않았습니다."
                        ),
                    )
                )

            if duty_id is None:
                # Tasks require a generated duty. Do not invent a placeholder
                # relationship after all safe duty evidence was filtered out.
                continue

            for element in bundle.elements:
                # Excluding a task removes the entire element-derived slice,
                # including criteria and KSA, so excluded work cannot leak back
                # through provenance or the person specification.
                if _contains_excluded_term(element.element_name, excluded_terms):
                    continue

                element_source_id = _source_identifier(element.element_id, label="element_id")
                element_ref = add_reference(
                    ReferenceRecord(
                        ref_id=_stable_id("ref-element", unit.unit_code, element.element_id),
                        source_system=bundle.source_audit.source_system,
                        source_type=SourceType.COMPETENCY_ELEMENT,
                        source_id=element_source_id,
                        unit_code=unit.unit_code,
                        element_id=element_source_id,
                        field="element_name",
                        evidence_grade=EvidenceGrade.DIRECT,
                        text_preview=_preview(element.element_name),
                        retrieved_at=request.retrieved_at,
                    )
                )
                task_refs = [element_ref]
                for criterion in element.criteria:
                    criterion_source_id = _source_identifier(criterion.criteria_id, label="criteria_id")
                    task_refs.append(
                        add_reference(
                            ReferenceRecord(
                                ref_id=_stable_id(
                                    "ref-criterion",
                                    unit.unit_code,
                                    element.element_id,
                                    criterion.criteria_id,
                                    criterion.criteria_text_raw,
                                ),
                                source_system=bundle.source_audit.source_system,
                                source_type=SourceType.PERFORMANCE_CRITERION,
                                source_id=criterion_source_id,
                                unit_code=unit.unit_code,
                                element_id=element_source_id,
                                field="criteria_text_raw",
                                evidence_grade=EvidenceGrade.DIRECT,
                                text_preview=_preview(criterion.criteria_text_raw),
                                retrieved_at=request.retrieved_at,
                            )
                        )
                    )

                tasks.append(
                    Task(
                        id=_stable_id("task", unit.unit_code, element.element_id),
                        duty_id=duty_id,
                        title=element.element_name,
                        # The full performance criteria remain in references.
                        # Repeating the element name is the smallest faithful
                        # deterministic description available in this DTO.
                        description=element.element_name,
                        origin=Origin.NCS_DIRECT,
                        source_refs=_unique(task_refs),
                    )
                )

                for ksa in element.ksa:
                    try:
                        ksa_type = KsaType(ksa.ksa_type)
                    except ValueError:
                        review_flags.append(
                            ReviewFlag(
                                code=ReviewFlagCode.PARTIAL_UNIT_EVIDENCE,
                                severity=ReviewSeverity.WARNING,
                                section="person_specification",
                                message=(
                                    f"선택 능력단위 {unit.unit_code}에서 지원하지 않는 KSA 유형이 제외되었습니다."
                                ),
                                source_refs=[unit_ref],
                            )
                        )
                        continue
                    raw_text = ksa.ksa_text_raw
                    normalized = _normalized_text(raw_text)
                    if not normalized:
                        review_flags.append(
                            ReviewFlag(
                                code=ReviewFlagCode.PARTIAL_UNIT_EVIDENCE,
                                severity=ReviewSeverity.WARNING,
                                section="person_specification",
                                message=f"선택 능력단위 {unit.unit_code}의 빈 KSA 원문이 제외되었습니다.",
                                source_refs=[unit_ref],
                            )
                        )
                        continue
                    ksa_source_id = _source_identifier(ksa.ksa_id, label="ksa_id")
                    ksa_ref = add_reference(
                        ReferenceRecord(
                            ref_id=_stable_id(
                                "ref-ksa",
                                unit.unit_code,
                                element.element_id,
                                ksa.ksa_id,
                                ksa.ksa_type,
                                raw_text,
                            ),
                            source_system=bundle.source_audit.source_system,
                            source_type=SourceType.KSA_ITEM,
                            source_id=ksa_source_id,
                            unit_code=unit.unit_code,
                            element_id=element_source_id,
                            field="ksa_text_raw",
                            evidence_grade=EvidenceGrade.DIRECT,
                            text_preview=_preview(raw_text),
                            retrieved_at=request.retrieved_at,
                        )
                    )
                    key = (ksa_type, normalized)
                    accumulator = ksa_by_key.get(key)
                    if accumulator is None:
                        ksa_by_key[key] = _KsaAccumulator(raw_text, ksa_type, [ksa_ref])
                    elif ksa_ref not in accumulator.source_refs:
                        accumulator.source_refs.append(ksa_ref)

            if (
                bundle.warnings
                or not bundle.elements
                or any(not element.criteria or not element.ksa for element in bundle.elements)
            ):
                warning_messages = [warning.message for warning in bundle.warnings]
                detail = " ".join(warning_messages) if warning_messages else "요소 근거가 비어 있습니다."
                review_flags.append(
                    ReviewFlag(
                        code=ReviewFlagCode.PARTIAL_UNIT_EVIDENCE,
                        severity=ReviewSeverity.WARNING,
                        section=f"evidence.{unit.unit_code}",
                        message=f"선택 능력단위 {unit.unit_code}의 근거가 불완전합니다. {detail}",
                        source_refs=[unit_ref],
                    )
                )

        organization = request.organization_input
        purpose_supplement = _clean_optional_text(organization.purpose_supplement)
        purpose_refs = _unique(purpose_ncs_refs)
        if purpose_supplement is not None:
            purpose_org_ref = add_organization_reference("purpose_supplement", 0, purpose_supplement)
            if purpose_parts:
                job_purpose = SourcedText(
                    text=" ".join((*purpose_parts, purpose_supplement)),
                    origin=Origin.MIXED,
                    source_refs=[*purpose_refs, purpose_org_ref],
                )
            else:
                job_purpose = SourcedText(
                    text=purpose_supplement,
                    origin=Origin.ORGANIZATION_INPUT,
                    source_refs=[purpose_org_ref],
                )
        elif purpose_parts:
            job_purpose = SourcedText(
                text=" ".join(purpose_parts),
                origin=Origin.NCS_DIRECT,
                source_refs=purpose_refs,
            )
            review_flags.append(
                ReviewFlag(
                    code=ReviewFlagCode.ORGANIZATION_PURPOSE_MISSING,
                    severity=ReviewSeverity.INFO,
                    section="job_description.job_purpose",
                    message="조직의 직무 목적 보완 입력이 없어 선택 NCS 근거만 사용했습니다.",
                    source_refs=purpose_refs,
                )
            )
        else:
            raise ValueError("job purpose requires organization input or at least one selected evidence bundle")

        def organization_statements(field: str, values: tuple[str, ...]) -> list[OrganizationStatement]:
            return [
                OrganizationStatement(
                    text=text,
                    origin=Origin.ORGANIZATION_INPUT,
                    source_refs=[add_organization_reference(field, index, text)],
                )
                for index, text in enumerate(_clean_texts(values))
            ]

        responsibilities = organization_statements("responsibilities", organization.responsibilities)
        decision_authority = organization_statements("decision_authority", organization.decision_authority)
        kpis = organization_statements("kpis", organization.kpis)
        collaboration = organization_statements("collaboration", organization.collaboration)
        reporting_relationships = organization_statements(
            "reporting_relationships", organization.reporting_relationships
        )
        experience_requirements = organization_statements(
            "experience_requirements", organization.experience_requirements
        )
        education_requirements = organization_statements(
            "education_requirements", organization.education_requirements
        )
        qualification_requirements = organization_statements(
            "qualification_requirements", organization.qualification_requirements
        )
        preference_requirements = organization_statements(
            "preference_requirements", organization.preference_requirements
        )

        missing_organization_fields = (
            (
                responsibilities,
                ReviewFlagCode.ORGANIZATION_RESPONSIBILITY_MISSING,
                "job_description.responsibilities",
                "조직의 핵심 책임 입력이 없어 비워 두었습니다.",
            ),
            (
                decision_authority,
                ReviewFlagCode.AUTHORITY_MISSING,
                "job_description.decision_authority",
                "조직의 의사결정 권한 입력이 없어 비워 두었습니다.",
            ),
            (
                kpis,
                ReviewFlagCode.ORGANIZATION_KPI_MISSING,
                "job_description.kpis",
                "조직의 KPI 입력이 없어 비워 두었습니다.",
            ),
            (
                reporting_relationships,
                ReviewFlagCode.REPORTING_RELATIONSHIP_MISSING,
                "job_description.reporting_relationships",
                "조직의 보고관계 입력이 없어 비워 두었습니다.",
            ),
        )
        for values, code, section, message in missing_organization_fields:
            if not values:
                review_flags.append(
                    ReviewFlag(code=code, severity=ReviewSeverity.INFO, section=section, message=message)
                )

        ksa_items = [
            KsaItem(
                id=_stable_id("ksa", accumulator.ksa_type.value, normalized),
                text=accumulator.text,
                type=accumulator.ksa_type,
                origin=Origin.NCS_DIRECT,
                requiredness=Requiredness.REVIEW_REQUIRED,
                source_refs=accumulator.source_refs,
            )
            for (ksa_type, normalized), accumulator in ksa_by_key.items()
        ]
        knowledge = [item for item in ksa_items if item.type == KsaType.KNOWLEDGE]
        skills = [item for item in ksa_items if item.type == KsaType.SKILL]
        attitudes = [item for item in ksa_items if item.type == KsaType.ATTITUDE]

        qualification_references: list[AdvisoryReferenceItem] = []
        job_base_references: list[AdvisoryReferenceItem] = []
        seen_advisory: set[tuple[str, str, str]] = set()
        for optional in request.selected_references:
            if optional.unit_code not in included_codes or optional.unit_code in excluded_code_set:
                continue
            if optional.kind not in {"qualification", "job_base"}:
                # JobProfile v1 has no career-path output field.  It must not be
                # silently promoted into experience requirements.
                continue
            advisory_key = (optional.kind, optional.unit_code, optional.reference_id)
            if advisory_key in seen_advisory:
                continue
            seen_advisory.add(advisory_key)
            source_type = (
                SourceType.QUALIFICATION
                if optional.kind == "qualification"
                else SourceType.JOB_BASE_COMPETENCY
            )
            optional_ref = add_reference(
                ReferenceRecord(
                    ref_id=_stable_id(
                        "ref-reference",
                        optional.kind,
                        optional.unit_code,
                        optional.reference_id,
                    ),
                    source_system="NCS_MCP",
                    source_type=source_type,
                    source_id=_source_identifier(optional.reference_id, label="optional reference id"),
                    unit_code=optional.unit_code,
                    field="text_raw",
                    evidence_grade=EvidenceGrade.REFERENCE,
                    text_preview=_preview(optional.text_raw),
                    retrieved_at=request.retrieved_at,
                )
            )
            item = AdvisoryReferenceItem(
                id=_stable_id(
                    "advisory",
                    optional.kind,
                    optional.unit_code,
                    optional.reference_id,
                ),
                text=optional.text_raw,
                origin=Origin.NCS_REFERENCE,
                requiredness="reference_only",
                source_refs=[optional_ref],
            )
            if optional.kind == "qualification":
                qualification_references.append(item)
            else:
                job_base_references.append(item)

        if not qualification_references:
            review_flags.append(
                ReviewFlag(
                    code=ReviewFlagCode.QUALIFICATION_EVIDENCE_INCOMPLETE,
                    severity=ReviewSeverity.INFO,
                    section="person_specification.qualification_references",
                    message="선택된 자격 참고 근거가 없어 비워 두었습니다.",
                )
            )
        review_flags.append(
            ReviewFlag(
                code=ReviewFlagCode.JOB_BASE_EVIDENCE_REFERENCE_ONLY,
                severity=ReviewSeverity.INFO,
                section="person_specification.job_base_references",
                message=(
                    "직업기초능력 자료는 참고 등급으로만 제공됩니다."
                    if job_base_references
                    else "선택된 직업기초능력 참고 근거가 없어 비워 두었습니다."
                ),
                source_refs=[ref for item in job_base_references for ref in item.source_refs],
            )
        )
        if ksa_items:
            review_flags.append(
                ReviewFlag(
                    code=ReviewFlagCode.KSA_REQUIREDNESS_REVIEW_REQUIRED,
                    severity=ReviewSeverity.INFO,
                    section="person_specification",
                    message="NCS 원문 KSA는 조직의 필수요건으로 확정되지 않았으며 검토가 필요합니다.",
                    source_refs=_unique([ref for item in ksa_items for ref in item.source_refs]),
                )
            )

        ncs_levels = _unique(
            [cleaned for unit in included_units if (cleaned := _clean_optional_text(unit.unit_level))]
        )
        target_level_input = _clean_optional_text(request.target_level_input)
        if target_level_input is not None and ncs_levels:
            review_flags.append(
                ReviewFlag(
                    code=ReviewFlagCode.NCS_LEVEL_NOT_EQUAL_TO_ORG_GRADE,
                    severity=ReviewSeverity.INFO,
                    section="person_specification.target_level",
                    message="NCS 수준 참고값을 조직의 직급 또는 숙련수준과 동일시하지 않았습니다.",
                )
            )

        review_flags.append(
            ReviewFlag(
                code=ReviewFlagCode.LLM_REWRITE_NOT_APPLIED,
                severity=ReviewSeverity.INFO,
                section="job_profile",
                message="LLM 재서술을 적용하지 않은 결정적 초안입니다.",
            )
        )

        scope = ScopeSelection(
            organization_job_title=job_title,
            classification_paths=list(request.classification_paths),
            included_units=list(included_units),
            excluded_unit_codes=list(excluded_codes),
            excluded_task_terms=list(excluded_term_labels.values()),
            target_level_input=target_level_input,
        )

        return JobProfile(
            schema="ncs_job_profile_v1",
            document_id=request.document_id,
            document_status="draft",
            created_at=request.created_at,
            updated_at=request.created_at,
            source_snapshot=SourceSnapshot(
                mcp_url_label=request.mcp_url_label,
                retrieved_at=request.retrieved_at,
                selected_unit_codes=list(included_codes),
            ),
            scope_selection=scope,
            job_description=JobDescription(
                job_title=OrganizationStatement(
                    text=job_title,
                    origin=Origin.ORGANIZATION_INPUT,
                    source_refs=[job_title_ref],
                ),
                job_purpose=job_purpose,
                duties=duties,
                tasks=tasks,
                responsibilities=responsibilities,
                decision_authority=decision_authority,
                kpis=kpis,
                collaboration=collaboration,
                reporting_relationships=reporting_relationships,
            ),
            person_specification=PersonSpecification(
                target_level=TargetLevel(
                    organization_value=target_level_input,
                    ncs_level_references=ncs_levels,
                    mapping_status="not_equated",
                ),
                knowledge=knowledge,
                skills=skills,
                attitudes=attitudes,
                experience_requirements=experience_requirements,
                education_requirements=education_requirements,
                qualification_requirements=qualification_requirements,
                preference_requirements=preference_requirements,
                qualification_references=qualification_references,
                job_base_references=job_base_references,
            ),
            references=list(references.values()),
            review_flags=review_flags,
        )


def assemble_job_profile(request: JobProfileAssemblyRequest) -> JobProfile:
    """Assemble a profile with no I/O and no ambient time or randomness."""

    return JobProfileAssembler().assemble(request)


__all__ = [
    "AssemblyRequest",
    "JobProfileAssembler",
    "JobProfileAssemblyRequest",
    "OrganizationInput",
    "OrganizationInputs",
    "assemble_job_profile",
]
