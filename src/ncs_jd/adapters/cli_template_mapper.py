"""Map uploaded form labels with an official CLI's subscription login.

The CLI is already used for rewrite suggestions and NCS scope selection.  This
adapter reuses that structured-output path, so an institution form can be filled
without the app ever asking a person for a key.  The model may only choose a
canonical source key for a label the inspector already extracted.
"""

from __future__ import annotations

import json
from ncs_jd.adapters.cli_llm import CliLlmAdapter
from ncs_jd.application.document_renderer import (
    HwpxTemplate,
    job_profile_to_template_values,
)
from ncs_jd.application.llm_rewriter import LlmCliError, LlmProvider
from ncs_jd.application.template_mapping import (
    TemplateInspectorPort,
    TemplateMappingError,
    TemplateMappingResult,
    apply_high_confidence_mappings,
    template_mapping_json_schema,
    template_mapping_user_payload,
)
from ncs_jd.domain.job_profile import JobProfile


_SYSTEM_PROMPT = (
    "Map Korean NCS job-description template labels to the supplied canonical "
    "source keys. Return only semantically equivalent mappings. Do not invent "
    "fields or content. Use high confidence only for an unambiguous equivalent "
    "label."
)


class CliTemplateMapper:
    """TemplateMappingPort backed by Codex or Claude CLI login, not an API key."""

    def __init__(
        self,
        inspector: TemplateInspectorPort,
        adapter: CliLlmAdapter,
        *,
        provider: LlmProvider = LlmProvider.CLAUDE,
    ) -> None:
        if provider not in {LlmProvider.CLAUDE, LlmProvider.CODEX}:
            raise ValueError("CLI template mapping requires Claude or Codex")
        self._inspector = inspector
        self._adapter = adapter
        self._provider = provider

    def map_fields(
        self,
        profile: JobProfile,
        template: HwpxTemplate,
    ) -> TemplateMappingResult:
        inspection = self._inspector.inspect_template(template)
        source_values = job_profile_to_template_values(profile)
        target_labels = tuple(dict.fromkeys(field.label for field in inspection.fields))
        if not target_labels:
            raise TemplateMappingError(
                "template_fields_missing",
                "업로드한 양식에서 대응할 필드를 찾지 못했습니다.",
            )

        schema = template_mapping_json_schema(tuple(source_values), target_labels)
        user_payload = template_mapping_user_payload(tuple(source_values), inspection.fields)
        prompt = (
            _SYSTEM_PROMPT
            + "\n\nINPUT="
            + json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            payload = self._adapter.complete_structured(
                self._provider,
                prompt,
                schema,
                "mappings",
            )
        except LlmCliError as exc:
            raise TemplateMappingError(_mapping_code(exc.code), _mapping_message(exc.code)) from exc
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise TemplateMappingError(
                "cli_template_mapping_invalid",
                "공식 CLI 양식 매핑 응답을 검증할 수 없습니다.",
            ) from exc

        aliases = apply_high_confidence_mappings(
            source_values,
            target_labels,
            payload.get("mappings"),
            invalid_code="cli_template_mapping_invalid",
            empty_code="cli_template_mapping_empty",
            ambiguous_code="cli_template_mapping_ambiguous",
        )
        merged = dict(source_values)
        merged.update(aliases)
        return TemplateMappingResult(
            provider=self._provider.value,
            model=f"{self._provider.value}-cli",
            field_values=tuple(merged.items()),
            mapped_labels=tuple(aliases),
        )


def _mapping_code(code: str) -> str:
    known = {
        "provider_not_installed": "cli_template_mapper_unavailable",
        "provider_unavailable": "cli_template_mapper_unavailable",
        "llm_login_required": "llm_login_required",
        "llm_usage_exhausted": "llm_usage_exhausted",
        "rewrite_timeout": "cli_template_mapping_timeout",
        "rewrite_input_too_large": "cli_template_mapping_invalid",
        "rewrite_output_too_large": "cli_template_mapping_invalid",
    }
    return known.get(code, "cli_template_mapping_failed")


def _mapping_message(code: str) -> str:
    messages = {
        "cli_template_mapper_unavailable": "공식 CLI 양식 매핑을 사용할 수 없습니다.",
        "llm_login_required": "공식 CLI 로그인이 필요합니다.",
        "llm_usage_exhausted": "공식 CLI 사용량 한도에 도달했습니다.",
        "cli_template_mapping_timeout": "공식 CLI 양식 매핑이 시간 제한을 초과했습니다.",
        "cli_template_mapping_invalid": "공식 CLI 양식 매핑 응답을 검증할 수 없습니다.",
    }
    return messages.get(_mapping_code(code), "공식 CLI 양식 필드 매핑을 완료할 수 없습니다.")


__all__ = ["CliTemplateMapper"]
