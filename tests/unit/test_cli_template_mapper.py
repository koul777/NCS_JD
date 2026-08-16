from __future__ import annotations

from pathlib import Path

import pytest

from ncs_jd.adapters.cli_template_mapper import CliTemplateMapper
from ncs_jd.application.document_renderer import HwpxTemplate
from ncs_jd.application.llm_rewriter import LlmCliError, LlmProvider
from ncs_jd.application.template_mapping import (
    TemplateField,
    TemplateInspection,
    TemplateMappingError,
)
from ncs_jd.domain.job_profile import JobProfile


FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "job_profile_v1.json"


def _profile() -> JobProfile:
    return JobProfile.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


class FakeInspector:
    def inspect_template(self, template: HwpxTemplate) -> TemplateInspection:
        assert template.source_name == "example.hwp"
        return TemplateInspection(
            source_format="hwp",
            confidence=1.0,
            fields=(
                TemplateField("채용\n분야", "전기", False, 0, 0),
                TemplateField("담당 업무", "기존 예시", False, 5, 0),
            ),
        )


class FakeCli:
    def __init__(self, payload: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {
            "mappings": [
                {"template_label": "채용\n분야", "source_key": "채용분야", "confidence": "high"},
                {"template_label": "담당 업무", "source_key": "직무수행내용", "confidence": "high"},
            ]
        }
        self.error = error
        self.prompts: list[str] = []

    def complete_structured(self, provider, prompt, schema, expected_key):
        self.prompts.append(prompt)
        self.provider = provider
        self.schema = schema
        self.expected_key = expected_key
        if self.error is not None:
            raise self.error
        return self.payload


def test_cli_mapping_adds_aliases_without_sending_generated_values() -> None:
    cli = FakeCli()
    mapper = CliTemplateMapper(FakeInspector(), cli, provider=LlmProvider.CLAUDE)  # type: ignore[arg-type]

    result = mapper.map_fields(_profile(), HwpxTemplate("example.hwp", b"template"))

    values = dict(result.field_values)
    assert values["채용\n분야"] == values["채용분야"] == "채용 담당자"
    assert values["담당 업무"] == values["직무수행내용"]
    assert result.provider == "claude"
    assert result.model == "claude-cli"
    assert "채용 담당자" not in cli.prompts[0]
    assert cli.expected_key == "mappings"
    assert "enum" in cli.schema["properties"]["mappings"]["items"]["properties"]["source_key"]


def test_cli_login_errors_are_sanitized() -> None:
    cli = FakeCli(error=LlmCliError("llm_login_required", retryable=True))
    mapper = CliTemplateMapper(FakeInspector(), cli, provider=LlmProvider.CLAUDE)  # type: ignore[arg-type]

    with pytest.raises(TemplateMappingError) as caught:
        mapper.map_fields(_profile(), HwpxTemplate("example.hwp", b"template"))

    assert caught.value.code == "llm_login_required"
    assert "로그인" in str(caught.value)


def test_low_confidence_cli_mapping_is_rejected() -> None:
    cli = FakeCli(
        payload={
            "mappings": [
                {
                    "template_label": "담당 업무",
                    "source_key": "직무수행내용",
                    "confidence": "low",
                }
            ]
        }
    )
    mapper = CliTemplateMapper(FakeInspector(), cli, provider=LlmProvider.CODEX)  # type: ignore[arg-type]

    with pytest.raises(TemplateMappingError) as caught:
        mapper.map_fields(_profile(), HwpxTemplate("example.hwp", b"template"))

    assert caught.value.code == "cli_template_mapping_empty"
