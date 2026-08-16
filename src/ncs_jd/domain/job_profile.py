"""Pydantic contract for the deterministic ``ncs_job_profile_v1`` artifact."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ContractModel(BaseModel):
    """Closed, immutable model used at every public contract boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class Origin(str, Enum):
    NCS_DIRECT = "ncs_direct"
    NCS_SUPPORTING = "ncs_supporting"
    NCS_REFERENCE = "ncs_reference"
    ORGANIZATION_INPUT = "organization_input"
    MIXED = "mixed"


class SourceType(str, Enum):
    CLASSIFICATION = "classification"
    COMPETENCY_UNIT = "competency_unit"
    COMPETENCY_ELEMENT = "competency_element"
    PERFORMANCE_CRITERION = "performance_criterion"
    KSA_ITEM = "ksa_item"
    CAREER_PATH = "career_path"
    QUALIFICATION = "qualification"
    JOB_BASE_COMPETENCY = "job_base_competency"
    ORGANIZATION_INPUT = "organization_input"


class EvidenceGrade(str, Enum):
    DIRECT = "direct"
    SUPPORTING = "supporting"
    REFERENCE = "reference"
    ORGANIZATION_INPUT = "organization_input"


class KsaType(str, Enum):
    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    ATTITUDE = "attitude"


class Requiredness(str, Enum):
    REVIEW_REQUIRED = "review_required"
    REQUIRED = "required"
    PREFERRED = "preferred"


class ReviewSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ReviewFlagCode(str, Enum):
    SCOPE_CONFIRMATION_REQUIRED = "scope_confirmation_required"
    AUTOMATIC_MATCHING_RATIONALE = "automatic_matching_rationale"
    ORGANIZATION_PURPOSE_MISSING = "organization_purpose_missing"
    ORGANIZATION_RESPONSIBILITY_MISSING = "organization_responsibility_missing"
    ORGANIZATION_KPI_MISSING = "organization_kpi_missing"
    AUTHORITY_MISSING = "authority_missing"
    REPORTING_RELATIONSHIP_MISSING = "reporting_relationship_missing"
    NCS_LEVEL_NOT_EQUAL_TO_ORG_GRADE = "ncs_level_not_equal_to_org_grade"
    QUALIFICATION_EVIDENCE_INCOMPLETE = "qualification_evidence_incomplete"
    JOB_BASE_EVIDENCE_REFERENCE_ONLY = "job_base_evidence_reference_only"
    KSA_REQUIREDNESS_REVIEW_REQUIRED = "ksa_requiredness_review_required"
    WEAK_OR_MISSING_SOURCE_REFERENCE = "weak_or_missing_source_reference"
    PARTIAL_UNIT_EVIDENCE = "partial_unit_evidence"
    LLM_REWRITE_NOT_APPLIED = "llm_rewrite_not_applied"


def _require_unique(values: list[str], label: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include an RFC3339 timezone offset")
    return value


class SourcedText(ContractModel):
    text: NonEmptyText
    origin: Origin
    source_refs: list[Identifier] = Field(default_factory=list)

    @field_validator("source_refs")
    @classmethod
    def source_refs_are_unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value, "source_refs")

    @model_validator(mode="after")
    def ncs_content_has_evidence(self) -> Self:
        if self.origin != Origin.ORGANIZATION_INPUT and not self.source_refs:
            raise ValueError("NCS-based or mixed content requires at least one source_ref")
        return self


class OrganizationStatement(ContractModel):
    text: NonEmptyText
    origin: Literal[Origin.ORGANIZATION_INPUT] = Origin.ORGANIZATION_INPUT
    source_refs: list[Identifier] = Field(default_factory=list)

    @field_validator("source_refs")
    @classmethod
    def source_refs_are_unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value, "source_refs")


class ClassificationPath(ContractModel):
    major_code: Identifier
    middle_code: Identifier
    small_code: Identifier
    sub_code: Identifier
    label: NonEmptyText


class IncludedUnit(ContractModel):
    unit_code: Identifier
    unit_name: NonEmptyText
    unit_level: NonEmptyText | None = None
    selection_reason: NonEmptyText


class ScopeSelection(ContractModel):
    organization_job_title: NonEmptyText
    classification_paths: list[ClassificationPath] = Field(min_length=1)
    included_units: list[IncludedUnit] = Field(min_length=1)
    excluded_unit_codes: list[Identifier] = Field(default_factory=list)
    excluded_task_terms: list[NonEmptyText] = Field(default_factory=list)
    target_level_input: NonEmptyText | None = None

    @model_validator(mode="after")
    def selection_is_consistent(self) -> Self:
        included = [item.unit_code for item in self.included_units]
        _require_unique(included, "included unit codes")
        _require_unique(self.excluded_unit_codes, "excluded unit codes")
        _require_unique([term.casefold() for term in self.excluded_task_terms], "excluded task terms")
        overlap = set(included).intersection(self.excluded_unit_codes)
        if overlap:
            raise ValueError(f"unit codes cannot be both included and excluded: {sorted(overlap)}")
        return self


class SourceSnapshot(ContractModel):
    mcp_url_label: NonEmptyText
    retrieved_at: datetime
    selected_unit_codes: list[Identifier] = Field(min_length=1)

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "retrieved_at")

    @field_validator("selected_unit_codes")
    @classmethod
    def selected_units_are_unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value, "selected_unit_codes")


class Duty(ContractModel):
    id: Identifier
    title: NonEmptyText
    summary: NonEmptyText
    origin: Origin
    source_refs: list[Identifier]

    @model_validator(mode="after")
    def evidence_is_present(self) -> Self:
        _require_unique(self.source_refs, "duty source_refs")
        if self.origin != Origin.ORGANIZATION_INPUT and not self.source_refs:
            raise ValueError("NCS-based duty requires at least one source_ref")
        return self


class Task(ContractModel):
    id: Identifier
    duty_id: Identifier
    title: NonEmptyText
    description: NonEmptyText
    origin: Origin
    source_refs: list[Identifier]

    @model_validator(mode="after")
    def evidence_is_present(self) -> Self:
        _require_unique(self.source_refs, "task source_refs")
        if self.origin != Origin.ORGANIZATION_INPUT and not self.source_refs:
            raise ValueError("NCS-based task requires at least one source_ref")
        return self


class JobDescription(ContractModel):
    job_title: OrganizationStatement
    job_purpose: SourcedText
    duties: list[Duty] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    responsibilities: list[OrganizationStatement] = Field(default_factory=list)
    decision_authority: list[OrganizationStatement] = Field(default_factory=list)
    kpis: list[OrganizationStatement] = Field(default_factory=list)
    collaboration: list[OrganizationStatement] = Field(default_factory=list)
    reporting_relationships: list[OrganizationStatement] = Field(default_factory=list)

    @model_validator(mode="after")
    def task_relationships_are_valid(self) -> Self:
        duty_ids = [duty.id for duty in self.duties]
        task_ids = [task.id for task in self.tasks]
        _require_unique(duty_ids, "duty ids")
        _require_unique(task_ids, "task ids")
        unknown = {task.duty_id for task in self.tasks}.difference(duty_ids)
        if unknown:
            raise ValueError(f"tasks reference unknown duty ids: {sorted(unknown)}")
        return self


class KsaItem(ContractModel):
    id: Identifier
    text: NonEmptyText
    type: KsaType
    origin: Origin
    requiredness: Requiredness = Requiredness.REVIEW_REQUIRED
    source_refs: list[Identifier]

    @model_validator(mode="after")
    def direct_ncs_ksa_stays_unconfirmed(self) -> Self:
        _require_unique(self.source_refs, "KSA source_refs")
        if self.origin != Origin.ORGANIZATION_INPUT and not self.source_refs:
            raise ValueError("NCS-based KSA requires at least one source_ref")
        if self.origin != Origin.ORGANIZATION_INPUT and self.requiredness != Requiredness.REVIEW_REQUIRED:
            raise ValueError("NCS KSA requiredness must remain review_required")
        return self


class AdvisoryReferenceItem(ContractModel):
    id: Identifier
    text: NonEmptyText
    origin: Literal[Origin.NCS_REFERENCE] = Origin.NCS_REFERENCE
    requiredness: Literal["reference_only"] = "reference_only"
    source_refs: list[Identifier] = Field(min_length=1)

    @field_validator("source_refs")
    @classmethod
    def source_refs_are_unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value, "advisory source_refs")


class TargetLevel(ContractModel):
    organization_value: NonEmptyText | None = None
    ncs_level_references: list[NonEmptyText] = Field(default_factory=list)
    mapping_status: Literal["not_equated"] = "not_equated"

    @field_validator("ncs_level_references")
    @classmethod
    def ncs_levels_are_unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value, "ncs_level_references")


class PersonSpecification(ContractModel):
    target_level: TargetLevel
    knowledge: list[KsaItem] = Field(default_factory=list)
    skills: list[KsaItem] = Field(default_factory=list)
    attitudes: list[KsaItem] = Field(default_factory=list)
    experience_requirements: list[OrganizationStatement] = Field(default_factory=list)
    education_requirements: list[OrganizationStatement] = Field(default_factory=list)
    qualification_requirements: list[OrganizationStatement] = Field(default_factory=list)
    preference_requirements: list[OrganizationStatement] = Field(default_factory=list)
    qualification_references: list[AdvisoryReferenceItem] = Field(default_factory=list)
    job_base_references: list[AdvisoryReferenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def ksa_collections_match_declared_type(self) -> Self:
        expected = (
            (self.knowledge, KsaType.KNOWLEDGE, "knowledge"),
            (self.skills, KsaType.SKILL, "skills"),
            (self.attitudes, KsaType.ATTITUDE, "attitudes"),
        )
        all_ids: list[str] = []
        for items, expected_type, label in expected:
            if any(item.type != expected_type for item in items):
                raise ValueError(f"{label} entries must declare type={expected_type.value}")
            all_ids.extend(item.id for item in items)
        _require_unique(all_ids, "KSA ids")
        return self


class ReferenceRecord(ContractModel):
    ref_id: Identifier
    source_system: NonEmptyText
    source_type: SourceType
    source_id: Identifier
    unit_code: Identifier | None = None
    element_id: Identifier | None = None
    field: NonEmptyText
    evidence_grade: EvidenceGrade
    text_preview: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "retrieved_at")

    @model_validator(mode="after")
    def source_type_and_grade_are_compatible(self) -> Self:
        if self.source_type == SourceType.ORGANIZATION_INPUT:
            if self.evidence_grade != EvidenceGrade.ORGANIZATION_INPUT:
                raise ValueError("organization_input references require organization_input evidence grade")
        elif self.evidence_grade == EvidenceGrade.ORGANIZATION_INPUT:
            raise ValueError("organization_input evidence grade requires organization_input source type")
        if self.source_type in {SourceType.QUALIFICATION, SourceType.JOB_BASE_COMPETENCY}:
            if self.evidence_grade != EvidenceGrade.REFERENCE:
                raise ValueError("qualification and job-base evidence must remain reference grade")
        return self


class ReviewFlag(ContractModel):
    code: ReviewFlagCode
    severity: ReviewSeverity
    section: NonEmptyText
    message: NonEmptyText
    source_refs: list[Identifier] = Field(default_factory=list)

    @field_validator("source_refs")
    @classmethod
    def source_refs_are_unique(cls, value: list[str]) -> list[str]:
        return _require_unique(value, "review flag source_refs")


class JobProfile(ContractModel):
    """The only persisted MVP artifact; validation enforces provenance invariants."""

    schema_name: Literal["ncs_job_profile_v1"] = Field(alias="schema", serialization_alias="schema")
    document_id: UUID
    document_status: Literal["draft"] = "draft"
    created_at: datetime
    updated_at: datetime
    source_snapshot: SourceSnapshot
    scope_selection: ScopeSelection
    job_description: JobDescription
    person_specification: PersonSpecification
    references: list[ReferenceRecord] = Field(default_factory=list)
    review_flags: list[ReviewFlag] = Field(default_factory=list)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _require_aware(value, "document timestamp")

    @model_validator(mode="after")
    def contract_invariants_hold(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")

        included_codes = [unit.unit_code for unit in self.scope_selection.included_units]
        if self.source_snapshot.selected_unit_codes != included_codes:
            raise ValueError("source snapshot unit codes must match included units in deterministic order")

        references_by_id = {reference.ref_id: reference for reference in self.references}
        if len(references_by_id) != len(self.references):
            raise ValueError("reference ids must be unique")

        excluded_units = set(self.scope_selection.excluded_unit_codes)
        for reference in self.references:
            if reference.unit_code in excluded_units:
                raise ValueError(f"reference uses excluded unit: {reference.unit_code}")
            if reference.unit_code is not None and reference.unit_code not in included_codes:
                raise ValueError(f"reference uses a unit that was not selected: {reference.unit_code}")

        sourced: list[tuple[str, Origin, list[str]]] = [
            ("job_description.job_title", Origin.ORGANIZATION_INPUT, self.job_description.job_title.source_refs),
            ("job_description.job_purpose", self.job_description.job_purpose.origin, self.job_description.job_purpose.source_refs),
        ]
        sourced.extend((f"duties[{item.id}]", item.origin, item.source_refs) for item in self.job_description.duties)
        sourced.extend((f"tasks[{item.id}]", item.origin, item.source_refs) for item in self.job_description.tasks)
        for field_name in (
            "responsibilities",
            "decision_authority",
            "kpis",
            "collaboration",
            "reporting_relationships",
        ):
            sourced.extend(
                (f"job_description.{field_name}", Origin.ORGANIZATION_INPUT, item.source_refs)
                for item in getattr(self.job_description, field_name)
            )
        for field_name in ("knowledge", "skills", "attitudes"):
            sourced.extend(
                (f"person_specification.{field_name}[{item.id}]", item.origin, item.source_refs)
                for item in getattr(self.person_specification, field_name)
            )
        for field_name in (
            "experience_requirements",
            "education_requirements",
            "qualification_requirements",
            "preference_requirements",
        ):
            sourced.extend(
                (f"person_specification.{field_name}", Origin.ORGANIZATION_INPUT, item.source_refs)
                for item in getattr(self.person_specification, field_name)
            )
        for field_name in ("qualification_references", "job_base_references"):
            sourced.extend(
                (f"person_specification.{field_name}[{item.id}]", Origin.NCS_REFERENCE, item.source_refs)
                for item in getattr(self.person_specification, field_name)
            )

        for label, origin, source_refs in sourced:
            missing = set(source_refs).difference(references_by_id)
            if missing:
                raise ValueError(f"{label} has unknown source refs: {sorted(missing)}")
            grades = {references_by_id[ref_id].evidence_grade for ref_id in source_refs}
            if origin == Origin.NCS_DIRECT and grades != {EvidenceGrade.DIRECT}:
                raise ValueError(f"{label} marked ncs_direct must reference only direct evidence")
            if origin == Origin.NCS_SUPPORTING and EvidenceGrade.SUPPORTING not in grades:
                raise ValueError(f"{label} marked ncs_supporting requires supporting evidence")
            if origin == Origin.NCS_REFERENCE and grades != {EvidenceGrade.REFERENCE}:
                raise ValueError(f"{label} marked ncs_reference must reference only reference evidence")
            if origin == Origin.ORGANIZATION_INPUT and any(
                grade != EvidenceGrade.ORGANIZATION_INPUT for grade in grades
            ):
                raise ValueError(f"{label} organization input cannot cite NCS evidence")

        for flag in self.review_flags:
            missing = set(flag.source_refs).difference(references_by_id)
            if missing:
                raise ValueError(f"review flag has unknown source refs: {sorted(missing)}")

        for item in self.person_specification.qualification_references:
            if any(references_by_id[ref_id].source_type != SourceType.QUALIFICATION for ref_id in item.source_refs):
                raise ValueError("qualification references must cite qualification source records")
        for item in self.person_specification.job_base_references:
            if any(
                references_by_id[ref_id].source_type != SourceType.JOB_BASE_COMPETENCY
                for ref_id in item.source_refs
            ):
                raise ValueError("job-base references must cite job_base_competency source records")

        excluded_terms = [term.casefold() for term in self.scope_selection.excluded_task_terms]
        for task in self.job_description.tasks:
            task_text = f"{task.title} {task.description}".casefold()
            if any(term in task_text for term in excluded_terms):
                raise ValueError(f"task {task.id} contains an excluded task term")
        return self


__all__ = [
    "AdvisoryReferenceItem",
    "ClassificationPath",
    "Duty",
    "EvidenceGrade",
    "IncludedUnit",
    "JobDescription",
    "JobProfile",
    "KsaItem",
    "KsaType",
    "OrganizationStatement",
    "Origin",
    "PersonSpecification",
    "ReferenceRecord",
    "Requiredness",
    "ReviewFlag",
    "ReviewFlagCode",
    "ReviewSeverity",
    "ScopeSelection",
    "SourceSnapshot",
    "SourceType",
    "SourcedText",
    "TargetLevel",
    "Task",
]
