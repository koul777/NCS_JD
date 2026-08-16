from __future__ import annotations

import base64
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from ncs_jd.adapters.kordoc_hwpx_renderer import (
    KordocHwpxRenderer,
    RendererBridgeProcessResult,
)
from ncs_jd.application.document_renderer import (
    DocumentRendererPort,
    DocumentRenderTimeoutError,
    HwpxTemplate,
    InvalidHwpxError,
    InvalidRendererResponseError,
    KordocRendererError,
    RendererNodeUnavailableError,
)
from ncs_jd.domain.job_profile import JobProfile


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


def _profile() -> JobProfile:
    return JobProfile.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


def _minimal_hwpx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", "<container />")
        archive.writestr("Contents/content.hpf", "<package />")
        archive.writestr("Contents/header.xml", '<head secCnt="1" />')
        archive.writestr("Contents/section0.xml", "<section />")
    return output.getvalue()


def _capability(*, requested: bool = False, reason: str | None = None) -> dict[str, object]:
    return {
        "requested": requested,
        "supported": False,
        "used": False,
        "mode": "standard_generate",
        "fallback_reason": reason,
        "matched_fields": [],
        "unmatched_fields": [],
        "ambiguous_fields": [],
    }


def _generate_response(
    content: bytes | None = None,
    *,
    capability: dict[str, object] | None = None,
) -> dict[str, object]:
    content = content if content is not None else _minimal_hwpx()
    return {
        "ok": True,
        "result": {
            "content_base64": base64.b64encode(content).decode("ascii"),
            "validation": {"ok": True, "entry_count": 5, "issue_count": 0},
            "template_capability": capability or _capability(),
        },
    }


class FakeRunner:
    def __init__(self, responses: dict[str, object] | list[dict[str, object]]) -> None:
        self.responses = responses if isinstance(responses, list) else [responses]
        self.requests: list[dict[str, object]] = []
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        command: tuple[str, ...],
        *,
        input_text: str,
        timeout_seconds: float,
    ) -> RendererBridgeProcessResult:
        del timeout_seconds
        self.commands.append(tuple(command))
        self.requests.append(json.loads(input_text))
        response = self.responses[len(self.requests) - 1]
        return RendererBridgeProcessResult(0, json.dumps(response, ensure_ascii=False))


def test_generate_request_contains_only_markdown_contract_and_validates_response() -> None:
    runner = FakeRunner(_generate_response())
    renderer = KordocHwpxRenderer(
        bridge_path=Path("scripts/kordoc_hwpx_bridge.mjs"),
        runner=runner,
    )

    result = renderer.render(_profile(), filename="../../bad:name?.HWPX")

    assert isinstance(renderer, DocumentRendererPort)
    request = runner.requests[0]
    assert request["action"] == "generate"
    assert request["max_output_bytes"] == 25 * 1024 * 1024
    assert "NCS 기반 채용 직무 설명자료 : 채용 담당자" in str(request["markdown"])
    assert "profile" not in request
    assert "document_id" not in request
    assert result.filename == "bad_name.hwpx"
    assert result.content == _minimal_hwpx()
    assert result.validation.valid is True
    assert result.template_capability.requested is False


@pytest.mark.parametrize(
    "response",
    [
        {
            "ok": True,
            "result": {
                "content_base64": "not base64!",
                "validation": {"ok": True, "entry_count": 5, "issue_count": 0},
                "template_capability": _capability(),
            },
        },
        _generate_response(b"PK\x03\x04not-a-zip"),
    ],
)
def test_invalid_generated_payload_is_rejected(response: dict[str, object]) -> None:
    renderer = KordocHwpxRenderer(runner=FakeRunner(response))

    with pytest.raises((InvalidRendererResponseError, InvalidHwpxError)):
        renderer.render(_profile())


def test_kordoc_failure_is_structured_without_exposing_stderr() -> None:
    runner = FakeRunner(
        {
            "ok": False,
            "error": {"code": "invalid_hwpx", "message": "HWPX 구조 검증에 실패했습니다."},
        }
    )

    with pytest.raises(KordocRendererError) as caught:
        KordocHwpxRenderer(runner=runner).render(_profile())

    assert caught.value.as_dict() == {
        "code": "invalid_hwpx",
        "message": "HWPX 구조 검증에 실패했습니다.",
    }


def test_pdf_template_is_sent_for_safe_structural_rebuild() -> None:
    capability = _capability(requested=True, reason="template_format_not_supported")
    runner = FakeRunner(_generate_response(capability=capability))
    template = HwpxTemplate("reference.pdf", b"%PDF-1.7 template")

    result = KordocHwpxRenderer(runner=runner).render(_profile(), template=template)

    request = runner.requests[0]["template"]
    assert request["format"] == "pdf"
    assert base64.b64decode(request["content_base64"]) == b"%PDF-1.7 template"
    assert request["field_values"]["채용분야"] == "채용 담당자"
    assert result.template_capability.requested is True
    assert result.template_capability.used is False
    assert result.template_capability.fallback_reason == "template_format_not_supported"


def test_template_field_override_is_used_without_changing_profile_facts() -> None:
    runner = FakeRunner(_generate_response(capability=_capability(requested=True)))
    template = HwpxTemplate(
        "reference.hwp",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1template",
        (("담당 업무", "검증된 수행 업무"),),
    )

    KordocHwpxRenderer(runner=runner).render(_profile(), template=template)

    request = runner.requests[0]["template"]
    assert request["field_values"] == {"담당 업무": "검증된 수행 업무"}


def test_render_fields_sends_agent_pairs_without_a_job_profile() -> None:
    runner = FakeRunner(_generate_response())
    fields = (("채용분야", "통신설비 운영"), ("대분류", "20. 정보통신 / 19. 전기·전자"))

    result = KordocHwpxRenderer(runner=runner).render_fields(
        fields,
        job_title="통신설비 운영",
    )

    request = runner.requests[0]
    assert "profile" not in request
    assert "통신설비 운영" in request["markdown"]
    assert "20. 정보통신 / 19. 전기·전자" in request["markdown"]
    assert result.filename.endswith(".hwpx")


def test_render_fields_fills_an_uploaded_form_from_the_agent_pairs() -> None:
    runner = FakeRunner(_generate_response(capability=_capability(requested=True)))
    fields = (("채용분야", "통신설비 운영"), ("담당 업무", "구내 전화 설비 운영"))
    template = HwpxTemplate(
        "reference.hwp",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1template",
    )

    KordocHwpxRenderer(runner=runner).render_fields(
        fields,
        job_title="통신설비 운영",
        template=template,
    )

    request = runner.requests[0]["template"]
    assert request["field_values"] == {
        "채용분야": "통신설비 운영",
        "담당 업무": "구내 전화 설비 운영",
    }
    runner = FakeRunner(
        {
            "ok": True,
            "result": {
                "format": "hwp",
                "confidence": 0.95,
                "fields": [
                    {
                        "label": "직무\n수행\n내용",
                        "value_preview": "기존 예시",
                        "empty": False,
                        "row": 5,
                        "col": 0,
                    }
                ],
            },
        }
    )
    template = HwpxTemplate(
        "reference.hwp",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1template",
    )

    inspection = KordocHwpxRenderer(runner=runner).inspect_template(template)

    assert runner.requests[0]["action"] == "inspect_template"
    assert inspection.source_format == "hwp"
    assert inspection.fields[0].label == "직무\n수행\n내용"
    assert inspection.fields[0].value_preview == "기존 예시"


def test_hwpx_template_request_uses_exact_fields_and_reports_unmatched_fallback() -> None:
    capability = {
        **_capability(requested=True, reason="template_fields_unmatched"),
        "matched_fields": ["채용분야"],
        "unmatched_fields": ["필요기술"],
    }
    runner = FakeRunner(_generate_response(capability=capability))
    template_bytes = _minimal_hwpx()

    result = KordocHwpxRenderer(runner=runner).render(
        _profile(),
        template=HwpxTemplate("reference.hwpx", template_bytes),
    )

    template_request = runner.requests[0]["template"]
    assert isinstance(template_request, dict)
    assert template_request["format"] == "hwpx"
    assert base64.b64decode(str(template_request["content_base64"])) == template_bytes
    assert template_request["field_values"]["채용분야"] == "채용 담당자"
    assert result.template_capability.matched_fields == ("채용분야",)
    assert result.template_capability.unmatched_fields == ("필요기술",)
    assert result.template_capability.fallback_reason == "template_fields_unmatched"


def test_ambiguous_template_capability_is_preserved_as_an_explicit_fallback() -> None:
    capability = {
        **_capability(requested=True, reason="template_fields_ambiguous"),
        "ambiguous_fields": ["채용분야"],
    }
    result = KordocHwpxRenderer(runner=FakeRunner(_generate_response(capability=capability))).render(
        _profile(),
        template=HwpxTemplate("reference.hwpx", _minimal_hwpx()),
    )

    assert result.template_capability.used is False
    assert result.template_capability.ambiguous_fields == ("채용분야",)
    assert result.template_capability.fallback_reason == "template_fields_ambiguous"


def test_render_preview_uses_restricted_render_action() -> None:
    runner = FakeRunner(
        {
            "ok": True,
            "result": {
                "svg": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
                "page_count": 1,
                "warnings": ["검토 필요"],
            },
        }
    )
    content = _minimal_hwpx()

    preview = KordocHwpxRenderer(runner=runner).render_preview(content)

    assert runner.requests[0]["action"] == "render"
    assert base64.b64decode(str(runner.requests[0]["content_base64"])) == content
    assert preview.page_count == 1
    assert preview.warnings == ("검토 필요",)


class TimeoutRunner:
    def run(self, *args: object, **kwargs: object) -> RendererBridgeProcessResult:
        raise subprocess.TimeoutExpired("node", 1)


class MissingNodeRunner:
    def run(self, *args: object, **kwargs: object) -> RendererBridgeProcessResult:
        raise FileNotFoundError("node")


def test_timeout_is_structured() -> None:
    with pytest.raises(DocumentRenderTimeoutError) as caught:
        KordocHwpxRenderer(runner=TimeoutRunner(), timeout_seconds=1).render(_profile())

    assert caught.value.as_dict()["code"] == "document_render_timeout"


def test_missing_node_is_structured() -> None:
    with pytest.raises(RendererNodeUnavailableError) as caught:
        KordocHwpxRenderer(runner=MissingNodeRunner()).render(_profile())

    assert caught.value.as_dict()["code"] == "node_unavailable"
