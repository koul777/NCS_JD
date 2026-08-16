"""Safe, optional mapping of generated fields onto uploaded document forms."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ncs_jd.application.document_renderer import HwpxTemplate
from ncs_jd.domain.job_profile import JobProfile


class TemplateMappingError(RuntimeError):
    """Sanitized failure from template inspection or an optional mapper."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TemplateField:
    label: str
    value_preview: str = ""
    empty: bool = False
    row: int | None = None
    col: int | None = None

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("template field label must not be empty")


@dataclass(frozen=True, slots=True)
class TemplateInspection:
    source_format: str
    confidence: float
    fields: tuple[TemplateField, ...]

    def __post_init__(self) -> None:
        if not self.source_format.strip():
            raise ValueError("template source format must not be empty")
        if not 0 <= self.confidence <= 1:
            raise ValueError("template inspection confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class TemplateMappingResult:
    provider: str
    model: str
    field_values: tuple[tuple[str, str], ...]
    mapped_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("template mapping provider and model are required")
        labels = [label for label, _ in self.field_values]
        if any(not label.strip() for label in labels) or len(labels) != len(set(labels)):
            raise ValueError("template mapping field labels must be unique and non-empty")


@runtime_checkable
class TemplateInspectorPort(Protocol):
    def inspect_template(self, template: HwpxTemplate) -> TemplateInspection: ...


@runtime_checkable
class TemplateMappingPort(Protocol):
    """Choose which of a form's own labels each generated value belongs in.

    Mapping authenticates through an official CLI's subscription login, so the
    port takes no credential: nothing here should ever accept a secret from a
    caller, and no implementation may ask a person for one.
    """

    def map_fields(
        self,
        profile: JobProfile,
        template: HwpxTemplate,
    ) -> TemplateMappingResult: ...


def template_mapping_json_schema(
    source_keys: Sequence[str],
    target_labels: Sequence[str],
) -> dict[str, object]:
    """Pin both sides of a mapping to labels the caller already observed."""

    keys = list(dict.fromkeys(source_keys))
    labels = list(dict.fromkeys(target_labels))
    if not keys or not labels:
        raise ValueError("source keys and target labels are required")
    return {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "maxItems": min(len(labels), len(keys)),
                "items": {
                    "type": "object",
                    "properties": {
                        "template_label": {"type": "string", "enum": labels},
                        "source_key": {"type": "string", "enum": keys},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["template_label", "source_key", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["mappings"],
        "additionalProperties": False,
    }


def template_mapping_user_payload(
    source_keys: Sequence[str],
    fields: Sequence[TemplateField],
) -> dict[str, object]:
    """Describe form cells without sending generated JobProfile values."""

    return {
        "source_fields": list(source_keys),
        "template_fields": [
            {
                "label": field.label,
                "existing_value_preview": field.value_preview[:300],
                "empty": field.empty,
            }
            for field in fields
        ],
    }


def apply_high_confidence_mappings(
    source_values: Mapping[str, str],
    target_labels: Sequence[str],
    raw_mappings: object,
    *,
    invalid_code: str,
    empty_code: str,
    ambiguous_code: str,
) -> dict[str, str]:
    """Keep canonical values and add only high-confidence form-label aliases."""

    if not isinstance(raw_mappings, list):
        raise TemplateMappingError(invalid_code, "양식 필드 매핑 응답을 검증할 수 없습니다.")
    allowed_targets = set(target_labels)
    allowed_sources = set(source_values)
    exact_values: dict[str, str] = {}
    for item in raw_mappings:
        if not isinstance(item, Mapping) or item.get("confidence") != "high":
            continue
        target = item.get("template_label")
        source = item.get("source_key")
        if target not in allowed_targets or source not in allowed_sources:
            raise TemplateMappingError(
                invalid_code,
                "허용되지 않은 템플릿 필드 매핑이 반환되었습니다.",
            )
        if not isinstance(target, str) or not isinstance(source, str):
            raise TemplateMappingError(invalid_code, "양식 필드 매핑 응답을 검증할 수 없습니다.")
        if target in exact_values:
            raise TemplateMappingError(
                ambiguous_code,
                "같은 템플릿 필드가 중복 매핑되었습니다.",
            )
        exact_values[target] = source_values[source]
    if not exact_values:
        raise TemplateMappingError(
            empty_code,
            "신뢰도 높은 템플릿 필드 매핑이 반환되지 않았습니다.",
        )
    return exact_values


__all__ = [
    "TemplateField",
    "TemplateInspection",
    "TemplateInspectorPort",
    "TemplateMappingError",
    "TemplateMappingPort",
    "TemplateMappingResult",
    "apply_high_confidence_mappings",
    "template_mapping_json_schema",
    "template_mapping_user_payload",
]
