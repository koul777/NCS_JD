from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ncs_jd.domain.job_profile import JobProfile


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


@pytest.fixture()
def sample_payload() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_sample_validates_and_round_trips(sample_payload: dict[str, object]) -> None:
    profile = JobProfile.model_validate(sample_payload)
    serialized = profile.model_dump(mode="json", by_alias=True)

    assert serialized == sample_payload
    assert JobProfile.model_validate_json(profile.model_dump_json(by_alias=True)) == profile


def test_json_schema_exposes_fixed_contract_values() -> None:
    schema = JobProfile.model_json_schema(by_alias=True)

    assert schema["properties"]["schema"]["const"] == "ncs_job_profile_v1"
    assert schema["properties"]["document_status"]["const"] == "draft"


@pytest.mark.parametrize("status", ["reviewed", "accepted", "human_reviewed", "published"])
def test_document_status_can_only_be_draft(sample_payload: dict[str, object], status: str) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["document_status"] = status

    with pytest.raises(ValidationError, match="draft"):
        JobProfile.model_validate(payload)


def test_ncs_direct_item_requires_source_reference(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["job_description"]["duties"][0]["source_refs"] = []

    with pytest.raises(ValidationError, match="source_ref"):
        JobProfile.model_validate(payload)


def test_dangling_source_reference_is_rejected(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["job_description"]["tasks"][0]["source_refs"] = ["ref-does-not-exist"]

    with pytest.raises(ValidationError, match="unknown source refs"):
        JobProfile.model_validate(payload)


def test_excluded_unit_cannot_reappear_in_references(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["references"][1]["unit_code"] = "0202020104_23v4"

    with pytest.raises(ValidationError, match="excluded unit"):
        JobProfile.model_validate(payload)


def test_excluded_task_term_cannot_reappear(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["job_description"]["tasks"][0]["description"] = "교육훈련 일정을 운영한다."

    with pytest.raises(ValidationError, match="excluded task term"):
        JobProfile.model_validate(payload)


def test_ncs_ksa_cannot_be_automatically_required(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["person_specification"]["knowledge"][0]["requiredness"] = "required"

    with pytest.raises(ValidationError, match="review_required"):
        JobProfile.model_validate(payload)


def test_ncs_level_cannot_be_equated_to_organization_grade(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["person_specification"]["target_level"]["mapping_status"] = "equated"

    with pytest.raises(ValidationError, match="not_equated"):
        JobProfile.model_validate(payload)


def test_unknown_review_status_field_is_rejected(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["references"][0]["review_status"] = "human_reviewed"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        JobProfile.model_validate(payload)


def test_reference_only_source_cannot_be_promoted_to_direct(sample_payload: dict[str, object]) -> None:
    payload = copy.deepcopy(sample_payload)
    payload["references"].append(
        {
            "ref_id": "ref-qualification-1",
            "source_system": "NCS_MCP",
            "source_type": "qualification",
            "source_id": "qualification-1",
            "unit_code": "0202020103_23v4",
            "element_id": None,
            "field": "qualification_name",
            "evidence_grade": "direct",
            "text_preview": "참고 자격",
            "retrieved_at": "2026-08-13T08:00:00Z",
        }
    )

    with pytest.raises(ValidationError, match="reference grade"):
        JobProfile.model_validate(payload)
