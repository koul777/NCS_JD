"""Application contract for safely parsing user-supplied source documents.

The DTOs in this module deliberately contain only text and small scalar metadata.
Original bytes, embedded images, and parser-specific payloads never cross the
application boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable


DocumentFormat = Literal["pdf", "hwp", "hwpx", "docx", "txt"]
BlockType = Literal["heading", "paragraph", "table", "list", "image", "separator", "unknown"]


class DocumentParserError(RuntimeError):
    """Base class for safe, structured document parser failures."""

    default_code = "document_parse_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class UnsupportedDocumentError(DocumentParserError):
    default_code = "unsupported_document"


class DocumentTooLargeError(DocumentParserError):
    default_code = "document_too_large"


class DocumentFormatMismatchError(DocumentParserError):
    default_code = "document_format_mismatch"


class NodeUnavailableError(DocumentParserError):
    default_code = "node_unavailable"


class DocumentParseTimeoutError(DocumentParserError):
    default_code = "document_parse_timeout"


class KordocParserError(DocumentParserError):
    default_code = "kordoc_error"


class InvalidParserResponseError(DocumentParserError):
    default_code = "invalid_parser_response"


@dataclass(frozen=True, slots=True)
class SourceLocator:
    """Stable source location suitable for a human-review UI."""

    block_id: str
    block_index: int
    page_number: int | None = None

    def __post_init__(self) -> None:
        if not self.block_id.strip():
            raise ValueError("block_id must not be empty")
        if self.block_index < 0:
            raise ValueError("block_index must be non-negative")
        if self.page_number is not None and self.page_number < 1:
            raise ValueError("page_number must be positive")


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """Sanitized Kordoc block with no binary or parser-internal payload."""

    locator: SourceLocator
    block_type: BlockType
    text: str = ""
    markdown: str = ""
    table_rows: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.block_type != "table" and self.table_rows:
            raise ValueError("table_rows are only allowed for table blocks")


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Small metadata allow-list normalized from Kordoc."""

    title: str | None = None
    author: str | None = None
    subject: str | None = None
    created_at: str | None = None
    modified_at: str | None = None
    page_count: int | None = None


@dataclass(frozen=True, slots=True)
class ParseWarning:
    code: str
    message: str
    locator: SourceLocator | None = None

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("warning code and message must not be empty")


@dataclass(frozen=True, slots=True)
class ParseQualitySummary:
    total_blocks: int
    text_blocks: int
    table_blocks: int
    page_count: int | None
    character_count: int
    warning_count: int
    status: Literal["good", "partial", "empty"]

    def __post_init__(self) -> None:
        counts = (
            self.total_blocks,
            self.text_blocks,
            self.table_blocks,
            self.character_count,
            self.warning_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("quality counts must not be negative")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source_name: str
    document_format: DocumentFormat
    markdown: str
    blocks: tuple[ParsedBlock, ...]
    metadata: DocumentMetadata = field(default_factory=DocumentMetadata)
    warnings: tuple[ParseWarning, ...] = ()
    quality: ParseQualitySummary = field(
        default_factory=lambda: ParseQualitySummary(0, 0, 0, None, 0, 0, "empty")
    )

    def __post_init__(self) -> None:
        if not self.source_name.strip():
            raise ValueError("source_name must not be empty")
        expected_ids = [f"block-{index + 1:04d}" for index in range(len(self.blocks))]
        actual_ids = [block.locator.block_id for block in self.blocks]
        if actual_ids != expected_ids:
            raise ValueError("blocks must use deterministic block ids in source order")
        if [block.locator.block_index for block in self.blocks] != list(range(len(self.blocks))):
            raise ValueError("block indexes must be contiguous and in source order")


@runtime_checkable
class DocumentParserPort(Protocol):
    """Technology-neutral document parsing boundary."""

    def parse(self, source_name: str | Path, content: bytes) -> ParsedDocument: ...


__all__ = [
    "BlockType",
    "DocumentFormat",
    "DocumentFormatMismatchError",
    "DocumentMetadata",
    "DocumentParseTimeoutError",
    "DocumentParserError",
    "DocumentParserPort",
    "DocumentTooLargeError",
    "InvalidParserResponseError",
    "KordocParserError",
    "NodeUnavailableError",
    "ParseQualitySummary",
    "ParseWarning",
    "ParsedBlock",
    "ParsedDocument",
    "SourceLocator",
    "UnsupportedDocumentError",
]
