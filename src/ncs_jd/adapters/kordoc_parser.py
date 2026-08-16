"""Kordoc adapter using a narrow JSON stdin/stdout Node bridge."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ncs_jd.application.document_parser import (
    BlockType,
    DocumentFormat,
    DocumentFormatMismatchError,
    DocumentMetadata,
    DocumentParseTimeoutError,
    DocumentParserPort,
    DocumentTooLargeError,
    InvalidParserResponseError,
    KordocParserError,
    NodeUnavailableError,
    ParseQualitySummary,
    ParseWarning,
    ParsedBlock,
    ParsedDocument,
    SourceLocator,
    UnsupportedDocumentError,
)


DEFAULT_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_RESPONSE_CHARS = 32 * 1024 * 1024
_ALLOWED_EXTENSIONS: dict[str, DocumentFormat] = {
    ".pdf": "pdf",
    ".hwp": "hwp",
    ".hwpx": "hwpx",
    ".docx": "docx",
    ".txt": "txt",
}
_BLOCK_TYPES: set[str] = {"heading", "paragraph", "table", "list", "image", "separator"}


@dataclass(frozen=True, slots=True)
class BridgeProcessResult:
    returncode: int
    stdout: str
    stderr: str = ""


@runtime_checkable
class BridgeRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str,
        timeout_seconds: float,
    ) -> BridgeProcessResult: ...


class SubprocessBridgeRunner:
    """Default runner. It never prints or logs bridge input/output."""

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str,
        timeout_seconds: float,
    ) -> BridgeProcessResult:
        completed = subprocess.run(
            tuple(command),
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
        )
        return BridgeProcessResult(completed.returncode, completed.stdout, completed.stderr)


class KordocDocumentParser(DocumentParserPort):
    """Validate an upload, invoke Kordoc, and expose a sanitized DTO."""

    def __init__(
        self,
        *,
        bridge_path: str | Path | None = None,
        node_executable: str = "node",
        runner: BridgeRunner | None = None,
        max_document_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
    ) -> None:
        if max_document_bytes < 1:
            raise ValueError("max_document_bytes must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._bridge_path = Path(bridge_path) if bridge_path else _default_bridge_path()
        self._node_executable = node_executable
        self._runner = runner or SubprocessBridgeRunner()
        self._max_document_bytes = max_document_bytes
        self._timeout_seconds = timeout_seconds
        self._max_response_chars = max_response_chars

    def parse(self, source_name: str | Path, content: bytes) -> ParsedDocument:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        safe_name = Path(source_name).name.strip()
        if not safe_name:
            raise UnsupportedDocumentError("document name must not be empty")
        document_format = _format_from_name(safe_name)
        if len(content) > self._max_document_bytes:
            raise DocumentTooLargeError(
                f"document exceeds the {self._max_document_bytes}-byte upload limit"
            )
        if not content:
            raise KordocParserError("document is empty", code="empty_document")
        detected_format = _detect_magic_format(content, document_format)
        if detected_format != document_format:
            raise DocumentFormatMismatchError(
                f"file extension indicates {document_format}, but content indicates {detected_format or 'unknown'}"
            )

        request = {
            "operation": "parse",
            "source_name": safe_name,
            "expected_format": document_format,
            "max_bytes": self._max_document_bytes,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        request_json = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
        command = (self._node_executable, str(self._bridge_path))
        try:
            raw_result = self._runner.run(
                command,
                input_text=request_json,
                timeout_seconds=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise NodeUnavailableError("Node.js executable is not available") from exc
        except subprocess.TimeoutExpired as exc:
            raise DocumentParseTimeoutError(
                f"document parsing exceeded {self._timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise NodeUnavailableError("Node.js bridge could not be started") from exc

        result = _coerce_process_result(raw_result)
        if len(result.stdout) > self._max_response_chars:
            raise InvalidParserResponseError("Kordoc bridge response exceeded the safe size limit")
        response = _decode_response(result.stdout)
        if not response.get("ok"):
            error = response.get("error")
            code = _safe_string(error.get("code"), 100) if isinstance(error, Mapping) else "kordoc_error"
            message = (
                _safe_string(error.get("message"), 500)
                if isinstance(error, Mapping)
                else "Kordoc could not parse the document"
            )
            raise KordocParserError(message or "Kordoc could not parse the document", code=code or "kordoc_error")
        if result.returncode != 0:
            raise KordocParserError("Kordoc bridge exited unsuccessfully", code="bridge_process_error")

        payload = response.get("result")
        if not isinstance(payload, Mapping):
            raise InvalidParserResponseError("Kordoc bridge result must be an object")
        returned_format = _safe_string(payload.get("format"), 20).lower()
        compatible_formats = {
            document_format,
            "hwp3" if document_format == "hwp" else document_format,
        }
        if returned_format and returned_format not in compatible_formats:
            raise DocumentFormatMismatchError(
                f"Kordoc detected {returned_format}, expected {document_format}"
            )
        return _normalize_document(safe_name, document_format, payload)


def _default_bridge_path() -> Path:
    return Path(__file__).resolve().parents[3] / "scripts" / "kordoc_bridge.mjs"


def _format_from_name(source_name: str) -> DocumentFormat:
    extension = Path(source_name).suffix.casefold()
    try:
        return _ALLOWED_EXTENSIONS[extension]
    except KeyError as exc:
        allowed = ", ".join(sorted(_ALLOWED_EXTENSIONS))
        raise UnsupportedDocumentError(f"unsupported document extension; allowed: {allowed}") from exc


def _detect_magic_format(content: bytes, expected: DocumentFormat) -> DocumentFormat | None:
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"HWP Document File V3.00") or content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "hwp"
    if content.startswith(b"PK\x03\x04") or content.startswith(b"PK\x05\x06"):
        return _detect_zip_format(content)
    if expected == "txt" and _looks_like_text(content):
        return "txt"
    return None


def _detect_zip_format(content: bytes) -> DocumentFormat | None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = {name.replace("\\", "/") for name in archive.namelist()}
            lower_names = {name.casefold() for name in names}
            if any(name.startswith("word/") for name in lower_names) and "[content_types].xml" in lower_names:
                return "docx"
            if (
                "contents/content.hpf" in lower_names
                or any(name.startswith("contents/section") and name.endswith(".xml") for name in lower_names)
            ):
                return "hwpx"
            if "mimetype" in names:
                try:
                    mime = archive.read("mimetype").decode("ascii", errors="ignore").casefold()
                except (KeyError, RuntimeError, ValueError):
                    mime = ""
                if "hwp" in mime:
                    return "hwpx"
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return None
    return None


def _looks_like_text(content: bytes) -> bool:
    if b"\x00" in content[:4096]:
        return False
    for encoding in ("utf-8-sig", "cp949"):
        try:
            content.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _coerce_process_result(value: Any) -> BridgeProcessResult:
    if isinstance(value, BridgeProcessResult):
        return value
    try:
        return BridgeProcessResult(
            int(value.returncode),
            str(value.stdout or ""),
            str(value.stderr or ""),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidParserResponseError("bridge runner returned an invalid process result") from exc


def _decode_response(stdout: str) -> Mapping[str, Any]:
    if not stdout.strip():
        raise InvalidParserResponseError("Kordoc bridge returned no JSON")
    try:
        response = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InvalidParserResponseError("Kordoc bridge returned malformed JSON") from exc
    if not isinstance(response, Mapping) or not isinstance(response.get("ok"), bool):
        raise InvalidParserResponseError("Kordoc bridge response has an invalid envelope")
    return response


def _normalize_document(
    source_name: str,
    document_format: DocumentFormat,
    payload: Mapping[str, Any],
) -> ParsedDocument:
    markdown = _safe_string(payload.get("markdown"), DEFAULT_MAX_RESPONSE_CHARS)
    raw_blocks = payload.get("blocks")
    if raw_blocks is None:
        raw_blocks = []
    if not isinstance(raw_blocks, list):
        raise InvalidParserResponseError("Kordoc blocks must be an array")
    blocks = tuple(_normalize_block(index, block) for index, block in enumerate(raw_blocks))
    metadata = _normalize_metadata(payload.get("metadata"))
    warnings = _normalize_warnings(payload.get("warnings"), blocks)
    page_numbers = [block.locator.page_number for block in blocks if block.locator.page_number]
    page_count = metadata.page_count or (max(page_numbers) if page_numbers else None)
    text_blocks = sum(bool(block.text.strip() or block.markdown.strip()) for block in blocks)
    table_blocks = sum(block.block_type == "table" for block in blocks)
    character_count = len(markdown.strip())
    if character_count == 0 and text_blocks == 0:
        status = "empty"
    elif warnings:
        status = "partial"
    else:
        status = "good"
    quality = ParseQualitySummary(
        total_blocks=len(blocks),
        text_blocks=text_blocks,
        table_blocks=table_blocks,
        page_count=page_count,
        character_count=character_count,
        warning_count=len(warnings),
        status=status,
    )
    return ParsedDocument(
        source_name=source_name,
        document_format=document_format,
        markdown=markdown,
        blocks=blocks,
        metadata=metadata,
        warnings=warnings,
        quality=quality,
    )


def _normalize_block(index: int, raw: Any) -> ParsedBlock:
    if not isinstance(raw, Mapping):
        raise InvalidParserResponseError("each Kordoc block must be an object")
    raw_type = _safe_string(raw.get("type"), 30).casefold()
    block_type: BlockType = raw_type if raw_type in _BLOCK_TYPES else "unknown"  # type: ignore[assignment]
    page_number = _positive_int(raw.get("page_number"))
    locator = SourceLocator(f"block-{index + 1:04d}", index, page_number)
    raw_rows = raw.get("table_rows", [])
    if not isinstance(raw_rows, list):
        raise InvalidParserResponseError("table_rows must be an array")
    table_rows: list[tuple[str, ...]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, list):
            raise InvalidParserResponseError("each table row must be an array")
        table_rows.append(tuple(_safe_string(cell, 20_000) for cell in raw_row))
    if table_rows and block_type != "table":
        block_type = "table"
    return ParsedBlock(
        locator=locator,
        block_type=block_type,
        text=_safe_string(raw.get("text"), 1_000_000),
        markdown=_safe_string(raw.get("markdown"), 1_000_000),
        table_rows=tuple(table_rows),
    )


def _normalize_metadata(raw: Any) -> DocumentMetadata:
    if not isinstance(raw, Mapping):
        return DocumentMetadata()
    return DocumentMetadata(
        title=_optional_string(raw.get("title"), 500),
        author=_optional_string(raw.get("author"), 500),
        subject=_optional_string(raw.get("subject"), 1_000),
        created_at=_optional_string(raw.get("created_at"), 100),
        modified_at=_optional_string(raw.get("modified_at"), 100),
        page_count=_positive_int(raw.get("page_count")),
    )


def _normalize_warnings(raw: Any, blocks: tuple[ParsedBlock, ...]) -> tuple[ParseWarning, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InvalidParserResponseError("warnings must be an array")
    warnings: list[ParseWarning] = []
    for warning in raw:
        if isinstance(warning, str):
            code, message, locator = "kordoc_warning", _safe_string(warning, 500), None
        elif isinstance(warning, Mapping):
            code = _safe_string(warning.get("code"), 100) or "kordoc_warning"
            message = _safe_string(warning.get("message"), 500) or "Kordoc reported a parse warning"
            raw_index = warning.get("block_index")
            locator = blocks[raw_index].locator if isinstance(raw_index, int) and 0 <= raw_index < len(blocks) else None
        else:
            continue
        warnings.append(ParseWarning(code=code, message=message, locator=locator))
    return tuple(warnings)


def _safe_string(value: Any, max_length: int) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    text = str(value).replace("\x00", "").strip()
    return text[:max_length]


def _optional_string(value: Any, max_length: int) -> str | None:
    text = _safe_string(value, max_length)
    return text or None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


__all__ = [
    "BridgeProcessResult",
    "BridgeRunner",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "KordocDocumentParser",
    "SubprocessBridgeRunner",
]
