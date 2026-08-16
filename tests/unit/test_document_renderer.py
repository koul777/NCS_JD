from __future__ import annotations

import json
from pathlib import Path

from ncs_jd.application.document_renderer import (
    job_profile_to_markdown,
    job_profile_to_template_values,
)
from ncs_jd.domain.job_profile import ClassificationPath, JobProfile


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


def _profile() -> JobProfile:
    return JobProfile.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_markdown_uses_standard_title_tables_and_separate_document_objects() -> None:
    markdown = job_profile_to_markdown(_profile())

    assert markdown.startswith("# NCS 기반 채용 직무 설명자료 : 채용 담당자\n")
    assert "| 채용분야 | 채용 담당자 |" in markdown
    assert "| 대분류 | 중분류 | 소분류 | 세분류 | 전체 경로 |" in markdown
    assert "## 직무기술서" in markdown
    assert "## 직무명세서" in markdown
    assert markdown.index("## 직무기술서") < markdown.index("## 직무명세서")
    # Content is grouped by subcategory (○) with · bullets, matching the
    # curated reference document; unit codes move to the 비고/근거 basis row.
    assert "| 능력단위 | ○ (인사)<br>· 인력채용 |" in markdown
    assert "| 직무수행내용 | ○ (인사)<br>· 채용 계획 수립: 채용 필요 인력과 절차를 검토한다. |" in markdown
    assert "| 필요지식 | ○ (인사)<br>· 채용 절차에 관한 지식 |" in markdown
    assert "○ 인사 : 0202020103_23v4" in markdown
    assert "| 필요기술 | 조직 입력 필요 |" in markdown
    assert "| 직무수행태도 | 조직 입력 필요 |" in markdown
    assert "| 직업기초능력 | 조직 입력 필요 |" in markdown


def test_multiple_classification_paths_keep_all_four_levels_in_input_order() -> None:
    profile = _profile()
    second = ClassificationPath(
        major_code="19",
        middle_code="01",
        small_code="07",
        sub_code="01",
        label="전기·전자 > 전기 > 전기공사 > 내선공사",
    )
    profile = profile.model_copy(
        update={
            "scope_selection": profile.scope_selection.model_copy(
                update={"classification_paths": [*profile.scope_selection.classification_paths, second]}
            )
        }
    )

    markdown = job_profile_to_markdown(profile)

    first_row = "| 02. 경영·회계·사무 | 02. 총무·인사 | 02. 인사·조직 | 01. 인사 |"
    second_row = "| 19. 전기·전자 | 01. 전기 | 07. 전기공사 | 01. 내선공사 |"
    assert first_row in markdown
    assert second_row in markdown
    assert markdown.index(first_row) < markdown.index(second_row)


def test_empty_organization_fields_are_not_inferred_from_ncs() -> None:
    markdown = job_profile_to_markdown(_profile())

    assert "| 의사결정 권한 | 조직 입력 필요 |" in markdown
    assert "| KPI·기대성과 | 조직 입력 필요 |" in markdown
    assert "| 협업대상 | 조직 입력 필요 |" in markdown
    assert "| 보고관계 | 조직 입력 필요 |" in markdown
    assert "organization_kpi_missing" in markdown


def test_reference_previews_are_not_dumped_and_ids_flags_stay_in_evidence_rows() -> None:
    markdown = job_profile_to_markdown(_profile())

    # This string exists only in ReferenceRecord.text_preview, not in a
    # presentation field of the validated JobProfile.
    assert "채용 필요 인력을 파악할 수 있다." not in markdown
    # Internal provenance handles must never reach the reader; the auditable
    # trail is the NCS unit codes in the 비고/근거 basis row instead.
    assert "ref-unit" not in markdown
    assert "출처 ID" not in markdown


def test_markdown_is_stable_and_changes_only_with_profile_presentation_fields() -> None:
    profile = _profile()
    assert job_profile_to_markdown(profile) == job_profile_to_markdown(profile)

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["job_description"]["tasks"][0]["description"] = "확인된 변경 업무 문장"
    changed = JobProfile.model_validate(payload)
    assert "확인된 변경 업무 문장" in job_profile_to_markdown(changed)

    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    payload["references"][3]["text_preview"] = "본문으로 복사되면 안 되는 변경 원문"
    reference_only_change = JobProfile.model_validate(payload)
    assert job_profile_to_markdown(reference_only_change) == job_profile_to_markdown(profile)


def test_draft_disclaimer_and_safe_exact_template_values_are_present() -> None:
    profile = _profile()
    markdown = job_profile_to_markdown(profile)
    values = job_profile_to_template_values(profile)

    assert "검토용 초안(draft)" in markdown
    assert "공식 승인 문서" in markdown
    assert "채용 결정" in markdown
    assert "공식 자격 인정이 아닙니다" in markdown
    assert tuple(values) == (
        "채용분야",
        "대분류",
        "중분류",
        "소분류",
        "세분류",
        "능력단위",
        "직무수행내용",
        "필요지식",
        "필요기술",
        "직무수행태도",
        "필요자격",
        "직업기초능력",
        "비고/근거",
    )
    assert "ref-unit-1" not in values["직무수행내용"]
    # The basis row carries NCS unit codes, never internal ref IDs.
    assert not any("ref-unit" in value for value in values.values())
    assert "0202020103_23v4" in values["비고/근거"]
    assert "검토용 초안(draft)" in values["비고/근거"]
