from __future__ import annotations

import pytest

from ncs_jd.application.announcement_extraction import (
    AnnouncementExtraction,
    ExtractedAnnouncementItem,
    RoleCandidate,
)
from ncs_jd.application.automatic_drafting import (
    AutomaticDraftPlanningError,
    _category_unit_budgets,
    _search_variants,
    plan_automatic_scope,
)
from ncs_jd.application.document_parser import SourceLocator
from ncs_jd.application.llm_scope_selection import (
    ScopeSelectionError,
    ScopeSelectionRequest,
    ScopeSelectionResult,
    SelectedUnitChoice,
)
from ncs_jd.application.ncs_source import ScopeCandidate


LOCATOR = SourceLocator(block_id="block-1", block_index=0, page_number=1)


def _item(field: str, text: str) -> ExtractedAnnouncementItem:
    return ExtractedAnnouncementItem(
        field=field,  # type: ignore[arg-type]
        text=text,
        source_locator=LOCATOR,
        extraction_method="explicit_label",
        confidence=0.99,
    )


def _candidate(
    unit_code: str,
    unit_name: str,
    *,
    category: str,
) -> ScopeCandidate:
    if category == "electrical":
        codes = ("19", "01", "07", "01")
        names = ("전기·전자", "전기", "전기공사", "내선공사")
        definition = "전원설비와 배선설비를 시공하고 유지보수하는 일"
    else:
        codes = ("05", "02", "01", "04")
        names = ("법률·경찰·소방", "소방방재", "소방", "소방안전관리")
        definition = "소방시설을 점검하고 유지관리하는 일"
    return ScopeCandidate(
        classification_path=" > ".join(names),
        duty_definition=definition,
        unit_code=unit_code,
        unit_name=unit_name,
        unit_level="4",
        unit_definition=f"{unit_name}를 수행한다.",
        major_code=codes[0],
        major_name=names[0],
        middle_code=codes[1],
        middle_name=names[1],
        small_code=codes[2],
        small_name=names[2],
        sub_code=codes[3],
        sub_name=names[3],
    )


class Source:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def search_scope_candidates(self, query: str, limit: int = 20):
        self.calls.append((query, limit))
        electrical = [
            _candidate("E-1", "수변전설비검사", category="electrical"),
            _candidate("E-2", "동력설비공사", category="electrical"),
        ]
        fire = [_candidate("F-1", "소방시설점검", category="fire")]
        if query.startswith("내선공사"):
            return electrical
        if query.startswith("소방안전관리"):
            return fire
        if "소방" in query:
            return [fire[0], electrical[1]]
        return [electrical[0], electrical[1], fire[0]]


def _extraction() -> AnnouncementExtraction:
    role = RoleCandidate(
        candidate_id="role-1",
        role_title=_item("role_title", "전기시설 유지보수"),
        duties=(
            _item("duty", "수변전설비와 동력설비 점검 및 유지보수"),
            _item("duty", "소방시설 점검 및 유지관리"),
        ),
    )
    return AnnouncementExtraction("announcement.pdf", (role,), ())


@pytest.mark.asyncio
async def test_planner_matches_subcategories_then_expands_their_units() -> None:
    source = Source()

    plan = await plan_automatic_scope(_extraction(), source)  # type: ignore[arg-type]

    assert plan.title == "전기시설 유지보수"
    assert [path.sub_code for path in plan.classification_paths] == ["01", "04"]
    assert {unit.unit_code for unit in plan.included_units} == {"E-1", "E-2", "F-1"}
    assert all("자동 매칭" in unit.selection_reason for unit in plan.included_units)
    assert any("핵심 매칭 근거 — 01 내선공사" in note for note in plan.match_notes)
    assert any("내선공사 세분류" in note and "소방안전관리 세분류" in note for note in plan.match_notes)
    assert not any("01 세분류" in note for note in plan.match_notes)
    assert any("직업기초능력" in note for note in plan.match_notes)
    assert ("수변전설비", 40) in source.calls
    assert ("소방시설 점검", 40) in source.calls
    assert ("내선공사", 100) in source.calls


@pytest.mark.asyncio
async def test_generic_department_assignment_is_preserved_but_not_used_for_ncs_search() -> None:
    generic_duty = "기타 소속 부서에서 부여한 업무"
    role = RoleCandidate(
        candidate_id="role-1",
        role_title=_item("role_title", "전기시설 유지보수"),
        duties=(
            _item("duty", "수변전설비와 동력설비 점검 및 유지보수"),
            _item("duty", "소방시설 점검 및 유지관리"),
            _item("duty", generic_duty),
        ),
    )
    source = Source()

    plan = await plan_automatic_scope(
        AnnouncementExtraction("announcement.pdf", (role,), ()),
        source,  # type: ignore[arg-type]
    )

    assert generic_duty in plan.duties
    assert any("NCS 매칭 제외" in note and generic_duty in note for note in plan.match_notes)
    searched_text = " ".join(query for query, limit in source.calls if limit == 40)
    assert "기타" not in searched_text
    assert "소속" not in searched_text
    assert "부여한" not in searched_text


class _RecordingSelector:
    """Selector stub that records its request and replays a fixed answer."""

    def __init__(
        self,
        choices: tuple[tuple[str, str], ...],
        *,
        unmatched: tuple[str, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.choices = choices
        self.unmatched = unmatched
        self.error = error
        self.requests: list[ScopeSelectionRequest] = []

    def select_scope(self, request: ScopeSelectionRequest) -> ScopeSelectionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ScopeSelectionResult(
            tuple(SelectedUnitChoice(code, reason) for code, reason in self.choices),
            self.unmatched,
        )


@pytest.mark.asyncio
async def test_selector_choice_overrides_bigram_scoring() -> None:
    source = Source()
    # "F-1" is the fire unit; a selector may pull it in for an electrical duty
    # that the bigram scorer would never connect.
    selector = _RecordingSelector((("F-1", "소방시설 점검 업무 때문에 선정"),))

    plan = await plan_automatic_scope(
        _extraction(),
        source,  # type: ignore[arg-type]
        scope_selector=selector,
    )

    assert plan.selection_mode == "llm_assisted"
    assert [unit.unit_code for unit in plan.included_units] == ["F-1"]
    assert [path.sub_code for path in plan.classification_paths] == ["04"]
    assert "소방시설 점검 업무 때문에 선정" in plan.included_units[0].selection_reason
    assert any("공식 CLI 제공자가" in note for note in plan.match_notes)
    # Unit name, level, and classification come from the MCP row, never the model.
    assert plan.included_units[0].unit_name == "소방시설점검"
    assert plan.included_units[0].unit_level == "4"


@pytest.mark.asyncio
async def test_selector_receives_only_mcp_returned_candidates() -> None:
    selector = _RecordingSelector((("E-1", "수변전설비 점검"),))

    await plan_automatic_scope(
        _extraction(),
        Source(),  # type: ignore[arg-type]
        scope_selector=selector,
    )

    request = selector.requests[0]
    assert set(request.candidate_codes) == {"E-1", "E-2", "F-1"}
    assert request.job_title == "전기시설 유지보수"


@pytest.mark.asyncio
async def test_unmatched_duties_trigger_a_second_retrieval_round() -> None:
    selector = _RecordingSelector(
        (("E-1", "수변전설비 점검"),),
        unmatched=("소방시설 점검 및 유지관리",),
    )

    plan = await plan_automatic_scope(
        _extraction(),
        Source(),  # type: ignore[arg-type]
        scope_selector=selector,
    )

    # The first pool already holds every stub candidate, so the widening search
    # adds nothing new and the planner must keep the first-round answer.
    assert len(selector.requests) == 1
    assert any("NCS 근거 미확보 업무" in note for note in plan.match_notes)


@pytest.mark.asyncio
async def test_selector_failure_falls_back_to_the_deterministic_plan() -> None:
    selector = _RecordingSelector((), error=ScopeSelectionError("provider_not_installed"))

    plan = await plan_automatic_scope(
        _extraction(),
        Source(),  # type: ignore[arg-type]
        scope_selector=selector,
    )

    assert plan.selection_mode == "deterministic"
    assert {unit.unit_code for unit in plan.included_units} == {"E-1", "E-2", "F-1"}
    assert any("핵심 매칭 근거" in note for note in plan.match_notes)


@pytest.mark.asyncio
async def test_selector_naming_an_unknown_unit_is_ignored_entirely() -> None:
    selector = _RecordingSelector((("NOT-IN-POOL", "허위 코드"),))

    plan = await plan_automatic_scope(
        _extraction(),
        Source(),  # type: ignore[arg-type]
        scope_selector=selector,
    )

    assert plan.selection_mode == "deterministic"
    assert "NOT-IN-POOL" not in {unit.unit_code for unit in plan.included_units}


@pytest.mark.asyncio
async def test_compound_equipment_list_is_split_before_ncs_search() -> None:
    role = RoleCandidate(
        candidate_id="role-1",
        role_title=_item("role_title", "전기시설 유지보수"),
        duties=(
            _item(
                "duty",
                "전기 관련 설비(수변전 및 분전반설비, 소방설비, 발전 및 동력설비, "
                "승강기 설비 등) 및 전기시설물 유지 보수 업무(현장 실무 업무)",
            ),
        ),
    )
    source = Source()

    await plan_automatic_scope(
        AnnouncementExtraction("announcement.pdf", (role,), ()),
        source,  # type: ignore[arg-type]
    )

    searched = {query for query, limit in source.calls if limit == 40}
    assert "소방설비 유지보수" in searched
    assert "소방시설 유지보수" in searched
    assert "승강기 유지보수" in searched
    assert "전기시설물 유지보수" in searched
    assert all("현장 교대근무" not in query for query in searched)


@pytest.mark.asyncio
async def test_planner_rejects_an_announcement_without_duties() -> None:
    extraction = AnnouncementExtraction(
        "empty.pdf",
        (RoleCandidate("role-1", _item("role_title", "시설관리")),),
        (),
    )

    try:
        await plan_automatic_scope(extraction, Source())  # type: ignore[arg-type]
    except AutomaticDraftPlanningError as exc:
        assert exc.code == "announcement_job_details_missing"
    else:  # pragma: no cover
        raise AssertionError("missing duties must be rejected")


@pytest.mark.asyncio
async def test_job_title_override_allows_a_duty_only_pasted_announcement() -> None:
    role = RoleCandidate(
        candidate_id="role-1",
        role_title=None,
        duties=(
            _item("duty", "시설 점검 및 유지보수"),
            _item("duty", "기타 소속 부서에서 부여한 업무"),
        ),
    )

    plan = await plan_automatic_scope(
        AnnouncementExtraction("pasted-announcement.txt", (role,), ()),
        Source(),  # type: ignore[arg-type]
        job_title_override="시설관리 담당자",
    )

    assert plan.title == "시설관리 담당자"
    assert plan.role.role_title is None
    assert any("NCS 매칭 제외" in note for note in plan.match_notes)


def test_three_subcategory_unit_budget_is_14_6_5() -> None:
    assert _category_unit_budgets(3) == (14, 6, 5)
    assert sum(_category_unit_budgets(3)) == 25


def test_counseling_duties_use_bounded_named_ncs_search_bridges() -> None:
    counseling = _search_variants("구성원 고충상담활동")
    prevention = _search_variants("폭력예방교육, 인권보호교육, 자살예방교육 기획")
    investigation = _search_variants("성희롱·성폭력 및 인권침해 사건 조사와 처리")

    assert counseling == ("심리상담안내",)
    assert prevention == ("심리교육", "위기상담")
    assert investigation[:2] == ("심리상담", "위기상담")
    assert all("사례관리" not in item for item in investigation)
    assert len(counseling) <= 8


@pytest.mark.asyncio
async def test_sensitive_investigation_and_survey_duties_stay_organization_only() -> None:
    duties = (
        "구성원 고충상담 운영",
        "예방교육 및 홍보 업무",
        # The exclusion rule keys on these legal categories, so the test has to
        # state them; they are statutory terms, not any one employer's wording.
        "사건 조사 및 처리(성희롱·성폭력, 인권침해 등) 업무",
        "근무환경 개선을 위한 실태조사 업무",
        "제반 업무 관련 기본계획 수립, 결과보고 등 행정업무",
    )
    role = RoleCandidate(
        candidate_id="role-counseling",
        role_title=_item("role_title", "고충상담 담당자"),
        duties=tuple(_item("duty", duty) for duty in duties),
    )
    source = Source()

    plan = await plan_automatic_scope(
        AnnouncementExtraction("announcement.txt", (role,), ()),
        source,  # type: ignore[arg-type]
    )

    searched = " ".join(query for query, limit in source.calls if limit == 40)
    assert "사건" not in searched
    assert "실태조사" not in searched
    assert "기본계획" not in searched
    assert all(duty in plan.duties for duty in duties)
    note = next(item for item in plan.match_notes if "NCS 매칭 제외" in item)
    assert duties[2] in note and duties[3] in note and duties[4] in note
