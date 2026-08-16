from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ncs_jd.application.agent_drafting import (
    AgentDraftError,
    AgentDraftResult,
    AgentProgress,
)
from ncs_jd.application.announcement_extraction import (
    AnnouncementExtraction,
    ExtractedAnnouncementItem,
    RoleCandidate,
)
from ncs_jd.application.document_parser import DocumentParseTimeoutError, SourceLocator
from ncs_jd.application.document_renderer import (
    SUPPORTED_TEMPLATE_LABELS,
    HwpxTemplate,
    HwpxValidationSummary,
    InvalidHwpxError,
    KordocRendererError,
    RenderedHwpx,
    TemplateCapability,
)
from ncs_jd.application.drafting_workflow import (
    ConfirmedDraftRequest,
    DraftGenerationResult,
    DraftingDiagnostics,
    DraftingWorkflowError,
    UnitEvidenceDiagnostic,
)
from ncs_jd.application.llm_scope_selection import ScopeSelectionError
from ncs_jd.application.ncs_source import NcsSourceUnavailableError, ScopeCandidate
from ncs_jd.application.template_mapping import (
    TemplateField,
    TemplateInspection,
    TemplateMappingResult,
)
from ncs_jd.domain.job_profile import ClassificationPath, IncludedUnit, JobProfile
from ncs_jd.web.api import create_api_router


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"
DOCUMENT_ID = UUID("de305d54-75b4-431b-adb2-eb6b9e546014")
CREATED_AT = "2026-08-13T09:30:00+09:00"
RETRIEVED_AT = "2026-08-13T09:31:00+09:00"


def _profile() -> JobProfile:
    return JobProfile.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


def _extraction() -> AnnouncementExtraction:
    locator = SourceLocator(block_id="block-0007", block_index=6, page_number=2)
    title = ExtractedAnnouncementItem(
        field="role_title",
        text="시설관리 담당자",
        source_locator=locator,
        extraction_method="table_cell",
        confidence=0.98,
    )
    duty = ExtractedAnnouncementItem(
        field="duty",
        text="설비 상태 점검",
        source_locator=locator,
        extraction_method="table_cell",
        confidence=0.96,
    )
    return AnnouncementExtraction(
        source_name="announcement.pdf",
        role_candidates=(
            RoleCandidate(candidate_id="role-001", role_title=title, duties=(duty,)),
        ),
        review_flags=(),
    )


def _diagnostics() -> DraftingDiagnostics:
    return DraftingDiagnostics(
        unit_evidence=(
            UnitEvidenceDiagnostic(
                unit_code="UNIT-A",
                status="loaded",
                code="unit_evidence_loaded",
                message="loaded",
            ),
            UnitEvidenceDiagnostic(
                unit_code="UNIT-B",
                status="failed",
                code="ncs_source_timeout",
                message="safe diagnostic",
                retryable=True,
            ),
        )
    )


class FakeWorkflow:
    def __init__(self) -> None:
        self.parse_calls: list[tuple[str, bytes]] = []
        self.draft_calls: list[ConfirmedDraftRequest] = []
        self.parse_error: Exception | None = None
        self.draft_error: Exception | None = None

    async def parse_announcement(self, source_name: str, content: bytes) -> AnnouncementExtraction:
        self.parse_calls.append((source_name, content))
        if self.parse_error:
            raise self.parse_error
        return _extraction()

    async def generate_draft(self, request: ConfirmedDraftRequest) -> DraftGenerationResult:
        self.draft_calls.append(request)
        if self.draft_error:
            raise self.draft_error
        return DraftGenerationResult(_profile(), _diagnostics())


class FakeSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.error: Exception | None = None
        self.candidate = ScopeCandidate(
            classification_path="전기전자 > 전기",
            duty_definition="시설을 유지한다.",
            unit_code="UNIT-A",
            unit_name="시설 점검",
            unit_level="4",
            unit_definition="시설 상태를 확인한다.",
        )

    async def search_scope_candidates(self, query: str, limit: int = 20) -> list[ScopeCandidate]:
        self.calls.append((query, limit))
        if self.error:
            raise self.error
        return [self.candidate]

    async def load_unit_evidence(self, unit_code: str):  # pragma: no cover - must stay unused
        raise AssertionError("search endpoint called a detail method")

    async def load_optional_references(self, unit_code: str, kinds: tuple[str, ...]):  # pragma: no cover
        raise AssertionError("search endpoint called an optional-reference method")

    async def check_readiness(self):  # pragma: no cover
        raise AssertionError("search endpoint called readiness")


class FakeRenderer:
    def __init__(self, capability: TemplateCapability | None = None) -> None:
        self.calls: list[tuple[JobProfile, str | None, HwpxTemplate | None]] = []
        self.field_calls: list[
            tuple[tuple[tuple[str, str], ...], str, str | None, HwpxTemplate | None]
        ] = []
        self.error: Exception | None = None
        self.capability = capability

    def render(
        self,
        profile: JobProfile,
        *,
        filename: str | None = None,
        template: HwpxTemplate | None = None,
    ) -> RenderedHwpx:
        self.calls.append((profile, filename, template))
        if self.error:
            raise self.error
        capability = self.capability or (
            TemplateCapability(
                requested=True,
                supported=True,
                used=True,
                mode="hwpx-preserve",
                matched_fields=("job_title",),
            )
            if template
            else TemplateCapability()
        )
        return RenderedHwpx(
            filename="시설관리 초안.hwpx",
            content=b"safe-hwpx-bytes",
            validation=HwpxValidationSummary(valid=True, entry_count=7, issue_count=0),
            template_capability=capability,
        )

    def render_fields(
        self,
        field_values: tuple[tuple[str, str], ...],
        *,
        job_title: str,
        filename: str | None = None,
        template: HwpxTemplate | None = None,
    ) -> RenderedHwpx:
        self.field_calls.append((field_values, job_title, filename, template))
        if self.error:
            raise self.error
        return RenderedHwpx(
            filename="에이전트 초안.hwpx",
            content=b"safe-hwpx-bytes",
            validation=HwpxValidationSummary(valid=True, entry_count=7, issue_count=0),
            template_capability=self.capability or TemplateCapability(),
        )

    def render_preview(self, content: bytes):  # pragma: no cover - must stay unused
        raise AssertionError("export endpoint called preview")


class FakeTemplateMapper:
    def __init__(self, provider: str = "claude", model: str = "claude-cli") -> None:
        self.calls: list[tuple[JobProfile, HwpxTemplate]] = []
        self.provider = provider
        self.model = model

    def map_fields(
        self,
        profile: JobProfile,
        template: HwpxTemplate,
    ) -> TemplateMappingResult:
        self.calls.append((profile, template))
        return TemplateMappingResult(
            provider=self.provider,
            model=self.model,
            field_values=(("담당 업무", "검증된 수행 업무"),),
            mapped_labels=("담당 업무",),
        )


class _FallbackSelector:
    def select_scope(self, request):  # pragma: no cover - planner catches this
        raise ScopeSelectionError("provider_not_installed")


class AutomaticSource(FakeSource):
    async def search_scope_candidates(self, query: str, limit: int = 20) -> list[ScopeCandidate]:
        self.calls.append((query, limit))
        return [
            ScopeCandidate(
                classification_path="전기·전자 > 전기 > 전기공사 > 내선공사",
                duty_definition="전기설비를 시공하고 유지보수하는 일",
                unit_code="UNIT-A",
                unit_name="시설 점검",
                unit_level="4",
                unit_definition="시설 상태를 확인한다.",
                major_code="19",
                major_name="전기·전자",
                middle_code="01",
                middle_name="전기",
                small_code="07",
                small_name="전기공사",
                sub_code="01",
                sub_name="내선공사",
            )
        ]


def _client(
    workflow: FakeWorkflow | None = None,
    source: FakeSource | None = None,
    renderer: FakeRenderer | None = None,
    **router_options: Any,
) -> tuple[TestClient, FakeWorkflow, FakeSource, FakeRenderer]:
    workflow = workflow or FakeWorkflow()
    source = source or FakeSource()
    renderer = renderer or FakeRenderer()
    app = FastAPI()
    app.include_router(
        create_api_router(
            workflow=workflow,  # type: ignore[arg-type]
            ncs_source=source,  # type: ignore[arg-type]
            renderer=renderer,
            **router_options,
        )
    )
    return TestClient(app), workflow, source, renderer


def _draft_payload(*, confirmed: bool = True) -> dict[str, Any]:
    return {
        "confirmed": confirmed,
        "document_id": str(DOCUMENT_ID),
        "created_at": CREATED_AT,
        "retrieved_at": RETRIEVED_AT,
        "organization_job_title": "시설관리 담당자",
        "classification_paths": [
            {
                "major_code": "19",
                "middle_code": "01",
                "small_code": "07",
                "sub_code": "01",
                "label": "전기전자 > 전기 > 전기공사 > 내선공사",
            }
        ],
        "included_units": [
            {
                "unit_code": "UNIT-A",
                "unit_name": "시설 점검",
                "unit_level": "4",
                "selection_reason": "사용자 확인",
            }
        ],
        "organization_input": {
            "purpose_supplement": "안전한 시설 운영",
            "responsibilities": ["점검 일정을 관리한다."],
            "kpis": ["점검 이행률"],
        },
        "excluded_unit_codes": ["UNIT-X"],
        "excluded_task_terms": ["채용"],
        "target_level_input": "주임",
        "mcp_url_label": "local-test-mcp",
    }


def test_extract_returns_safe_dataclass_json_with_locator_and_review_gate() -> None:
    client, workflow, _, _ = _client()

    response = client.post(
        "/api/extract",
        files={"announcement": ("announcement.pdf", b"fake-pdf", "application/pdf")},
    )

    assert response.status_code == 200
    assert workflow.parse_calls == [("announcement.pdf", b"fake-pdf")]
    item = response.json()["role_candidates"][0]["duties"][0]
    assert item["source_locator"] == {
        "block_id": "block-0007",
        "block_index": 6,
        "page_number": 2,
    }
    assert item["review_required"] is True
    assert "content" not in response.json()


def test_extract_enforces_bound_before_calling_workflow() -> None:
    client, workflow, _, _ = _client(max_announcement_bytes=4)

    response = client.post(
        "/api/extract",
        files={"announcement": ("announcement.pdf", b"12345", "application/pdf")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "document_too_large"
    assert workflow.parse_calls == []


def test_search_calls_only_scope_candidate_port_and_returns_dataclass_json() -> None:
    client, _, source, _ = _client()

    response = client.get("/api/ncs/search", params={"query": "  시설관리  ", "limit": 7})

    assert response.status_code == 200
    assert source.calls == [("시설관리", 7)]
    assert response.json()["candidates"] == [
        {
            "classification_path": "전기전자 > 전기",
            "duty_definition": "시설을 유지한다.",
            "unit_code": "UNIT-A",
            "unit_name": "시설 점검",
            "unit_level": "4",
            "unit_definition": "시설 상태를 확인한다.",
            "major_code": None,
            "major_name": None,
            "middle_code": None,
            "middle_name": None,
            "small_code": None,
            "small_name": None,
            "sub_code": None,
            "sub_name": None,
        }
    ]


def test_one_request_generates_deterministic_hwpx_from_announcement_and_template() -> None:
    workflow = FakeWorkflow()
    source = AutomaticSource()
    renderer = FakeRenderer()
    client, _, _, _ = _client(workflow, source, renderer)

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "off",
        },
        files={
            "announcement": ("announcement.pdf", b"fake-pdf", "application/pdf"),
            "template": ("template.hwpx", b"fake-template", "application/octet-stream"),
        },
    )

    assert response.status_code == 200
    assert response.content == b"safe-hwpx-bytes"
    assert response.headers["x-ncs-jd-ai-provider"] == "off"
    assert response.headers["x-ncs-jd-generation-mode"] == "deterministic"
    assert response.headers["x-ncs-jd-selected-units"] == "1"
    assert response.headers["x-ncs-jd-selected-subcategories"] == "1"
    assert workflow.parse_calls == [("announcement.pdf", b"fake-pdf")]
    request = workflow.draft_calls[0]
    assert request.scope_confirmation_required is True
    assert request.scope_match_notes
    assert request.organization_input.responsibilities == ("설비 상태 점검",)
    assert request.included_units[0].selection_reason.endswith("검토 필요")
    rendered_profile, filename, template = renderer.calls[0]
    assert not rendered_profile.job_description.job_purpose.text.endswith(" 정리")
    assert filename == "NCS_직무기술서_시설관리 담당자.hwpx"
    assert template == HwpxTemplate("template.hwpx", b"fake-template")


def test_one_request_accepts_pasted_announcement_and_job_title_override() -> None:
    workflow = FakeWorkflow()
    client, _, _, _ = _client(workflow, AutomaticSource(), FakeRenderer())

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "off",
            "job_title": "행정지원 담당자",
            "announcement_text": "담당 업무\n- 공문서 작성 및 관리\n- 회의 운영 지원",
        },
    )

    assert response.status_code == 200
    assert workflow.parse_calls == [
        (
            "pasted-announcement.txt",
            "담당 업무\n- 공문서 작성 및 관리\n- 회의 운영 지원".encode("utf-8"),
        )
    ]
    assert workflow.draft_calls[0].organization_job_title == "행정지원 담당자"


class FakeAgentRunner:
    """Emit a fixed progress sequence, or fail, without launching a CLI."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[Any] = []

    def run_draft(self, request: Any, on_progress: Any = None) -> Any:
        self.requests.append(request)
        if on_progress is not None:
            on_progress(AgentProgress("started", 1, "탐색 시작"))
            on_progress(AgentProgress("tool_call", 2, "NCS 검색", "구내통신 (limit 20)"))
        if self.error is not None:
            raise self.error
        return AgentDraftResult(
            field_values=(("채용분야", "통신설비 운영"),),
            unit_codes=("2002010210_25v5",),
            notes=("대관 업무 근거 없음",),
            turns=4,
            duration_ms=1234,
            tool_calls=2,
        )


def _agent_events(response: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_agent_draft_streams_progress_then_the_result() -> None:
    runner = FakeAgentRunner()
    client, _, _, _ = _client(agent_runners={"claude": runner})

    response = client.post(
        "/api/agent-draft",
        json={"job_title": "통신설비 운영", "duties": ["구내 전화 설비 운영"]},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["X-NCS-JD-Generation-Mode"] == "agent_loop"
    events = _agent_events(response)
    assert [event["event"] for event in events] == ["progress", "progress", "result"]
    assert events[1]["label"] == "NCS 검색"
    assert events[1]["detail"] == "구내통신 (limit 20)"
    assert events[-1]["unit_codes"] == ["2002010210_25v5"]
    assert events[-1]["notes"][0] == "대관 업무 근거 없음"


def test_agent_draft_defaults_to_the_supported_template_labels() -> None:
    runner = FakeAgentRunner()
    client, _, _, _ = _client(agent_runners={"claude": runner})

    client.post(
        "/api/agent-draft",
        json={"job_title": "통신설비 운영", "duties": ["구내 전화 설비 운영", " "]},
    )

    request = runner.requests[0]
    # 대분류~세분류 are the one part of the supported set the run never writes:
    # they are read back from the units it adopts.
    assert request.template_labels == tuple(
        label for label in SUPPORTED_TEMPLATE_LABELS if not label.endswith("분류")
    )
    assert "대분류" not in request.template_labels
    # Blank duties are dropped rather than sent to a run that costs minutes.
    assert request.duties == ("구내 전화 설비 운영",)


def test_agent_draft_fills_the_classification_from_the_units_it_adopted() -> None:
    """The four classification cells must come from NCS, not from the model."""

    source = FakeSource()
    source.candidate = ScopeCandidate(
        classification_path="전기·전자 > 전기 > 전기공사 > 내선공사",
        duty_definition=None,
        unit_code="2002010210_25v5",
        unit_name="배선공사",
        unit_level="2",
        unit_definition=None,
        major_code="19",
        major_name="전기·전자",
        middle_code="01",
        middle_name="전기",
        small_code="07",
        small_name="전기공사",
        sub_code="01",
        sub_name="내선공사",
    )
    client, _, _, _ = _client(source=source, agent_runners={"claude": FakeAgentRunner()})

    response = client.post(
        "/api/agent-draft",
        json={"job_title": "통신설비 운영", "duties": ["구내 전화 설비 운영"]},
    )

    result = _agent_events(response)[-1]
    assert result["field_values"] == [
        ["채용분야", "통신설비 운영"],
        ["대분류", "19. 전기·전자"],
        ["중분류", "01. 전기"],
        ["소분류", "07. 전기공사"],
        ["세분류", "01. 내선공사"],
    ]
    # The classification stays silent about units it could confirm.
    assert result["notes"] == ["대관 업무 근거 없음"]


def test_agent_draft_reports_a_unit_whose_classification_is_unconfirmed() -> None:
    """A missing lookup leaves a note rather than an invented classification."""

    client, _, _, _ = _client(agent_runners={"claude": FakeAgentRunner()})

    response = client.post(
        "/api/agent-draft",
        json={"job_title": "통신설비 운영", "duties": ["구내 전화 설비 운영"]},
    )

    result = _agent_events(response)[-1]
    assert result["field_values"] == [["채용분야", "통신설비 운영"]]
    assert result["notes"] == [
        "대관 업무 근거 없음",
        "다음 능력단위는 NCS에서 분류체계를 다시 확인하지 못해 분류 표기에서 빠졌습니다: 2002010210_25v5",
    ]


def test_agent_draft_reports_a_failure_as_a_final_error_event() -> None:
    runner = FakeAgentRunner(error=AgentDraftError("llm_login_required"))
    client, _, _, _ = _client(agent_runners={"claude": runner})

    response = client.post(
        "/api/agent-draft",
        json={"job_title": "통신설비 운영", "duties": ["구내 전화 설비 운영"]},
    )

    assert response.status_code == 200
    events = _agent_events(response)
    assert events[-1] == {
        "event": "error",
        "code": "llm_login_required",
        "message": "공식 CLI 로그인이 필요합니다.",
    }


def test_agent_draft_is_unavailable_without_a_runner() -> None:
    client, _, _, _ = _client()

    response = client.post(
        "/api/agent-draft",
        json={"job_title": "통신설비 운영", "duties": ["구내 전화 설비 운영"]},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "agent_runner_unavailable"


def test_agent_draft_runs_on_the_engine_the_client_chose() -> None:
    """Whoever the person logged into is who must actually run the draft."""

    claude = FakeAgentRunner()
    codex = FakeAgentRunner()
    client, _, _, _ = _client(agent_runners={"claude": claude, "codex": codex})

    response = client.post(
        "/api/agent-draft",
        json={
            "job_title": "통신설비 운영",
            "duties": ["구내 전화 설비 운영"],
            "provider": "codex",
        },
    )

    assert response.status_code == 200
    assert response.headers["x-ncs-jd-ai-provider"] == "codex"
    assert codex.requests and not claude.requests


def test_agent_draft_refuses_an_engine_that_is_not_wired() -> None:
    client, _, _, _ = _client(agent_runners={"claude": FakeAgentRunner()})

    response = client.post(
        "/api/agent-draft",
        json={
            "job_title": "통신설비 운영",
            "duties": ["구내 전화 설비 운영"],
            "provider": "codex",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "agent_runner_unavailable"


class FakeTemplateInspector:
    def __init__(self, inspection: TemplateInspection | None = None) -> None:
        self.calls: list[HwpxTemplate] = []
        self.error: Exception | None = None
        self.inspection = inspection or TemplateInspection(
            source_format="hwpx",
            confidence=0.9,
            fields=(
                TemplateField(label="항목", value_preview="내용", row=0, col=0),
                TemplateField(label="채용분야", row=1, col=0),
                TemplateField(label="직무수행내용", row=2, col=0),
                TemplateField(label="가" * 80, row=3, col=0),
            ),
        )

    def inspect_template(self, template: HwpxTemplate) -> TemplateInspection:
        self.calls.append(template)
        if self.error:
            raise self.error
        return self.inspection


def test_template_schema_returns_the_labels_the_agent_should_fill() -> None:
    inspector = FakeTemplateInspector()
    client, _, _, _ = _client(template_inspector=inspector)

    response = client.post(
        "/api/template/schema",
        files={"template": ("기관양식.hwpx", b"template-bytes", "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    # The column header and the over-long prose cell are both excluded.
    assert body["labels"] == ["채용분야", "직무수행내용"]
    assert body["detected_field_count"] == 4
    assert body["usable_label_count"] == 2
    assert inspector.calls[0].source_name == "기관양식.hwpx"


def test_template_schema_is_unavailable_without_an_inspector() -> None:
    client, _, _, _ = _client()

    response = client.post(
        "/api/template/schema",
        files={"template": ("기관양식.hwpx", b"template-bytes", "application/octet-stream")},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "template_inspector_unavailable"


def test_template_schema_reports_an_inspection_failure_safely() -> None:
    inspector = FakeTemplateInspector()
    inspector.error = KordocRendererError("kordoc stack trace")
    client, _, _, _ = _client(template_inspector=inspector)

    response = client.post(
        "/api/template/schema",
        files={"template": ("기관양식.hwpx", b"template-bytes", "application/octet-stream")},
    )

    assert response.status_code == 503
    assert "kordoc stack trace" not in response.text


def test_agent_draft_uses_the_uploaded_form_labels_over_the_standard_set() -> None:
    runner = FakeAgentRunner()
    client, _, _, _ = _client(agent_runners={"claude": runner})

    client.post(
        "/api/agent-draft",
        json={
            "job_title": "통신설비 운영",
            "duties": ["구내 전화 설비 운영"],
            "template_labels": ["담당업무", " 담당업무 ", "요구역량", "  "],
        },
    )

    request = runner.requests[0]
    assert request.template_labels == ("담당업무", "요구역량")


def test_agent_draft_forwards_reviewed_preferences() -> None:
    runner = FakeAgentRunner()
    client, _, _, _ = _client(agent_runners={"claude": runner})

    client.post(
        "/api/agent-draft",
        json={
            "job_title": "통신설비 운영",
            "duties": ["구내 전화 설비 운영"],
            "preferences": ["관련 자격 우대", " "],
        },
    )

    assert runner.requests[0].preferences == ("관련 자격 우대",)


def test_agent_export_renders_the_reviewed_fields_verbatim() -> None:
    client, _, _, renderer = _client()

    response = client.post(
        "/api/agent-draft/export/hwpx",
        json={
            "job_title": "통신설비 운영",
            "fields": [
                {"label": "채용분야", "value": " 통신설비 운영 "},
                {"label": "대분류", "value": "20. 정보통신 / 19. 전기·전자"},
            ],
        },
    )

    assert response.status_code == 200
    assert response.content == b"safe-hwpx-bytes"
    assert response.headers["X-NCS-JD-Generation-Mode"] == "agent_loop"
    fields, job_title, _, template = renderer.field_calls[0]
    # A reviewer's edits reach the renderer unchanged; only padding is trimmed.
    assert fields == (
        ("채용분야", "통신설비 운영"),
        ("대분류", "20. 정보통신 / 19. 전기·전자"),
    )
    assert job_title == "통신설비 운영"
    assert template is None


def test_agent_export_rejects_a_duplicated_label() -> None:
    client, _, _, renderer = _client()

    response = client.post(
        "/api/agent-draft/export/hwpx",
        json={
            "job_title": "통신설비 운영",
            "fields": [
                {"label": "채용분야", "value": "가"},
                {"label": "채용분야", "value": "나"},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_agent_export_request"
    assert renderer.field_calls == []


def test_agent_export_reports_a_renderer_failure_safely() -> None:
    renderer = FakeRenderer()
    renderer.error = InvalidHwpxError("bad container")
    client, _, _, _ = _client(renderer=renderer)

    response = client.post(
        "/api/agent-draft/export/hwpx",
        json={"job_title": "통신설비 운영", "fields": [{"label": "채용분야", "value": "가"}]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_hwpx"
    # The renderer's internal message must not reach the client.
    assert "bad container" not in response.text


def test_one_request_rejects_unknown_provider() -> None:
    workflow = FakeWorkflow()
    client, _, _, renderer = _client(workflow, AutomaticSource(), FakeRenderer())

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "gemini",
            "announcement_text": "담당 업무\n- 설비 상태 점검",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_generate_request"
    assert workflow.parse_calls == []
    assert renderer.calls == []


def test_cli_provider_without_a_registered_selector_is_refused() -> None:
    """A CLI provider must fail loudly rather than quietly drafting without it."""

    workflow = FakeWorkflow()
    client, _, _, renderer = _client(workflow, AutomaticSource(), FakeRenderer())

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "codex",
            "announcement_text": "담당 업무\n- 설비 상태 점검",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "scope_selector_unavailable"
    assert workflow.parse_calls == []
    assert renderer.calls == []


def test_generate_rejects_a_provider_that_is_no_longer_offered() -> None:
    """The API-key path is gone; asking for it must fail before any parsing."""

    client, workflow, _, _ = _client(FakeWorkflow(), AutomaticSource(), FakeRenderer())

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "openai",
            "announcement_text": "담당 업무: 설비 점검",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_generate_request"
    assert workflow.parse_calls == []


def test_generate_ignores_a_stray_api_key_field() -> None:
    """A stale client must not be able to smuggle a credential into a request."""

    mapper = FakeTemplateMapper()
    client, _, _, _ = _client(
        FakeWorkflow(),
        AutomaticSource(),
        FakeRenderer(),
        cli_template_mapper=mapper,
    )
    api_key = "sk-test-12345678901234567890"

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "off",
            "announcement_text": "담당 업무\n- 설비 상태 점검",
            "openai_api_key": api_key,
        },
        files={"template": ("example.hwp", b"fake-template", "application/octet-stream")},
    )

    assert response.status_code == 200
    # The mapper's signature has no credential parameter at all, so there is
    # nowhere for a supplied key to land.
    assert mapper.calls[0] == (mapper.calls[0][0], mapper.calls[0][1])
    assert api_key.encode() not in response.content


def test_cli_template_mapping_runs_without_an_api_key() -> None:
    mapper = FakeTemplateMapper(provider="claude", model="claude-cli")
    renderer = FakeRenderer()
    client, _, _, _ = _client(
        FakeWorkflow(),
        AutomaticSource(),
        renderer,
        cli_template_mapper=mapper,
        scope_selectors={"codex": _FallbackSelector()},
    )

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "codex",
            "announcement_text": "담당 업무\n- 설비 상태 점검",
        },
        files={"template": ("example.hwp", b"fake-template", "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.headers["x-ncs-jd-template-mapping"] == "claude"
    assert response.headers["x-ncs-jd-template-mapping-model"] == "claude-cli"
    assert len(mapper.calls[0]) == 2
    assert renderer.calls[0][2].field_values == (("담당 업무", "검증된 수행 업무"),)


def test_local_generation_maps_an_uploaded_form_through_the_cli_when_present() -> None:
    mapper = FakeTemplateMapper(provider="claude", model="claude-cli")
    renderer = FakeRenderer()
    client, _, _, _ = _client(
        FakeWorkflow(),
        AutomaticSource(),
        renderer,
        cli_template_mapper=mapper,
    )

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "off",
            "announcement_text": "담당 업무\n- 설비 상태 점검",
        },
        files={"template": ("example.hwp", b"fake-template", "application/octet-stream")},
    )

    assert response.status_code == 200
    assert response.headers["x-ncs-jd-ai-provider"] == "off"
    assert response.headers["x-ncs-jd-template-mapping"] == "claude"
    assert len(mapper.calls[0]) == 2
    assert renderer.calls[0][2].field_values == (("담당 업무", "검증된 수행 업무"),)


def test_uploaded_template_must_be_applied_instead_of_silent_fallback() -> None:
    renderer = FakeRenderer(
        TemplateCapability(
            requested=True,
            supported=True,
            used=False,
            mode="standard_generate",
            matched_fields=("채용분야",),
            unmatched_fields=("필요기술",),
            fallback_reason="template_fields_unmatched",
        )
    )
    client, _, _, _ = _client(FakeWorkflow(), AutomaticSource(), renderer)

    response = client.post(
        "/api/generate-job-description",
        data={
            "document_id": str(DOCUMENT_ID),
            "created_at": CREATED_AT,
            "provider": "off",
            "announcement_text": "담당 업무\n- 설비 상태 점검",
        },
        files={"template": ("template.hwpx", b"fake-template", "application/octet-stream")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "template_not_applied"
    assert response.json()["error"]["diagnostics"]["fallback_reason"] == "template_fields_unmatched"


def test_one_request_rejects_missing_or_ambiguous_announcement_source() -> None:
    client, workflow, _, _ = _client()
    metadata = {
        "document_id": str(DOCUMENT_ID),
        "created_at": CREATED_AT,
        "provider": "off",
    }

    missing = client.post("/api/generate-job-description", data=metadata)
    ambiguous = client.post(
        "/api/generate-job-description",
        data={**metadata, "announcement_text": "담당 업무: 점검"},
        files={"announcement": ("announcement.txt", b"file", "text/plain")},
    )

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "announcement_required"
    assert ambiguous.status_code == 422
    assert ambiguous.json()["error"]["code"] == "announcement_source_ambiguous"
    assert workflow.parse_calls == []


def test_draft_requires_confirmation_and_builds_domain_request_from_caller_values() -> None:
    client, workflow, _, _ = _client()

    rejected = client.post("/api/drafts", json=_draft_payload(confirmed=False))
    assert rejected.status_code == 422
    assert workflow.draft_calls == []

    response = client.post("/api/drafts", json=_draft_payload())

    assert response.status_code == 200
    assert len(workflow.draft_calls) == 1
    request = workflow.draft_calls[0]
    assert request.document_id == DOCUMENT_ID
    assert request.created_at == datetime.fromisoformat(CREATED_AT)
    assert request.retrieved_at == datetime.fromisoformat(RETRIEVED_AT)
    assert request.created_at.utcoffset() is not None
    assert request.classification_paths == (
        ClassificationPath(
            major_code="19",
            middle_code="01",
            small_code="07",
            sub_code="01",
            label="전기전자 > 전기 > 전기공사 > 내선공사",
        ),
    )
    assert request.included_units == (
        IncludedUnit(
            unit_code="UNIT-A",
            unit_name="시설 점검",
            unit_level="4",
            selection_reason="사용자 확인",
        ),
    )
    assert request.organization_input.responsibilities == ("점검 일정을 관리한다.",)
    assert request.organization_input.kpis == ("점검 이행률",)
    assert request.excluded_unit_codes == ("UNIT-X",)
    assert request.excluded_task_terms == ("채용",)
    assert request.mcp_url_label == "local-test-mcp"


def test_draft_json_preserves_references_and_partial_failure_diagnostics() -> None:
    client, _, _, _ = _client()

    response = client.post("/api/drafts", json=_draft_payload())

    assert response.status_code == 200
    payload = response.json()
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["job_profile"] == fixture
    assert payload["job_profile"]["job_description"]["tasks"][0]["source_refs"] == fixture[
        "job_description"
    ]["tasks"][0]["source_refs"]
    assert payload["diagnostics"]["loaded_unit_codes"] == ["UNIT-A"]
    assert payload["diagnostics"]["failed_unit_codes"] == ["UNIT-B"]
    assert payload["diagnostics"]["unit_evidence"][1]["retryable"] is True


def test_export_validates_profile_decodes_template_and_returns_attachment_headers() -> None:
    client, _, _, renderer = _client()
    template_bytes = b"fake-template"

    response = client.post(
        "/api/drafts/export/hwpx",
        json={
            "job_profile": json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
            "filename": "requested.hwpx",
            "template": {
                "filename": "source.hwpx",
                "content_base64": base64.b64encode(template_bytes).decode("ascii"),
            },
        },
    )

    assert response.status_code == 200
    assert response.content == b"safe-hwpx-bytes"
    assert response.headers["content-type"].startswith("application/vnd.hancom.hwpx")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["x-hwpx-validation-entries"] == "7"
    assert response.headers["x-hwpx-template-mode"] == "hwpx-preserve"
    assert response.headers["x-hwpx-template-used"] == "true"
    assert "x-hwpx-template-fallback" not in response.headers
    profile, filename, template = renderer.calls[0]
    assert filename == "requested.hwpx"
    assert template == HwpxTemplate("source.hwpx", template_bytes)
    assert profile.job_description.tasks[0].source_refs == _profile().job_description.tasks[0].source_refs


def test_template_size_is_rejected_before_renderer_boundary_call() -> None:
    client, _, _, renderer = _client(max_template_bytes=3)

    response = client.post(
        "/api/drafts/export/hwpx",
        json={
            "job_profile": json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
            "template": {
                "filename": "source.hwpx",
                "content_base64": base64.b64encode(b"1234").decode("ascii"),
            },
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "document_render_too_large"
    assert renderer.calls == []


def test_structured_boundary_errors_are_safe_and_do_not_echo_payload_or_stderr() -> None:
    secret = "SECRET-PAYLOAD-AND-STDERR"
    workflow = FakeWorkflow()
    source = FakeSource()
    renderer = FakeRenderer()
    client, _, _, _ = _client(workflow, source, renderer)

    workflow.parse_error = DocumentParseTimeoutError(secret)
    parsed = client.post(
        "/api/extract",
        files={"announcement": ("secret.pdf", secret.encode(), "application/pdf")},
    )
    assert parsed.status_code == 503
    assert parsed.json()["error"]["code"] == "document_parse_timeout"
    assert secret not in parsed.text

    source.error = NcsSourceUnavailableError(secret, operation="search_scope_candidates")
    searched = client.get("/api/ncs/search", params={"query": "facility"})
    assert searched.status_code == 503
    assert searched.json()["error"]["code"] == "ncs_source_unavailable"
    assert secret not in searched.text

    workflow.draft_error = DraftingWorkflowError(secret, diagnostics=_diagnostics())
    drafted = client.post("/api/drafts", json=_draft_payload())
    assert drafted.status_code == 503
    assert drafted.json()["error"]["code"] == "drafting_workflow_error"
    assert drafted.json()["error"]["diagnostics"]["failed_unit_codes"] == ["UNIT-B"]
    assert secret not in drafted.text

    renderer.error = KordocRendererError(secret, code="bridge_process_error")
    exported = client.post(
        "/api/drafts/export/hwpx",
        json={"job_profile": json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))},
    )
    assert exported.status_code == 503
    assert exported.json()["error"]["code"] == "bridge_process_error"
    assert secret not in exported.text
