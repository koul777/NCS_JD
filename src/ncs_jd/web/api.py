"""Dependency-injected FastAPI routes for deterministic NCS drafting.

The router owns no source, parser, renderer, clock, UUID generator, or draft
storage.  Those capabilities are supplied by the application factory so that
this HTTP boundary cannot silently call an external service or retain user
documents between requests.  Every optional AI capability -- NCS scope
selection, template field-name mapping, and the agent draft loop -- runs
through an official CLI's own subscription login, so no route accepts a
credential from a caller.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
import re
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Body, File, Form, Header, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from ncs_jd.application.document_parser import (
    DocumentFormatMismatchError,
    DocumentParseTimeoutError,
    DocumentParserError,
    DocumentTooLargeError,
    InvalidParserResponseError,
    NodeUnavailableError,
    UnsupportedDocumentError,
)
from ncs_jd.application.agent_drafting import (
    CLASSIFICATION_FIELD_LABELS,
    MAX_DOCUMENT_CHARS,
    MAX_TEMPLATE_LABELS,
    AgentDraftError,
    AgentDraftPort,
    AgentDraftRequest,
    AgentDraftResult,
    AgentProgress,
    classification_field_values,
    template_labels_from_inspection,
)
from ncs_jd.application.document_renderer import (
    SUPPORTED_TEMPLATE_LABELS,
    DocumentRenderError,
    DocumentRenderTimeoutError,
    DocumentRendererPort,
    DocumentRenderTooLargeError,
    HwpxTemplate,
    InvalidHwpxError,
    InvalidRendererResponseError,
    InvalidTemplateError,
    RendererNodeUnavailableError,
)
from ncs_jd.application.automatic_drafting import (
    AutomaticDraftPlanningError,
    plan_automatic_scope,
)
from ncs_jd.application.drafting_workflow import (
    ConfirmedDraftRequest,
    DraftingWorkflow,
    DraftingWorkflowError,
)
from ncs_jd.application.job_profile_assembler import OrganizationInput
from ncs_jd.application.llm_rewriter import LlmCliError, LlmCliPort, LlmProvider
from ncs_jd.application.llm_scope_selection import ScopeSelectorPort
from ncs_jd.application.ncs_source import (
    NcsSourceError,
    NcsSourcePort,
    OptionalReference,
    ScopeCandidate,
)
from ncs_jd.application.template_mapping import (
    TemplateInspectorPort,
    TemplateMappingError,
    TemplateMappingPort,
)
from ncs_jd.domain.job_profile import ClassificationPath, IncludedUnit, JobProfile


DEFAULT_MAX_ANNOUNCEMENT_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TEMPLATE_BYTES = 25 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 64 * 1024
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
LOCAL_ACTION_HEADER = "X-NCS-JD-Local-Action"


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClassificationPathInput(_ApiModel):
    major_code: str = Field(min_length=1, max_length=200)
    middle_code: str = Field(min_length=1, max_length=200)
    small_code: str = Field(min_length=1, max_length=200)
    sub_code: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1)


class IncludedUnitInput(_ApiModel):
    unit_code: str = Field(min_length=1, max_length=200)
    unit_name: str = Field(min_length=1)
    unit_level: str | None = None
    selection_reason: str = Field(min_length=1)


class OrganizationInputPayload(_ApiModel):
    purpose_supplement: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    decision_authority: list[str] = Field(default_factory=list)
    kpis: list[str] = Field(default_factory=list)
    collaboration: list[str] = Field(default_factory=list)
    reporting_relationships: list[str] = Field(default_factory=list)
    experience_requirements: list[str] = Field(default_factory=list)
    education_requirements: list[str] = Field(default_factory=list)
    qualification_requirements: list[str] = Field(default_factory=list)
    preference_requirements: list[str] = Field(default_factory=list)


class OptionalReferenceInput(_ApiModel):
    reference_id: str = Field(min_length=1)
    unit_code: str = Field(min_length=1)
    kind: Literal["career_path", "qualification", "job_base"]
    text_raw: str = Field(min_length=1)
    evidence_grade: Literal["reference"] = "reference"


class ConfirmedDraftPayload(_ApiModel):
    """Explicit human-confirmation gate and complete deterministic inputs."""

    confirmed: Literal[True]
    document_id: UUID
    created_at: AwareDatetime
    retrieved_at: AwareDatetime
    organization_job_title: str = Field(min_length=1)
    classification_paths: list[ClassificationPathInput] = Field(min_length=1)
    included_units: list[IncludedUnitInput] = Field(min_length=1)
    organization_input: OrganizationInputPayload = Field(default_factory=OrganizationInputPayload)
    excluded_unit_codes: list[str] = Field(default_factory=list)
    excluded_task_terms: list[str] = Field(default_factory=list)
    selected_references: list[OptionalReferenceInput] = Field(default_factory=list)
    target_level_input: str | None = None
    mcp_url_label: str = Field(default="local-ncs-mcp", min_length=1)


class TemplateUploadPayload(_ApiModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)


class HwpxExportPayload(_ApiModel):
    job_profile: JobProfile
    filename: str | None = Field(default=None, max_length=255)
    template: TemplateUploadPayload | None = None


class RewriteSuggestionsPayload(_ApiModel):
    provider: LlmProvider = LlmProvider.OFF
    job_profile: JobProfile


class AgentDraftPayload(_ApiModel):
    """Human-confirmed announcement facts for the agent-loop draft.

    Duties arrive already reviewed by a person, which is the point of the
    two-step flow: the agent spends minutes searching, so it must not spend them
    on a misread announcement.
    """

    job_title: str = Field(min_length=1, max_length=200)
    duties: list[str] = Field(min_length=1, max_length=40)
    qualifications: list[str] = Field(default_factory=list, max_length=40)
    preferences: list[str] = Field(default_factory=list, max_length=40)
    organization_context: str = Field(default="", max_length=1000)
    template_labels: list[str] = Field(default_factory=list, max_length=40)
    # Which official CLI drives the loop.  The client picks it explicitly so a
    # person who logged into one provider is not silently run on the other.
    provider: Literal["claude", "codex"] = "claude"


class AgentFieldPayload(_ApiModel):
    label: str = Field(min_length=1, max_length=100)
    # A single reviewed field may hold up to the whole document budget; the
    # renderer enforces the real output byte ceiling.
    value: str = Field(default="", max_length=MAX_DOCUMENT_CHARS)


class AgentExportPayload(_ApiModel):
    """An agent-composed field document on its way to HWPX.

    The client returns the field set it was shown, so a reviewer can correct the
    text before exporting.  Nothing here is re-derived from the agent run.
    """

    job_title: str = Field(min_length=1, max_length=200)
    fields: list[AgentFieldPayload] = Field(min_length=1, max_length=MAX_TEMPLATE_LABELS)
    filename: str | None = Field(default=None, max_length=255)
    template: TemplateUploadPayload | None = None


class AutomaticGenerateMetadata(_ApiModel):
    document_id: UUID
    created_at: AwareDatetime
    provider: Literal["off", "claude", "codex"] = "off"


def create_api_router(
    *,
    workflow: DraftingWorkflow,
    ncs_source: NcsSourcePort,
    renderer: DocumentRendererPort,
    cli_template_mapper: TemplateMappingPort | None = None,
    scope_selectors: Mapping[str, ScopeSelectorPort] | None = None,
    agent_runners: Mapping[str, AgentDraftPort] | None = None,
    template_inspector: TemplateInspectorPort | None = None,
    max_announcement_bytes: int = DEFAULT_MAX_ANNOUNCEMENT_BYTES,
    max_template_bytes: int = DEFAULT_MAX_TEMPLATE_BYTES,
) -> APIRouter:
    """Build an API router around caller-owned application dependencies.

    ``scope_selectors`` maps a CLI provider name to the selector that picks NCS
    units for it.  Absent an entry, generation stays on the deterministic path.
    ``agent_runners`` maps the same provider names to agent-loop runners; a name
    missing from it simply cannot be chosen for an agent draft.
    """

    if max_announcement_bytes < 1 or max_template_bytes < 1:
        raise ValueError("upload size limits must be positive")
    available_selectors = dict(scope_selectors or {})
    available_agents = dict(agent_runners or {})

    router = APIRouter(prefix="/api", tags=["drafting"])

    @router.post("/extract")
    async def extract_announcement(
        announcement: Annotated[UploadFile, File(...)],
    ) -> Any:
        try:
            content = await _read_upload_bounded(
                announcement,
                max_bytes=max_announcement_bytes,
                too_large_code="document_too_large",
            )
            extraction = await workflow.parse_announcement(
                announcement.filename or "",
                content,
            )
        except DocumentParserError as exc:
            return _parser_error_response(exc)
        except Exception:
            return _safe_error(503, "document_parse_error", "공고문을 처리할 수 없습니다.")
        return _dataclass_json(extraction)

    @router.post("/generate-job-description")
    async def generate_job_description(
        document_id: Annotated[str, Form(...)],
        created_at: Annotated[str, Form(...)],
        provider: Annotated[str, Form(...)],
        announcement: Annotated[UploadFile | None, File()] = None,
        announcement_text: Annotated[str | None, Form()] = None,
        job_title: Annotated[str | None, Form()] = None,
        template: Annotated[UploadFile | None, File()] = None,
    ) -> Response:
        """Run Kordoc → deterministic NCS scope/evidence → HWPX in one request."""

        try:
            metadata = AutomaticGenerateMetadata.model_validate(
                {"document_id": document_id, "created_at": created_at, "provider": provider}
            )
        except (ValidationError, ValueError, TypeError):
            return _safe_error(422, "invalid_generate_request", "생성 요청이 올바르지 않습니다.")
        if metadata.provider in {"claude", "codex"} and metadata.provider not in available_selectors:
            return _safe_error(
                503,
                "scope_selector_unavailable",
                "선택한 공식 CLI 제공자로 능력단위를 선정할 수 없습니다.",
            )
        try:
            pasted_text = (announcement_text or "").strip()
            if announcement is not None and pasted_text:
                return _safe_error(
                    422,
                    "announcement_source_ambiguous",
                    "공고문 파일과 붙여넣기 중 하나만 선택해 주세요.",
                )
            if announcement is None and not pasted_text:
                return _safe_error(
                    422,
                    "announcement_required",
                    "공고문 파일을 선택하거나 공고 내용을 붙여넣어 주세요.",
                )
            if announcement is not None:
                content = await _read_upload_bounded(
                    announcement,
                    max_bytes=max_announcement_bytes,
                    too_large_code="document_too_large",
                )
                announcement_name = announcement.filename or "announcement.txt"
            else:
                content = pasted_text.encode("utf-8")
                if len(content) > max_announcement_bytes:
                    return _safe_error(
                        413,
                        "document_too_large",
                        "붙여넣은 공고문이 허용 크기를 초과했습니다.",
                    )
                announcement_name = "pasted-announcement.txt"
            template_content = (
                await _read_upload_bounded(
                    template,
                    max_bytes=max_template_bytes,
                    too_large_code="template_too_large",
                )
                if template is not None
                else None
            )
            extraction = await workflow.parse_announcement(announcement_name, content)
        except DocumentParserError as exc:
            if exc.code == "template_too_large":
                return _safe_error(413, exc.code, "업로드한 양식이 허용 크기를 초과했습니다.")
            return _parser_error_response(exc)
        except Exception:
            return _safe_error(503, "document_parse_error", "공고문을 처리할 수 없습니다.")

        try:
            plan = await plan_automatic_scope(
                extraction,
                ncs_source,
                job_title_override=job_title,
                scope_selector=available_selectors.get(metadata.provider),
            )
        except AutomaticDraftPlanningError as exc:
            return _safe_error(422, exc.code, str(exc))
        except NcsSourceError as exc:
            return _safe_error(503, exc.code, "NCS 검색 서비스를 사용할 수 없습니다.")
        except Exception:
            return _safe_error(503, "automatic_scope_error", "NCS 자동 매칭을 완료할 수 없습니다.")

        request = ConfirmedDraftRequest(
            document_id=metadata.document_id,
            created_at=metadata.created_at,
            retrieved_at=metadata.created_at,
            organization_job_title=plan.title,
            classification_paths=plan.classification_paths,
            included_units=plan.included_units,
            organization_input=OrganizationInput(
                purpose_supplement=" ".join(
                    item.text.strip()
                    for item in plan.role.recruitment_reasons
                    if item.text.strip()
                ) or None,
                responsibilities=plan.duties,
                qualification_requirements=tuple(
                    item.text.strip()
                    for item in plan.role.qualifications
                    if item.text.strip()
                ),
                preference_requirements=tuple(
                    item.text.strip()
                    for item in plan.role.preferences
                    if item.text.strip()
                ),
            ),
            scope_confirmation_required=True,
            scope_match_notes=plan.match_notes,
        )
        try:
            generated = await workflow.generate_draft(request)
        except DraftingWorkflowError as exc:
            return _safe_error(
                503,
                exc.code,
                "NCS 근거로 초안을 생성할 수 없습니다.",
                diagnostics=_safe_workflow_diagnostics(exc),
            )
        except Exception:
            return _safe_error(503, "drafting_workflow_error", "초안을 생성할 수 없습니다.")

        # NCS scope, evidence, and generated facts are always deterministic.
        # A CLI provider may only select equivalent labels in an uploaded form;
        # it cannot rewrite this validated JobProfile.
        profile = generated.job_profile

        hwpx_template: HwpxTemplate | None = (
            HwpxTemplate(template.filename or "template.hwpx", template_content)
            if template is not None and template_content is not None
            else None
        )
        mapping_model: str | None = None
        mapping_provider = "none"
        if hwpx_template is not None and cli_template_mapper is not None:
            try:
                mapping = await asyncio.to_thread(
                    cli_template_mapper.map_fields,
                    profile,
                    hwpx_template,
                )
            except TemplateMappingError:
                mapping = None
            except Exception:
                mapping = None
            if mapping is not None:
                hwpx_template = HwpxTemplate(
                    hwpx_template.source_name,
                    hwpx_template.content,
                    mapping.field_values,
                )
                mapping_model = mapping.model
                mapping_provider = mapping.provider
        try:
            rendered = await _render_in_thread(
                renderer,
                profile,
                filename=f"NCS_직무기술서_{plan.title}.hwpx",
                template=hwpx_template,
            )
        except DocumentRenderError as exc:
            return _renderer_error_response(exc)
        except Exception:
            return _safe_error(503, "document_render_error", "HWPX 문서를 생성할 수 없습니다.")

        if hwpx_template is not None and not rendered.template_capability.used:
            return _safe_error(
                422,
                "template_not_applied",
                "업로드한 예시 양식을 안전하게 적용하지 못했습니다. 양식의 필드 이름과 빈 입력칸을 확인해 주세요.",
                diagnostics={
                    "mode": rendered.template_capability.mode,
                    "fallback_reason": rendered.template_capability.fallback_reason,
                    "matched_fields": list(rendered.template_capability.matched_fields),
                    "unmatched_fields": list(rendered.template_capability.unmatched_fields),
                    "ambiguous_fields": list(rendered.template_capability.ambiguous_fields),
                },
            )

        filename = _safe_attachment_filename(rendered.filename)
        headers = {
            "Content-Disposition": _content_disposition(filename),
            "X-HWPX-Validation-Entries": str(rendered.validation.entry_count),
            "X-HWPX-Template-Mode": rendered.template_capability.mode,
            "X-HWPX-Template-Used": str(rendered.template_capability.used).lower(),
            "X-NCS-JD-AI-Provider": metadata.provider,
            "X-NCS-JD-Generation-Mode": "deterministic",
            # Scope selection is the only step a CLI provider may influence, and
            # it silently falls back, so report what actually ran.
            "X-NCS-JD-Scope-Selection": plan.selection_mode,
            "X-NCS-JD-Template-Mapping": mapping_provider if mapping_provider != "none" else metadata.provider,
            "X-NCS-JD-Selected-Units": str(len(plan.included_units)),
            "X-NCS-JD-Selected-Subcategories": str(len(plan.classification_paths)),
        }
        if rendered.template_capability.fallback_reason:
            headers["X-HWPX-Template-Fallback"] = rendered.template_capability.fallback_reason
        if mapping_model:
            headers["X-NCS-JD-Template-Mapping-Model"] = mapping_model
        return Response(content=rendered.content, media_type=rendered.media_type, headers=headers)

    @router.post("/agent-draft")
    async def agent_draft(payload: Annotated[AgentDraftPayload, Body(...)]) -> Any:
        """Stream the agent loop as newline-delimited JSON progress events.

        The run takes minutes and its step count is not known in advance, so the
        client receives each tool call as it happens and renders an ordered list
        instead of a fabricated percentage.  A streamed POST body is used rather
        than SSE because ``EventSource`` cannot POST, and a job registry would
        add server state this local-only app does not otherwise keep.
        """

        agent_runner = available_agents.get(payload.provider)
        if agent_runner is None:
            return _safe_error(
                503,
                "agent_runner_unavailable",
                "선택한 공식 CLI로 에이전트 초안을 생성할 수 없습니다.",
            )
        duties = tuple(dict.fromkeys(item.strip() for item in payload.duties if item.strip()))
        if not duties:
            return _safe_error(422, "duties_required", "직무수행내역을 한 건 이상 확인해 주세요.")
        labels = tuple(
            dict.fromkeys(item.strip() for item in payload.template_labels if item.strip())
        ) or SUPPORTED_TEMPLATE_LABELS
        # The classification cells are read back from the units the run adopts,
        # so the agent is neither asked for them nor allowed to return them.
        labels = tuple(
            label
            for label in labels
            if "".join(label.split()) not in CLASSIFICATION_FIELD_LABELS
        )
        try:
            request = AgentDraftRequest(
                job_title=payload.job_title.strip(),
                duties=duties,
                template_labels=labels,
                qualifications=tuple(
                    item.strip() for item in payload.qualifications if item.strip()
                ),
                preferences=tuple(item.strip() for item in payload.preferences if item.strip()),
                organization_context=payload.organization_context.strip(),
            )
        except ValueError:
            return _safe_error(422, "invalid_agent_request", "에이전트 초안 요청이 올바르지 않습니다.")

        return StreamingResponse(
            _stream_agent_draft(agent_runner, request, ncs_source),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "X-NCS-JD-Generation-Mode": "agent_loop",
                "X-NCS-JD-AI-Provider": payload.provider,
            },
        )

    @router.post("/template/schema")
    async def inspect_template_schema(
        template: Annotated[UploadFile, File(...)],
    ) -> Any:
        """Report the item labels an uploaded institution form expects.

        The agent writes into whatever labels it is handed, so this is what lets
        a user's own form drive the output instead of the built-in standard set.
        The labels are returned to the client rather than applied silently: a
        heuristic form reading is exactly the kind of thing a person should see
        before a run that costs minutes commits to it.
        """

        if template_inspector is None:
            return _safe_error(
                503,
                "template_inspector_unavailable",
                "양식 항목을 읽을 수 없습니다.",
            )
        try:
            content = await _read_upload_bounded(
                template,
                max_bytes=max_template_bytes,
                too_large_code="template_too_large",
            )
            uploaded = HwpxTemplate(source_name=template.filename or "template", content=content)
        except DocumentRenderTooLargeError as exc:
            return _renderer_error_response(exc)
        except (ValueError, TypeError):
            return _safe_error(422, "invalid_template", "양식 파일이 유효하지 않습니다.")

        try:
            inspection = await asyncio.to_thread(template_inspector.inspect_template, uploaded)
        except DocumentRenderError as exc:
            return _renderer_error_response(exc)
        except Exception:
            return _safe_error(503, "template_inspect_failed", "양식 항목을 읽을 수 없습니다.")

        labels = template_labels_from_inspection(inspection.fields)
        return {
            "source_format": inspection.source_format,
            "confidence": inspection.confidence,
            "labels": list(labels),
            "detected_field_count": len(inspection.fields),
            "usable_label_count": len(labels),
        }

    @router.post("/agent-draft/export/hwpx")
    async def export_agent_draft_hwpx(
        raw_payload: Annotated[dict[str, Any], Body(...)],
    ) -> Response:
        """Turn a reviewed agent field document into HWPX.

        This is a separate route from ``/drafts/export/hwpx`` because that one
        renders a ``JobProfile``, and the agent path never produces one -- see
        ``field_values_to_markdown`` for why reconstructing a profile from the
        agent's own prose would be inventing structure it never asserted.
        """

        try:
            payload = AgentExportPayload.model_validate(raw_payload)
            template = _decode_template(payload.template, max_bytes=max_template_bytes)
        except (ValidationError, ValueError, TypeError):
            return _safe_error(
                422,
                "invalid_agent_export_request",
                "직무기술서 내보내기 입력이 유효하지 않습니다.",
            )
        except DocumentRenderTooLargeError as exc:
            return _renderer_error_response(exc)

        fields = tuple((item.label.strip(), item.value.strip()) for item in payload.fields)
        if len({label for label, _ in fields}) != len(fields) or not all(
            label for label, _ in fields
        ):
            return _safe_error(
                422,
                "invalid_agent_export_request",
                "직무기술서 내보내기 입력이 유효하지 않습니다.",
            )

        try:
            rendered = await asyncio.to_thread(
                renderer.render_fields,
                fields,
                job_title=payload.job_title.strip(),
                filename=payload.filename,
                template=template,
            )
        except DocumentRenderError as exc:
            return _renderer_error_response(exc)
        except (ValueError, TypeError):
            return _safe_error(
                422,
                "invalid_agent_export_request",
                "직무기술서 내보내기 입력이 유효하지 않습니다.",
            )
        except Exception:
            return _safe_error(503, "document_render_error", "HWPX 문서를 생성할 수 없습니다.")

        filename = _safe_attachment_filename(rendered.filename)
        headers = {
            "Content-Disposition": _content_disposition(filename),
            "X-HWPX-Validation-Entries": str(rendered.validation.entry_count),
            "X-HWPX-Template-Mode": rendered.template_capability.mode,
            "X-HWPX-Template-Used": str(rendered.template_capability.used).lower(),
            "X-NCS-JD-Generation-Mode": "agent_loop",
        }
        if rendered.template_capability.fallback_reason:
            headers["X-HWPX-Template-Fallback"] = rendered.template_capability.fallback_reason
        return Response(content=rendered.content, media_type=rendered.media_type, headers=headers)

    @router.get("/ncs/search")
    async def search_ncs(
        query: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> Any:
        normalized_query = query.strip()
        if not normalized_query:
            return _safe_error(422, "invalid_search_query", "검색어를 입력해 주세요.")
        try:
            candidates = await ncs_source.search_scope_candidates(normalized_query, limit)
        except NcsSourceError as exc:
            return _safe_error(503, exc.code, "NCS 검색 서비스를 사용할 수 없습니다.")
        except Exception:
            return _safe_error(503, "ncs_source_error", "NCS 검색 서비스를 사용할 수 없습니다.")
        return {"candidates": [_dataclass_json(candidate) for candidate in candidates]}

    @router.post("/drafts")
    async def create_draft(
        raw_payload: Annotated[dict[str, Any], Body(...)],
    ) -> Any:
        try:
            payload = ConfirmedDraftPayload.model_validate(raw_payload)
            request = _to_confirmed_request(payload)
        except (ValidationError, ValueError, TypeError):
            return _safe_error(
                422,
                "invalid_confirmed_draft_request",
                "확정된 초안 생성 입력이 유효하지 않습니다.",
            )
        try:
            result = await workflow.generate_draft(request)
        except DraftingWorkflowError as exc:
            return _safe_error(
                503,
                exc.code,
                "선택한 NCS 근거로 초안을 생성할 수 없습니다.",
                diagnostics=_safe_workflow_diagnostics(exc),
            )
        except (ValueError, TypeError):
            return _safe_error(422, "invalid_draft_input", "초안 생성 입력이 유효하지 않습니다.")
        except Exception:
            return _safe_error(503, "drafting_workflow_error", "초안을 생성할 수 없습니다.")
        return {
            "job_profile": result.job_profile.model_dump(mode="json", by_alias=True),
            "diagnostics": result.diagnostics.as_dict(),
        }

    @router.post("/drafts/export/hwpx")
    async def export_hwpx(
        raw_payload: Annotated[dict[str, Any], Body(...)],
    ) -> Response:
        try:
            payload = HwpxExportPayload.model_validate(raw_payload)
            template = _decode_template(payload.template, max_bytes=max_template_bytes)
        except (ValidationError, ValueError, TypeError):
            return _safe_error(
                422,
                "invalid_hwpx_export_request",
                "HWPX 내보내기 입력이 유효하지 않습니다.",
            )
        except DocumentRenderTooLargeError as exc:
            return _renderer_error_response(exc)

        try:
            rendered = await _render_in_thread(
                renderer,
                payload.job_profile,
                filename=payload.filename,
                template=template,
            )
        except DocumentRenderError as exc:
            return _renderer_error_response(exc)
        except (ValueError, TypeError):
            return _safe_error(422, "invalid_hwpx_export_request", "HWPX 내보내기 입력이 유효하지 않습니다.")
        except Exception:
            return _safe_error(503, "document_render_error", "HWPX 문서를 생성할 수 없습니다.")

        filename = _safe_attachment_filename(rendered.filename)
        headers = {
            "Content-Disposition": _content_disposition(filename),
            "X-HWPX-Validation-Entries": str(rendered.validation.entry_count),
            "X-HWPX-Template-Mode": rendered.template_capability.mode,
            "X-HWPX-Template-Used": str(rendered.template_capability.used).lower(),
        }
        if rendered.template_capability.fallback_reason:
            headers["X-HWPX-Template-Fallback"] = rendered.template_capability.fallback_reason
        return Response(content=rendered.content, media_type=rendered.media_type, headers=headers)

    return router


def create_llm_api_router(*, llm_cli: LlmCliPort) -> APIRouter:
    """Build the optional CLI status/login/rewrite API.

    This is a separate router so provider login remains available even while a
    drafting dependency such as NCS MCP is temporarily unavailable.
    """

    router = APIRouter(prefix="/api/llm", tags=["llm-rewrite"])

    @router.get("/providers")
    async def list_providers() -> Any:
        try:
            statuses = await asyncio.to_thread(llm_cli.list_provider_statuses)
        except Exception:
            return _llm_error(503, "provider_status_unavailable", retryable=True)
        return {
            "default_provider": LlmProvider.OFF.value,
            "providers": [status.as_dict() for status in statuses],
        }

    @router.post("/providers/{provider}/login", status_code=202)
    async def start_provider_login(
        provider: str,
        local_action: Annotated[str | None, Header(alias=LOCAL_ACTION_HEADER)] = None,
    ) -> Any:
        if local_action != "login":
            return _llm_error(403, "local_action_required", retryable=False)
        try:
            selected = LlmProvider(provider)
        except ValueError:
            return _llm_error(404, "unknown_provider", retryable=False)
        try:
            result = await asyncio.to_thread(llm_cli.start_login, selected)
        except LlmCliError as exc:
            return _llm_error(_llm_error_status(exc.code), exc.code, retryable=exc.retryable)
        except Exception:
            return _llm_error(503, "login_launch_failed", retryable=True)
        return result.as_dict()

    @router.post("/rewrite-suggestions")
    async def rewrite_suggestions(
        raw_payload: Annotated[dict[str, Any], Body(...)],
        local_action: Annotated[str | None, Header(alias=LOCAL_ACTION_HEADER)] = None,
    ) -> Any:
        if local_action != "rewrite":
            return _llm_error(403, "local_action_required", retryable=False)
        try:
            payload = RewriteSuggestionsPayload.model_validate(raw_payload)
        except (ValidationError, ValueError, TypeError):
            return _llm_error(422, "invalid_rewrite_request", retryable=False)
        try:
            result = await asyncio.to_thread(
                llm_cli.suggest_rewrites,
                payload.job_profile,
                payload.provider,
            )
        except LlmCliError as exc:
            return _llm_error(_llm_error_status(exc.code), exc.code, retryable=exc.retryable)
        except Exception:
            return _llm_error(503, "rewrite_provider_failed", retryable=True)
        return result.as_dict()

    return router


async def _resolve_agent_classification(
    ncs_source: NcsSourcePort,
    result: AgentDraftResult,
) -> AgentDraftResult:
    """Add the 대분류~세분류 cells the adopted units resolve to.

    Reading the codes back is what makes these cells evidence rather than prose:
    they come from the same records the agent cited, not from its answer.  Codes
    the source cannot confirm are left out and reported, because a classification
    that quietly disagrees with the unit list beside it is worse than a gap.
    """

    codes = tuple(dict.fromkeys(result.unit_codes))[:MAX_TEMPLATE_LABELS]
    if not codes:
        return result

    async def lookup(code: str) -> ScopeCandidate | None:
        try:
            found = await ncs_source.search_scope_candidates(code, limit=5)
        except (NcsSourceError, ValueError):
            return None
        return next((item for item in found if item.unit_code == code), None)

    resolved = await asyncio.gather(*(lookup(code) for code in codes))
    values = classification_field_values(item for item in resolved if item is not None)

    notes = result.notes
    unresolved = [code for code, item in zip(codes, resolved, strict=True) if item is None]
    if unresolved:
        # Said out loud because the form keeps whatever those cells already held,
        # which on a filled sample form is another job's classification.
        notes = (
            *notes,
            "다음 능력단위는 NCS에서 분류체계를 다시 확인하지 못해 분류 표기에서 빠졌습니다: "
            + ", ".join(unresolved),
        )
    taken = {label for label, _ in result.field_values}
    merged = result.field_values + tuple(pair for pair in values if pair[0] not in taken)
    return replace(result, field_values=merged, notes=notes)


async def _stream_agent_draft(
    runner: AgentDraftPort,
    request: AgentDraftRequest,
    ncs_source: NcsSourcePort | None = None,
) -> AsyncIterator[bytes]:
    """Bridge the blocking, callback-based runner to an async NDJSON stream.

    ``run_draft`` blocks for minutes and reports progress on the worker thread,
    so events are handed back to the event loop through a queue.  The loop below
    forwards them as they arrive; without it the client would see nothing until
    the whole run finished, which is the exact experience this endpoint exists
    to avoid.
    """

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

    def on_progress(event: AgentProgress) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("progress", event))

    def work() -> None:
        try:
            result = runner.run_draft(request, on_progress)
        except AgentDraftError as exc:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", exc.code))
        except Exception:
            loop.call_soon_threadsafe(queue.put_nowait, ("error", "agent_draft_failed"))
        else:
            loop.call_soon_threadsafe(queue.put_nowait, ("result", result))

    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "progress" and isinstance(payload, AgentProgress):
                body: dict[str, object] = {"event": "progress", **payload.as_dict()}
            elif kind == "result" and isinstance(payload, AgentDraftResult):
                if ncs_source is not None:
                    payload = await _resolve_agent_classification(ncs_source, payload)
                body = {"event": "result", **payload.as_dict()}
            else:
                code = str(payload)
                body = {
                    "event": "error",
                    "code": code,
                    "message": _agent_error_message(code),
                }
            yield (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")
            if kind in {"result", "error"}:
                return
    finally:
        # A disconnected client must not leave the CLI subprocess unobserved.
        if not task.done():
            await task


def _agent_error_message(code: str) -> str:
    return {
        "provider_not_installed": "공식 CLI가 설치되어 있지 않습니다.",
        "provider_unavailable": "공식 CLI를 실행할 수 없습니다.",
        "llm_login_required": "공식 CLI 로그인이 필요합니다.",
        "llm_usage_exhausted": "제공자 사용량 한도에 도달했습니다.",
        "agent_timeout": "탐색이 제한 시간을 초과했습니다.",
        "agent_result_not_json": "에이전트 결과를 해석하지 못했습니다.",
        "agent_produced_no_result": "에이전트가 결과를 만들지 못했습니다.",
    }.get(code, "에이전트 초안을 생성하지 못했습니다.")


async def _render_in_thread(
    renderer: DocumentRendererPort,
    profile: JobProfile,
    *,
    filename: str | None,
    template: HwpxTemplate | None,
):
    # Renderer ports are synchronous because their adapters own a bounded
    # subprocess.  Keep that boundary off the event loop.
    import asyncio

    return await asyncio.to_thread(
        renderer.render,
        profile,
        filename=filename,
        template=template,
    )


async def _read_upload_bounded(
    upload: UploadFile,
    *,
    max_bytes: int,
    too_large_code: str,
) -> bytes:
    content = bytearray()
    while True:
        chunk = await upload.read(min(_UPLOAD_CHUNK_BYTES, max_bytes - len(content) + 1))
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_bytes:
            raise DocumentTooLargeError(
                "upload exceeds the configured parser boundary",
                code=too_large_code,
            )
    return bytes(content)


def _to_confirmed_request(payload: ConfirmedDraftPayload) -> ConfirmedDraftRequest:
    return ConfirmedDraftRequest(
        document_id=payload.document_id,
        created_at=payload.created_at,
        retrieved_at=payload.retrieved_at,
        organization_job_title=payload.organization_job_title,
        classification_paths=tuple(
            ClassificationPath(**item.model_dump()) for item in payload.classification_paths
        ),
        included_units=tuple(IncludedUnit(**item.model_dump()) for item in payload.included_units),
        organization_input=OrganizationInput(
            purpose_supplement=payload.organization_input.purpose_supplement,
            responsibilities=tuple(payload.organization_input.responsibilities),
            decision_authority=tuple(payload.organization_input.decision_authority),
            kpis=tuple(payload.organization_input.kpis),
            collaboration=tuple(payload.organization_input.collaboration),
            reporting_relationships=tuple(payload.organization_input.reporting_relationships),
            experience_requirements=tuple(payload.organization_input.experience_requirements),
            education_requirements=tuple(payload.organization_input.education_requirements),
            qualification_requirements=tuple(
                payload.organization_input.qualification_requirements
            ),
            preference_requirements=tuple(payload.organization_input.preference_requirements),
        ),
        excluded_unit_codes=tuple(payload.excluded_unit_codes),
        excluded_task_terms=tuple(payload.excluded_task_terms),
        selected_references=tuple(
            OptionalReference(**item.model_dump()) for item in payload.selected_references
        ),
        target_level_input=payload.target_level_input,
        mcp_url_label=payload.mcp_url_label,
    )


def _decode_template(
    payload: TemplateUploadPayload | None,
    *,
    max_bytes: int,
) -> HwpxTemplate | None:
    if payload is None:
        return None
    encoded = payload.content_base64.strip()
    # Reject before decoding when the base64 representation cannot possibly fit.
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 4:
        raise DocumentRenderTooLargeError("template exceeds the renderer boundary")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid template base64") from exc
    if len(content) > max_bytes:
        raise DocumentRenderTooLargeError("template exceeds the renderer boundary")
    return HwpxTemplate(source_name=payload.filename, content=content)


def _dataclass_json(value: Any) -> dict[str, Any]:
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError("application response must be a dataclass instance")
    return jsonable_encoder(asdict(value))


def _parser_error_response(exc: DocumentParserError) -> JSONResponse:
    if isinstance(exc, DocumentTooLargeError):
        return _safe_error(413, exc.code, "업로드한 공고문이 허용 크기를 초과했습니다.")
    if isinstance(exc, (UnsupportedDocumentError, DocumentFormatMismatchError)):
        return _safe_error(415, exc.code, "지원되는 공고문 형식과 파일 내용을 확인해 주세요.")
    if isinstance(exc, (DocumentParseTimeoutError, NodeUnavailableError, InvalidParserResponseError)):
        return _safe_error(503, exc.code, "공고문 분석 서비스를 사용할 수 없습니다.")
    return _safe_error(422, exc.code, "공고문을 분석할 수 없습니다.")


def _renderer_error_response(exc: DocumentRenderError) -> JSONResponse:
    if isinstance(exc, DocumentRenderTooLargeError):
        return _safe_error(413, exc.code, "HWPX 입력 또는 출력이 허용 크기를 초과했습니다.")
    if isinstance(exc, (InvalidTemplateError, InvalidHwpxError)):
        return _safe_error(422, exc.code, "HWPX 템플릿 또는 문서가 유효하지 않습니다.")
    if isinstance(
        exc,
        (DocumentRenderTimeoutError, RendererNodeUnavailableError, InvalidRendererResponseError),
    ):
        return _safe_error(503, exc.code, "HWPX 생성 서비스를 사용할 수 없습니다.")
    return _safe_error(503, exc.code, "HWPX 문서를 생성할 수 없습니다.")


def _safe_workflow_diagnostics(exc: DraftingWorkflowError) -> dict[str, Any]:
    diagnostics = exc.diagnostics
    return {
        "loaded_unit_codes": list(diagnostics.loaded_unit_codes),
        "failed_unit_codes": list(diagnostics.failed_unit_codes),
        "unit_evidence": [
            {
                "unit_code": item.unit_code,
                "status": item.status,
                "code": item.code,
                "message": (
                    "NCS 근거를 불러왔습니다."
                    if item.status == "loaded"
                    else "NCS 근거를 불러오지 못했습니다."
                ),
                "retryable": item.retryable,
                "source_warning_codes": list(item.source_warning_codes),
            }
            for item in diagnostics.unit_evidence
        ],
    }


def _safe_error(
    status_code: int,
    code: str,
    message: str,
    *,
    diagnostics: dict[str, Any] | None = None,
    retryable: bool | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if diagnostics is not None:
        error["diagnostics"] = diagnostics
    if retryable is not None:
        error["retryable"] = retryable
    return JSONResponse(status_code=status_code, content={"error": error})


def _llm_error(status_code: int, code: str, *, retryable: bool) -> JSONResponse:
    messages = {
        "unknown_provider": "지원하지 않는 재서술 제공자입니다.",
        "local_action_required": "로컬 작업 확인 헤더가 필요합니다.",
        "provider_off": "LLM OFF 상태에서는 로그인을 시작하지 않습니다.",
        "provider_not_installed": "선택한 공식 CLI가 설치되어 있지 않습니다.",
        "login_already_running": "해당 CLI 로그인 창이 이미 열려 있습니다.",
        "login_launch_failed": "공식 CLI 로그인 창을 시작하지 못했습니다.",
        "llm_login_required": "선택한 CLI에 로그인이 필요합니다.",
        "llm_usage_exhausted": "선택한 CLI의 사용 한도를 확인해 주세요.",
        "rewrite_timeout": "재서술 제안 시간이 초과되었습니다.",
        "rewrite_input_too_large": "재서술 입력이 허용 크기를 초과했습니다.",
        "rewrite_output_too_large": "재서술 출력이 허용 크기를 초과했습니다.",
        "invalid_rewrite_response": "안전 검증을 통과한 재서술 제안이 없습니다.",
        "invalid_rewrite_request": "재서술 요청 형식이 올바르지 않습니다.",
        "provider_status_unavailable": "CLI 상태를 확인할 수 없습니다.",
        "provider_unavailable": "선택한 CLI를 실행할 수 없습니다.",
        "rewrite_provider_failed": "선택한 CLI가 재서술 제안을 완료하지 못했습니다.",
    }
    return _safe_error(
        status_code,
        code,
        messages.get(code, "재서술 기능을 사용할 수 없습니다."),
        retryable=retryable,
    )


def _llm_error_status(code: str) -> int:
    return {
        "provider_off": 400,
        "unknown_provider": 404,
        "provider_not_installed": 409,
        "login_already_running": 409,
        "llm_login_required": 401,
        "llm_usage_exhausted": 429,
        "rewrite_timeout": 504,
        "rewrite_input_too_large": 413,
        "rewrite_output_too_large": 502,
        "invalid_rewrite_response": 502,
    }.get(code, 503)


def _safe_attachment_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name
    name = _CONTROL_CHARACTERS.sub("", name).replace('"', "_").strip().rstrip(". ")
    if not name.casefold().endswith(".hwpx"):
        name = f"{name or 'ncs_job_profile'}.hwpx"
    return name[:180] or "ncs_job_profile.hwpx"


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
    if not ascii_name or ascii_name == ".hwpx":
        ascii_name = "ncs_job_profile.hwpx"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


__all__ = [
    "ConfirmedDraftPayload",
    "DEFAULT_MAX_ANNOUNCEMENT_BYTES",
    "DEFAULT_MAX_TEMPLATE_BYTES",
    "HwpxExportPayload",
    "LOCAL_ACTION_HEADER",
    "create_api_router",
    "RewriteSuggestionsPayload",
    "create_llm_api_router",
]
