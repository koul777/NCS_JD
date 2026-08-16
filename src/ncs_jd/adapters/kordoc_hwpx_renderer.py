"""Kordoc HWPX renderer isolated behind a stdin/stdout JSON bridge."""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import subprocess
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ncs_jd.application.document_renderer import (
    DocumentRendererPort,
    DocumentRenderTimeoutError,
    DocumentRenderTooLargeError,
    HwpxRenderPreview,
    HwpxTemplate,
    HwpxValidationSummary,
    InvalidHwpxError,
    InvalidRendererResponseError,
    InvalidTemplateError,
    KordocRendererError,
    RenderedHwpx,
    RendererNodeUnavailableError,
    TemplateCapability,
    field_values_to_markdown,
    job_profile_to_markdown,
    job_profile_to_template_values,
)
from ncs_jd.application.template_mapping import (
    TemplateField,
    TemplateInspection,
)
from ncs_jd.domain.job_profile import JobProfile


DEFAULT_MAX_HWPX_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_TEMPLATE_BYTES = 25 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_RESPONSE_CHARS = 48 * 1024 * 1024
DEFAULT_MAX_SVG_CHARS = 16 * 1024 * 1024
_MAX_HWPX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_REQUIRED_HWPX_ENTRIES = {
    "mimetype",
    "META-INF/container.xml",
    "Contents/content.hpf",
    "Contents/header.xml",
    "Contents/section0.xml",
}
_HWPX_MIMETYPE = b"application/hwp+zip"
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class RendererBridgeProcessResult:
    returncode: int
    stdout: str
    stderr: str = ""


@runtime_checkable
class RendererBridgeRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str,
        timeout_seconds: float,
    ) -> RendererBridgeProcessResult: ...


class SubprocessRendererBridgeRunner:
    """Default runner. Bridge request/response and stderr are never logged."""

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str,
        timeout_seconds: float,
    ) -> RendererBridgeProcessResult:
        completed = subprocess.run(
            tuple(command),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
        )
        return RendererBridgeProcessResult(completed.returncode, completed.stdout, completed.stderr)


class KordocHwpxRenderer(DocumentRendererPort):
    """Generate validated HWPX bytes from a validated ``JobProfile``."""

    def __init__(
        self,
        *,
        bridge_path: str | Path | None = None,
        node_executable: str = "node",
        runner: RendererBridgeRunner | None = None,
        max_hwpx_bytes: int = DEFAULT_MAX_HWPX_BYTES,
        max_template_bytes: int = DEFAULT_MAX_TEMPLATE_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
        max_svg_chars: int = DEFAULT_MAX_SVG_CHARS,
    ) -> None:
        if max_hwpx_bytes < 1 or max_template_bytes < 1:
            raise ValueError("HWPX and template size limits must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_response_chars < 1 or max_svg_chars < 1:
            raise ValueError("response size limits must be positive")
        self._bridge_path = Path(bridge_path) if bridge_path else _default_bridge_path()
        self._node_executable = node_executable
        self._runner = runner or SubprocessRendererBridgeRunner()
        self._max_hwpx_bytes = max_hwpx_bytes
        self._max_template_bytes = max_template_bytes
        self._timeout_seconds = timeout_seconds
        self._max_response_chars = max_response_chars
        self._max_svg_chars = max_svg_chars

    def render(
        self,
        profile: JobProfile,
        *,
        filename: str | None = None,
        template: HwpxTemplate | None = None,
    ) -> RenderedHwpx:
        if not isinstance(profile, JobProfile):
            raise TypeError("profile must be a validated JobProfile")

        return self._generate(
            markdown=job_profile_to_markdown(profile),
            template=template,
            template_field_values=(
                None if template is None else self._template_field_values(profile, template)
            ),
            filename=filename
            or f"NCS_직무설명자료_{profile.job_description.job_title.text}.hwpx",
        )

    def render_fields(
        self,
        field_values: tuple[tuple[str, str], ...],
        *,
        job_title: str,
        filename: str | None = None,
        template: HwpxTemplate | None = None,
    ) -> RenderedHwpx:
        """Render a label/value document the agent loop already composed.

        An uploaded template's own ``field_values`` still win when present, so a
        caller can map the agent's labels onto an institution form without this
        adapter guessing the correspondence.
        """

        markdown = field_values_to_markdown(field_values, job_title=job_title)
        resolved = (
            None
            if template is None
            else dict(template.field_values)
            if template.field_values is not None
            else dict(field_values)
        )
        return self._generate(
            markdown=markdown,
            template=template,
            template_field_values=resolved,
            filename=filename or f"NCS_직무설명자료_{job_title}.hwpx",
        )

    def _generate(
        self,
        *,
        markdown: str,
        template: HwpxTemplate | None,
        template_field_values: dict[str, str] | None,
        filename: str,
    ) -> RenderedHwpx:
        request: dict[str, Any] = {
            "action": "generate",
            "markdown": markdown,
            "max_output_bytes": self._max_hwpx_bytes,
        }
        if template is not None:
            template_request = self._template_source_request(template)
            template_request["field_values"] = template_field_values or {}
            request["template"] = template_request

        payload = self._invoke(request, response_limit=self._max_response_chars)
        encoded = payload.get("content_base64")
        if not isinstance(encoded, str) or not encoded:
            raise InvalidRendererResponseError("Kordoc bridge omitted HWPX content")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidRendererResponseError("Kordoc bridge returned invalid base64") from exc
        if len(content) > self._max_hwpx_bytes:
            raise DocumentRenderTooLargeError(
                f"rendered HWPX exceeds the {self._max_hwpx_bytes}-byte output limit"
            )
        actual_entry_count = _validate_hwpx_container(content, InvalidHwpxError)

        validation = _normalize_validation(payload.get("validation"))
        if not validation.valid:
            raise InvalidHwpxError("Kordoc reported an invalid generated HWPX")
        if validation.entry_count != actual_entry_count:
            raise InvalidRendererResponseError("Kordoc validation entry count does not match the HWPX")
        capability = _normalize_template_capability(
            payload.get("template_capability"),
            requested=template is not None,
        )
        safe_filename = sanitize_hwpx_filename(filename)
        return RenderedHwpx(
            filename=safe_filename,
            content=content,
            validation=validation,
            template_capability=capability,
        )

    def render_preview(self, content: bytes) -> HwpxRenderPreview:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if len(content) > self._max_hwpx_bytes:
            raise DocumentRenderTooLargeError(
                f"HWPX exceeds the {self._max_hwpx_bytes}-byte preview limit"
            )
        _validate_hwpx_container(content, InvalidHwpxError)
        request = {
            "action": "render",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "max_input_bytes": self._max_hwpx_bytes,
            "max_svg_chars": self._max_svg_chars,
        }
        payload = self._invoke(request, response_limit=self._max_svg_chars + 100_000)
        svg = payload.get("svg")
        page_count = payload.get("page_count")
        if not isinstance(svg, str) or len(svg) > self._max_svg_chars:
            raise InvalidRendererResponseError("Kordoc bridge returned an invalid SVG preview")
        if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
            raise InvalidRendererResponseError("Kordoc bridge returned an invalid preview page count")
        warnings = _normalize_strings(payload.get("warnings"), "render warnings", limit=1_000)
        try:
            return HwpxRenderPreview(svg=svg, page_count=page_count, warnings=warnings)
        except ValueError as exc:
            raise InvalidRendererResponseError("Kordoc bridge returned malformed SVG") from exc

    def inspect_template(self, template: HwpxTemplate) -> TemplateInspection:
        """Return bounded form metadata without exposing the original document bytes."""

        request = {
            "action": "inspect_template",
            "template": self._template_source_request(template),
        }
        payload = self._invoke(request, response_limit=2 * 1024 * 1024)
        source_format = _safe_string(payload.get("format"), 20)
        confidence = payload.get("confidence")
        raw_fields = payload.get("fields")
        if not source_format or not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise InvalidRendererResponseError("Kordoc bridge returned invalid template metadata")
        if not isinstance(raw_fields, list) or len(raw_fields) > 100:
            raise InvalidRendererResponseError("Kordoc bridge returned invalid template fields")
        fields: list[TemplateField] = []
        for raw in raw_fields:
            if not isinstance(raw, Mapping):
                raise InvalidRendererResponseError("Kordoc bridge returned malformed template fields")
            label = _safe_string(raw.get("label"), 100)
            if not label:
                continue
            row = raw.get("row")
            col = raw.get("col")
            fields.append(
                TemplateField(
                    label=label,
                    value_preview=_safe_string(raw.get("value_preview"), 500),
                    empty=raw.get("empty") is True,
                    row=row if isinstance(row, int) and not isinstance(row, bool) and row >= 0 else None,
                    col=col if isinstance(col, int) and not isinstance(col, bool) and col >= 0 else None,
                )
            )
        try:
            return TemplateInspection(
                source_format=source_format,
                confidence=float(confidence),
                fields=tuple(fields),
            )
        except ValueError as exc:
            raise InvalidRendererResponseError("Kordoc bridge returned invalid template metadata") from exc

    def _template_field_values(
        self, profile: JobProfile, template: HwpxTemplate
    ) -> dict[str, str]:
        if template.field_values is not None:
            return dict(template.field_values)
        return job_profile_to_template_values(profile)

    def _template_source_request(self, template: HwpxTemplate) -> dict[str, Any]:
        if len(template.content) > self._max_template_bytes:
            raise DocumentRenderTooLargeError(
                f"template exceeds the {self._max_template_bytes}-byte input limit"
            )
        extension = Path(template.source_name).suffix.casefold()
        source_format = extension[1:] if extension else "unknown"
        if extension in {".pdf", ".hwp"}:
            _validate_rebuild_template(template.content, extension)
            return {
                "format": source_format,
                "content_base64": base64.b64encode(template.content).decode("ascii"),
                "max_bytes": self._max_template_bytes,
            }
        if extension != ".hwpx":
            return {"format": source_format}
        try:
            _validate_hwpx_container(template.content, InvalidTemplateError)
        except InvalidTemplateError:
            raise
        return {
            "format": "hwpx",
            "content_base64": base64.b64encode(template.content).decode("ascii"),
            "max_bytes": self._max_template_bytes,
        }
    def _invoke(self, request: Mapping[str, Any], *, response_limit: int) -> Mapping[str, Any]:
        request_json = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
        command = (self._node_executable, str(self._bridge_path))
        try:
            raw_result = self._runner.run(
                command,
                input_text=request_json,
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RendererNodeUnavailableError("Node.js executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise DocumentRenderTimeoutError(
                f"document rendering exceeded {self._timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise RendererNodeUnavailableError("Node.js bridge could not be started") from exc

        result = _coerce_process_result(raw_result)
        if len(result.stdout) > response_limit:
            raise InvalidRendererResponseError("Kordoc bridge response exceeded the safe size limit")
        response = _decode_response(result.stdout)
        if not response.get("ok"):
            error = response.get("error")
            code = _safe_string(error.get("code"), 100) if isinstance(error, Mapping) else "kordoc_error"
            message = (
                _safe_string(error.get("message"), 500)
                if isinstance(error, Mapping)
                else "Kordoc could not render the document"
            )
            raise KordocRendererError(
                message or "Kordoc could not render the document",
                code=code or "kordoc_error",
            )
        if result.returncode != 0:
            raise KordocRendererError(
                "Kordoc bridge exited unsuccessfully",
                code="bridge_process_error",
            )
        payload = response.get("result")
        if not isinstance(payload, Mapping):
            raise InvalidRendererResponseError("Kordoc bridge result must be an object")
        return payload


def _validate_rebuild_template(content: bytes, extension: str) -> None:
    if not content:
        raise InvalidTemplateError("template content must not be empty")
    if extension == ".pdf" and not content.startswith(b"%PDF-"):
        raise InvalidTemplateError("PDF template content does not match its extension")
    if extension == ".hwp" and not content.startswith(
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"HWP Document File V3.00")
    ):
        raise InvalidTemplateError("HWP template content does not match its extension")


def _default_bridge_path() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "kordoc_hwpx_bridge.mjs"


def sanitize_hwpx_filename(value: str) -> str:
    """Create a traversal-free Windows-safe filename and force ``.hwpx``."""

    if not isinstance(value, str):
        raise TypeError("filename must be a string")
    name = Path(value.replace("\\", "/")).name.strip().rstrip(". ")
    stem = Path(name).stem if Path(name).suffix else name
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip().rstrip(". ")
    stem = re.sub(r"\s+", "_", stem)
    stem = stem[:100].rstrip(". _") or "ncs_job_profile"
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return f"{stem}.hwpx"


def _validate_hwpx_container(
    content: bytes,
    error_type: type[InvalidHwpxError] | type[InvalidTemplateError],
) -> int:
    if not content.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        raise error_type("HWPX content is not a ZIP container")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.testzip() is not None:
                raise error_type("HWPX ZIP contains a corrupt entry")
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = {info.filename for info in infos}
            if sum(info.file_size for info in infos) > _MAX_HWPX_UNCOMPRESSED_BYTES:
                raise error_type("HWPX uncompressed content exceeds the safe size limit")
            if len(names) != len(infos):
                raise error_type("HWPX contains duplicate package entries")
            if not infos or infos[0].filename != "mimetype":
                raise error_type("HWPX mimetype must be the first ZIP entry")
            missing = _REQUIRED_HWPX_ENTRIES.difference(names)
            if missing:
                raise error_type("HWPX is missing required package entries")
            if archive.read("mimetype").strip() != _HWPX_MIMETYPE:
                raise error_type("HWPX has an invalid mimetype")
            return len(infos)
    except error_type:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise error_type("HWPX ZIP could not be inspected") from exc


def _coerce_process_result(value: Any) -> RendererBridgeProcessResult:
    if isinstance(value, RendererBridgeProcessResult):
        return value
    try:
        return RendererBridgeProcessResult(
            int(value.returncode),
            str(value.stdout or ""),
            str(value.stderr or ""),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidRendererResponseError("bridge runner returned an invalid process result") from exc


def _decode_response(stdout: str) -> Mapping[str, Any]:
    if not stdout.strip():
        raise InvalidRendererResponseError("Kordoc bridge returned no JSON")
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InvalidRendererResponseError("Kordoc bridge returned malformed JSON") from exc
    if not isinstance(response, Mapping) or not isinstance(response.get("ok"), bool):
        raise InvalidRendererResponseError("Kordoc bridge response has an invalid envelope")
    return response


def _normalize_validation(raw: Any) -> HwpxValidationSummary:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("ok"), bool):
        raise InvalidRendererResponseError("Kordoc validation summary is invalid")
    entry_count = _non_negative_int(raw.get("entry_count"))
    issue_count = _non_negative_int(raw.get("issue_count"))
    if entry_count is None or issue_count is None:
        raise InvalidRendererResponseError("Kordoc validation counts are invalid")
    if raw["ok"] and issue_count:
        raise InvalidRendererResponseError("Kordoc validation summary is inconsistent")
    return HwpxValidationSummary(bool(raw["ok"]), entry_count, issue_count)


def _normalize_template_capability(raw: Any, *, requested: bool) -> TemplateCapability:
    if raw is None and not requested:
        return TemplateCapability()
    if not isinstance(raw, Mapping):
        raise InvalidRendererResponseError("template capability report is missing or invalid")
    supported = raw.get("supported")
    used = raw.get("used")
    mode = raw.get("mode")
    if not isinstance(supported, bool) or not isinstance(used, bool):
        raise InvalidRendererResponseError("template capability booleans are invalid")
    if mode not in {"standard_generate", "template_rebuild", "hwpx-preserve"}:
        raise InvalidRendererResponseError("template capability mode is invalid")
    fallback = raw.get("fallback_reason")
    if fallback is not None and not isinstance(fallback, str):
        raise InvalidRendererResponseError("template fallback reason is invalid")
    try:
        return TemplateCapability(
            requested=requested,
            supported=supported,
            used=used,
            mode=mode,
            fallback_reason=_safe_string(fallback, 300) or None,
            matched_fields=_normalize_strings(raw.get("matched_fields"), "matched fields", limit=100),
            unmatched_fields=_normalize_strings(raw.get("unmatched_fields"), "unmatched fields", limit=100),
            ambiguous_fields=_normalize_strings(raw.get("ambiguous_fields"), "ambiguous fields", limit=100),
        )
    except ValueError as exc:
        raise InvalidRendererResponseError("template capability report is inconsistent") from exc


def _normalize_strings(raw: Any, label: str, *, limit: int) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > limit or any(not isinstance(item, str) for item in raw):
        raise InvalidRendererResponseError(f"{label} must be a bounded string array")
    return tuple(_safe_string(item, 500) for item in raw if _safe_string(item, 500))


def _safe_string(value: Any, max_length: int) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return str(value).replace("\x00", "").strip()[:max_length]


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


__all__ = [
    "DEFAULT_MAX_HWPX_BYTES",
    "DEFAULT_MAX_RESPONSE_CHARS",
    "DEFAULT_MAX_SVG_CHARS",
    "DEFAULT_MAX_TEMPLATE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "KordocHwpxRenderer",
    "RendererBridgeProcessResult",
    "RendererBridgeRunner",
    "SubprocessRendererBridgeRunner",
    "sanitize_hwpx_filename",
]
