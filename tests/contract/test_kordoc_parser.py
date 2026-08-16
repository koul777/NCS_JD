from __future__ import annotations

import base64
import io
import json
import subprocess
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from ncs_jd.adapters.kordoc_parser import BridgeProcessResult, KordocDocumentParser
from ncs_jd.application.document_parser import (
    DocumentFormatMismatchError,
    DocumentParseTimeoutError,
    DocumentParserPort,
    DocumentTooLargeError,
    KordocParserError,
    NodeUnavailableError,
    UnsupportedDocumentError,
)


PDF_BYTES = b"%PDF-1.7\nsmall fake body"


class FakeRunner:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []
        self.commands: list[tuple[str, ...]] = []

    def run(
        self,
        command: tuple[str, ...],
        *,
        input_text: str,
        timeout_seconds: float,
    ) -> BridgeProcessResult:
        del timeout_seconds
        self.commands.append(tuple(command))
        self.requests.append(json.loads(input_text))
        return BridgeProcessResult(0, json.dumps(self.response, ensure_ascii=False))


def _success_response() -> dict[str, object]:
    return {
        "ok": True,
        "result": {
            "format": "pdf",
            "markdown": "## 담당업무\n- 채용 계획 수립",
            "blocks": [
                {
                    "type": "heading",
                    "page_number": 2,
                    "text": "담당업무",
                    "markdown": "## 담당업무",
                    "table_rows": [],
                    "image_data": "must-not-cross-boundary",
                    "source_payload": {"private": True},
                },
                {
                    "type": "paragraph",
                    "page_number": 2,
                    "text": "채용 계획 수립",
                    "markdown": "채용 계획 수립",
                    "table_rows": [],
                },
            ],
            "metadata": {
                "title": "채용 공고",
                "author": "기관",
                "page_count": 2,
                "raw_properties": {"not": "exposed"},
            },
            "warnings": [{"code": "hidden_text", "message": "숨김 텍스트 제외", "block_index": 1}],
            "original_payload": "must-not-cross-boundary",
        },
    }


def _empty_success(document_format: str) -> dict[str, object]:
    return {
        "ok": True,
        "result": {
            "format": document_format,
            "markdown": "parsed",
            "blocks": [
                {
                    "type": "paragraph",
                    "page_number": 1,
                    "text": "parsed",
                    "markdown": "parsed",
                    "table_rows": [],
                }
            ],
            "metadata": {},
            "warnings": [],
        },
    }


def _zip_with(name: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, "<xml />")
        if name.startswith("word/"):
            archive.writestr("[Content_Types].xml", "<Types />")
    return output.getvalue()


def test_parser_uses_json_bridge_and_normalizes_safe_payload() -> None:
    runner = FakeRunner(_success_response())
    parser = KordocDocumentParser(
        bridge_path=Path("scripts/kordoc_bridge.mjs"),
        runner=runner,
    )

    result = parser.parse("기관 채용공고.pdf", PDF_BYTES)

    assert isinstance(parser, DocumentParserPort)
    assert runner.requests[0]["expected_format"] == "pdf"
    assert base64.b64decode(str(runner.requests[0]["content_base64"])) == PDF_BYTES
    assert result.markdown == "## 담당업무\n- 채용 계획 수립"
    assert [block.locator.block_id for block in result.blocks] == ["block-0001", "block-0002"]
    assert result.blocks[1].locator.page_number == 2
    assert result.metadata.title == "채용 공고"
    assert result.quality.status == "partial"
    assert result.quality.total_blocks == 2
    assert result.warnings[0].locator == result.blocks[1].locator
    serialized = json.dumps(asdict(result), ensure_ascii=False)
    assert "image_data" not in serialized
    assert "source_payload" not in serialized
    assert "original_payload" not in serialized


def test_same_input_is_deterministic() -> None:
    parser = KordocDocumentParser(runner=FakeRunner(_success_response()))

    first = parser.parse("notice.pdf", PDF_BYTES)
    second = parser.parse("notice.pdf", PDF_BYTES)

    assert first == second


@pytest.mark.parametrize(
    ("name", "content", "bridge_format"),
    [
        ("notice.pdf", PDF_BYTES, "pdf"),
        ("notice.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake", "hwp"),
        ("legacy.hwp", b"HWP Document File V3.00fake", "hwp3"),
        ("notice.hwpx", _zip_with("Contents/section0.xml"), "hwpx"),
        ("notice.docx", _zip_with("word/document.xml"), "docx"),
        ("notice.txt", "채용 공고".encode(), "txt"),
    ],
)
def test_allowed_formats_pass_magic_validation(
    name: str,
    content: bytes,
    bridge_format: str,
) -> None:
    runner = FakeRunner(_empty_success(bridge_format))

    result = KordocDocumentParser(runner=runner).parse(name, content)

    assert result.source_name == name
    assert len(runner.requests) == 1


@pytest.mark.parametrize("name", ["notice.exe", "notice.xlsx", "notice"])
def test_unsupported_extension_is_rejected_before_runner(name: str) -> None:
    runner = FakeRunner(_success_response())

    with pytest.raises(UnsupportedDocumentError, match="unsupported document extension"):
        KordocDocumentParser(runner=runner).parse(name, PDF_BYTES)

    assert runner.requests == []


def test_magic_and_extension_mismatch_is_rejected() -> None:
    runner = FakeRunner(_success_response())

    with pytest.raises(DocumentFormatMismatchError, match="extension indicates docx"):
        KordocDocumentParser(runner=runner).parse("forged.docx", PDF_BYTES)

    assert runner.requests == []


def test_size_limit_is_enforced_before_encoding_or_runner() -> None:
    runner = FakeRunner(_success_response())

    with pytest.raises(DocumentTooLargeError, match="upload limit"):
        KordocDocumentParser(runner=runner, max_document_bytes=4).parse("notice.pdf", PDF_BYTES)

    assert runner.requests == []


class MissingNodeRunner:
    def run(self, *args: object, **kwargs: object) -> BridgeProcessResult:
        raise FileNotFoundError("node")


class TimeoutRunner:
    def run(self, *args: object, **kwargs: object) -> BridgeProcessResult:
        raise subprocess.TimeoutExpired("node", 1)


def test_node_unavailable_is_structured() -> None:
    with pytest.raises(NodeUnavailableError) as caught:
        KordocDocumentParser(runner=MissingNodeRunner()).parse("notice.pdf", PDF_BYTES)

    assert caught.value.as_dict()["code"] == "node_unavailable"


def test_timeout_is_structured() -> None:
    with pytest.raises(DocumentParseTimeoutError) as caught:
        KordocDocumentParser(runner=TimeoutRunner(), timeout_seconds=1).parse("notice.pdf", PDF_BYTES)

    assert caught.value.as_dict()["code"] == "document_parse_timeout"


def test_kordoc_failure_is_mapped_without_stderr_or_payload() -> None:
    runner = FakeRunner(
        {
            "ok": False,
            "error": {"code": "encrypted", "message": "암호화 문서는 파싱할 수 없습니다."},
        }
    )

    with pytest.raises(KordocParserError) as caught:
        KordocDocumentParser(runner=runner).parse("notice.pdf", PDF_BYTES)

    assert caught.value.as_dict() == {
        "code": "encrypted",
        "message": "암호화 문서는 파싱할 수 없습니다.",
    }
