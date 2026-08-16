from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncs_jd.application.agent_drafting import (
    MAX_DOCUMENT_CHARS,
    MAX_FIELD_VALUE_CHARS,
    TRUNCATION_MARKER,
    AgentDraftError,
    AgentDraftRequest,
    agent_draft_prompt,
    classification_field_values,
    field_value_budget,
    template_labels_from_inspection,
    validate_agent_draft,
)
from ncs_jd.application.document_renderer import (
    DRAFT_DISCLAIMER,
    SUPPORTED_TEMPLATE_LABELS,
    field_values_to_markdown,
    job_profile_to_template_values,
)
from ncs_jd.application.ncs_source import ScopeCandidate
from ncs_jd.application.template_mapping import TemplateField
from ncs_jd.domain.job_profile import JobProfile


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


def _request(**overrides: object) -> AgentDraftRequest:
    defaults: dict[str, object] = {
        "job_title": "통신설비 운영",
        "duties": ("구내 전화 설비 운영", "CCTV 시스템 관리"),
        "template_labels": ("채용분야", "능력단위", "직무수행내용"),
    }
    defaults.update(overrides)
    return AgentDraftRequest(**defaults)  # type: ignore[arg-type]


def test_supported_labels_match_the_renderer_output() -> None:
    """The agent's target labels must stay identical to what the renderer fills."""

    profile = JobProfile.model_validate(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))

    assert tuple(job_profile_to_template_values(profile)) == SUPPORTED_TEMPLATE_LABELS


def test_request_rejects_empty_and_duplicated_input() -> None:
    with pytest.raises(ValueError):
        _request(duties=())
    with pytest.raises(ValueError):
        _request(job_title="   ")
    with pytest.raises(ValueError):
        _request(template_labels=("능력단위", "능력단위"))


def test_prompt_states_the_search_rules_that_prevent_wasted_lookups() -> None:
    prompt = agent_draft_prompt(_request())

    # Multi-word queries mostly return NOT_FOUND, and a subcategory name returns
    # its whole roster; an unguided run rediscovers both by wasting calls.
    assert "부분 문자열 매칭" in prompt
    assert "세분류명을 query로 넣으면" in prompt
    assert 'include=["elements","ksa"]' in prompt
    assert "구내 전화 설비 운영" in prompt


def test_validate_accepts_requested_labels_and_normalizes_notes() -> None:
    payload = {
        "fields": [
            {"label": "채용분야", "value": " 통신설비 운영 "},
            {"label": "능력단위", "value": "구내통신 운영관리"},
        ],
        "unit_codes": ["2002010210_25v5", "2002010210_25v5", " "],
        "notes": ["대관  업무  근거  없음", "대관  업무  근거  없음"],
    }

    values, codes, notes = validate_agent_draft(payload, _request())

    assert values == (("채용분야", "통신설비 운영"), ("능력단위", "구내통신 운영관리"))
    assert codes == ("2002010210_25v5",)
    assert notes == ("대관 업무 근거 없음",)


def test_field_budget_grows_when_a_form_has_fewer_items() -> None:
    """A five-item form packs the same job description into fewer cells."""

    assert field_value_budget(13) == MAX_FIELD_VALUE_CHARS
    assert field_value_budget(5) == MAX_DOCUMENT_CHARS // 5
    assert field_value_budget(5) > field_value_budget(13)
    with pytest.raises(ValueError):
        field_value_budget(0)


def test_prompt_states_the_budget_so_the_agent_can_stay_inside_it() -> None:
    request = _request(template_labels=("담당업무", "필요역량", "비고"))

    assert str(field_value_budget(3)) in agent_draft_prompt(request)


def test_prompt_defaults_to_the_grouped_style_without_a_form() -> None:
    prompt = agent_draft_prompt(_request())

    assert "기본형" in prompt
    assert "'○ (세분류명)'" in prompt
    assert "붙임 양식을 그대로 따르세요" not in prompt


def test_prompt_mirrors_an_uploaded_form_style_when_examples_are_present() -> None:
    request = _request(
        template_labels=("능력 단위", "필요 지식"),
        template_examples=(
            ("능력 단위", "○ (전기설비운영) 01.수전설비 운영 03.변전설비 운영"),
            ("필요 지식", "·전기도면 지식\n·차단기의 종류 및 특성"),
        ),
    )
    prompt = agent_draft_prompt(request)

    # The form's own cell samples become the style the agent must mirror.
    assert "붙임 양식을 그대로 따르세요" in prompt
    assert "01.수전설비 운영" in prompt
    assert "·전기도면 지식" in prompt
    # The fixed ○/· default is not imposed on top of the form's style.
    assert "[작성 서식 — 기본형]" not in prompt


def test_validate_truncates_an_oversized_field_instead_of_discarding_the_run() -> None:
    request = _request(template_labels=("담당업무",))
    budget = field_value_budget(1)
    payload = {
        "fields": [{"label": "담당업무", "value": "가" * (budget + 500)}],
        "notes": ["기존 검토 사항"],
    }

    values, _, notes = validate_agent_draft(payload, request)

    assert len(values[0][1]) == budget
    assert values[0][1].endswith(TRUNCATION_MARKER)
    # The cut is surfaced for review rather than left for the reader to notice.
    assert "담당업무" in notes[0] and str(budget) in notes[0]
    assert notes[1] == "기존 검토 사항"


def test_validate_leaves_a_field_inside_the_budget_untouched() -> None:
    request = _request(template_labels=("담당업무",))
    value = "가" * field_value_budget(1)
    payload = {"fields": [{"label": "담당업무", "value": value}]}

    values, _, notes = validate_agent_draft(payload, request)

    assert values[0][1] == value
    assert notes == ()


def test_validate_rejects_a_label_outside_the_template() -> None:
    payload = {"fields": [{"label": "연봉", "value": "5000만원"}]}

    with pytest.raises(AgentDraftError) as excinfo:
        validate_agent_draft(payload, _request())

    assert excinfo.value.code == "agent_field_label_unknown"


def test_validate_drops_unit_codes_the_source_does_not_have() -> None:
    payload = {
        "fields": [{"label": "채용분야", "value": "통신설비 운영"}],
        "unit_codes": ["2002010210_25v5", "9999999999_99v9"],
    }

    _, codes, _ = validate_agent_draft(
        payload,
        _request(),
        known_unit_codes=["2002010210_25v5"],
    )

    assert codes == ("2002010210_25v5",)


def test_markdown_keeps_the_disclaimer_and_escapes_table_breaking_text() -> None:
    markdown = field_values_to_markdown(
        (
            ("채용분야", "통신설비 운영"),
            ("대분류", "20. 정보통신 / 19. 전기·전자"),
            # A pipe would split the cell and a newline would end the row.
            ("능력단위", "구내전화설비공사 | 2002010407\n영상정보처리기기설비공사"),
            ("필요자격", ""),
        ),
        job_title="통신설비 운영",
    )

    assert DRAFT_DISCLAIMER in markdown
    rows = [line for line in markdown.splitlines() if line.startswith("| ")]
    # Header, separator, then exactly one row per field: nothing leaked a row.
    assert len(rows) == 6
    assert "| 능력단위 | 구내전화설비공사 \\| 2002010407<br>영상정보처리기기설비공사 |" in markdown
    assert "| 필요자격 | 조직 입력 필요 |" in markdown


def test_markdown_rejects_input_that_would_render_a_meaningless_document() -> None:
    with pytest.raises(ValueError):
        field_values_to_markdown((("채용분야", "가"),), job_title="  ")
    with pytest.raises(ValueError):
        field_values_to_markdown((), job_title="통신설비 운영")
    with pytest.raises(ValueError):
        field_values_to_markdown(
            (("채용분야", "가"), ("채용분야", "나")),
            job_title="통신설비 운영",
        )


def test_inspection_labels_drop_prose_and_keep_form_item_order() -> None:
    labels = template_labels_from_inspection(
        [
            TemplateField(label="  채용분야 ", row=1),
            TemplateField(label="채용분야", row=2),  # the same cell text twice
            TemplateField(label="직무수행\n내용", row=3),
            # A sentence sitting in the label column: filling it would invent
            # a section the form never asked for.
            TemplateField(label="본 양식은 인사위원회 의결을 거쳐 확정한다. " * 3, row=4),
            TemplateField(label="필요지식", row=5),
        ]
    )

    assert labels == ("채용분야", "직무수행 내용", "필요지식")


def test_inspection_labels_drop_the_table_header_but_keep_it_elsewhere() -> None:
    """``항목 | 내용`` captions the columns; filling it overwrites the caption."""

    header_form = template_labels_from_inspection(
        [
            TemplateField(label="항목", value_preview="내용", row=0, col=0),
            TemplateField(label="채용분야", row=1, col=0),
        ]
    )
    assert header_form == ("채용분야",)

    # The same word below the header row is a real item and must survive.
    body_item = template_labels_from_inspection(
        [
            TemplateField(label="채용분야", row=0, col=0),
            TemplateField(label="구분", row=1, col=0),
        ]
    )
    assert body_item == ("채용분야", "구분")


def test_inspection_labels_drop_the_sample_answers_a_filled_form_ships_with() -> None:
    """An institution form filled in with an example must not dictate items.

    These are the cells Kordoc reports for a public-institution form that
    ships filled in with one worked example.  Detection pairs each cell with
    the one after it, so every sample value also arrives as a label; taking
    them as items would make the agent write prose into ``02. 경영∙회계∙사무``
    and leave the real classification cells holding another job's answer.
    """

    labels = template_labels_from_inspection(
        [
            TemplateField(label="채용 분야", value_preview="전문직(국제교류)", row=1, col=0),
            TemplateField(label="전문직(국제교류)", row=1, col=1),
            TemplateField(label="분류 체계", value_preview="대분류", row=2, col=0),
            TemplateField(label="대분류", row=2, col=1),
            TemplateField(label="중분류", row=2, col=3),
            TemplateField(label="소분류", row=2, col=5),
            TemplateField(label="세분류", row=2, col=7),
            TemplateField(label="02. 경영∙회계∙사무", row=4, col=1),
            TemplateField(label="03. 일반사무", row=5, col=5),
            TemplateField(label="능력 단위", value_preview="○ (프로젝트관리)", row=7, col=0),
            TemplateField(label="필요 지식", value_preview="○ (프로젝트관리)", row=9, col=0),
        ]
    )

    assert labels == ("채용 분야", "분류 체계", "능력 단위", "필요 지식")


def test_inspection_labels_drop_the_classification_the_units_decide() -> None:
    """A blank form lists 대분류~세분류 as ordinary items; they are not.

    Every unit already records which industry it belongs to, so asking the model
    for these four cells only creates a way for them to disagree with the
    능력단위 list printed beside them.  They are dropped here and refilled from
    the adopted units instead.
    """

    labels = template_labels_from_inspection(
        [
            TemplateField(label="채용분야", row=0, col=0),
            TemplateField(label="대분류", row=1, col=0),
            TemplateField(label="중분류", row=2, col=0),
            TemplateField(label="소분류", row=3, col=0),
            TemplateField(label="세 분류", row=4, col=0),
            TemplateField(label="능력단위", row=5, col=0),
        ]
    )

    assert labels == ("채용분야", "능력단위")


def _candidate(**overrides: object) -> ScopeCandidate:
    defaults: dict[str, object] = {
        "classification_path": "전기·전자 > 전기 > 전기공사 > 내선공사",
        "duty_definition": None,
        "unit_code": "1901070117_22v4",
        "unit_name": "배선공사",
        "unit_level": "2",
        "unit_definition": None,
        "major_code": "19",
        "major_name": "전기·전자",
        "middle_code": "01",
        "middle_name": "전기",
        "small_code": "07",
        "small_name": "전기공사",
        "sub_code": "01",
        "sub_name": "내선공사",
    }
    defaults.update(overrides)
    return ScopeCandidate(**defaults)  # type: ignore[arg-type]


def test_classification_cells_are_read_back_from_the_adopted_units() -> None:
    values = classification_field_values(
        [
            _candidate(),
            # A second unit from the same subcategory must not print it twice.
            _candidate(unit_code="1901070118_22v4", unit_name="전선관공사"),
            _candidate(
                unit_code="1505010101_23v1",
                unit_name="승강기 설치",
                major_code="15",
                major_name="기계",
                middle_code="05",
                middle_name="기계장치설치",
                small_code="01",
                small_name="기계장비설치·정비",
                sub_code="07",
                sub_name="승강기설치·정비",
            ),
        ]
    )

    assert values == (
        ("대분류", "19. 전기·전자 / 15. 기계"),
        ("중분류", "01. 전기 / 05. 기계장치설치"),
        ("소분류", "07. 전기공사 / 01. 기계장비설치·정비"),
        ("세분류", "01. 내선공사 / 07. 승강기설치·정비"),
    )


def test_classification_skips_a_unit_whose_path_is_incomplete() -> None:
    """Printing three of four levels states a classification nobody selected."""

    values = classification_field_values(
        [_candidate(small_code=None, small_name=None), _candidate(unit_code="x")]
    )

    assert values == (
        ("대분류", "19. 전기·전자"),
        ("중분류", "01. 전기"),
        ("소분류", "07. 전기공사"),
        ("세분류", "01. 내선공사"),
    )


def test_classification_is_empty_when_no_unit_resolves() -> None:
    assert classification_field_values([]) == ()


def test_inspection_labels_respect_the_cap() -> None:
    labels = template_labels_from_inspection(
        (TemplateField(label=f"항목{index}", row=index + 1) for index in range(60)),
        limit=3,
    )

    assert labels == ("항목0", "항목1", "항목2")
    with pytest.raises(ValueError):
        template_labels_from_inspection((TemplateField(label="담당업무"),), limit=0)


def test_validate_rejects_a_duplicated_label() -> None:
    payload = {
        "fields": [
            {"label": "채용분야", "value": "가"},
            {"label": "채용분야", "value": "나"},
        ]
    }

    with pytest.raises(AgentDraftError) as excinfo:
        validate_agent_draft(payload, _request())

    assert excinfo.value.code == "agent_field_label_duplicated"
