from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ncs_jd.adapters.ncs_mcp_source import HttpProbe, NcsMcpSourceAdapter
from ncs_jd.application.ncs_source import (
    NcsSourceContractError,
    NcsSourcePort,
    NcsSourceTimeoutError,
)


UNIT_CODE = "0202020103_23v4"
FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "ncs_mcp_contract_responses.json"
FIXED_NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


class RecordingTransport:
    def __init__(
        self,
        *,
        tool_responses: Mapping[str, list[object]] | None = None,
        probe_responses: Mapping[str, list[object]] | None = None,
    ) -> None:
        self.tool_responses = {name: list(values) for name, values in (tool_responses or {}).items()}
        self.probe_responses = {url: list(values) for url, values in (probe_responses or {}).items()}
        self.tool_calls: list[tuple[str, dict[str, object]]] = []
        self.probe_calls: list[str] = []

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        self.tool_calls.append((name, dict(arguments)))
        value = self.tool_responses[name].pop(0)
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    async def get_json(self, url: str) -> HttpProbe:
        self.probe_calls.append(url)
        value = self.probe_responses[url].pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, HttpProbe)
        return value


@pytest.fixture()
def responses() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _adapter(transport: RecordingTransport, **overrides: object) -> NcsMcpSourceAdapter:
    return NcsMcpSourceAdapter(
        transport=transport,
        timeout_seconds=float(overrides.pop("timeout_seconds", 1)),
        max_retries=int(overrides.pop("max_retries", 1)),
        clock=lambda: FIXED_NOW,
        **overrides,
    )


def test_module_import_and_adapter_do_not_require_mcp_sdk(responses: dict[str, object]) -> None:
    transport = RecordingTransport(tool_responses={"ncs_search": [responses["empty_search"]]})
    adapter = _adapter(transport)

    assert isinstance(adapter, NcsSourcePort)
    assert asyncio.run(adapter.search_scope_candidates("결과 없음")) == []


def test_search_calls_only_explicit_tool_and_maps_candidates_deterministically(
    responses: dict[str, object],
) -> None:
    transport = RecordingTransport(tool_responses={"ncs_search": [responses["search"]]})
    adapter = _adapter(transport)

    candidates = asyncio.run(adapter.search_scope_candidates("인사 담당자", limit=20))

    assert transport.tool_calls == [
        ("ncs_search", {"query": "인사 담당자", "scope": "all", "limit": 20})
    ]
    assert [item.unit_code for item in candidates] == [UNIT_CODE, "0202020104_23v4"]
    assert candidates[0].classification_path == "경영·회계·사무 > 총무·인사 > 인사·조직 > 인사"
    assert (
        candidates[0].major_code,
        candidates[0].middle_code,
        candidates[0].small_code,
        candidates[0].sub_code,
    ) == ("02", "02", "02", "01")
    assert candidates[0].duty_definition == "인적자원을 관리한다."
    assert candidates[0].unit_level == "5"
    assert candidates[0].unit_definition == "조직에 필요한 인력을 채용하는 능력"
    assert not hasattr(candidates[0], "source_payload")
    assert not hasattr(candidates[0], "human_reviewed")


def test_empty_search_is_a_stable_empty_result(responses: dict[str, object]) -> None:
    transport = RecordingTransport(
        tool_responses={"ncs_search": [responses["empty_search"], responses["empty_search"]]}
    )
    adapter = _adapter(transport)

    first = asyncio.run(adapter.search_scope_candidates("없는 직무"))
    second = asyncio.run(adapter.search_scope_candidates("없는 직무"))

    assert first == second == []


def test_unit_detail_maps_raw_evidence_audit_and_drops_status_fields(
    responses: dict[str, object],
) -> None:
    transport = RecordingTransport(tool_responses={"ncs_unit_detail": [responses["unit_detail"]]})
    adapter = _adapter(transport)

    bundle = asyncio.run(adapter.load_unit_evidence(UNIT_CODE))

    assert transport.tool_calls == [
        (
            "ncs_unit_detail",
            {"unit_code": UNIT_CODE, "include": ["elements", "criteria", "ksa"]},
        )
    ]
    assert bundle.unit.unit_code == UNIT_CODE
    assert bundle.unit.classification_path == "경영·회계·사무 > 총무·인사 > 인사·조직 > 인사"
    assert bundle.unit.duty_definition == "인적자원을 관리한다."
    assert [item.element_id for item in bundle.elements] == ["31", "32"]
    assert bundle.elements[0].criteria[0].criteria_text_raw == "채용계획을 수립할 수 있다."
    assert bundle.elements[0].ksa[0].ksa_text_raw == "채용 절차에 관한 지식"
    assert bundle.elements[0].ksa[0].ksa_type == "knowledge"
    assert bundle.elements[1].ksa[0].ksa_type == "skill"
    assert bundle.source_audit.retrieved_at == datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    assert bundle.source_audit.data_sources == (
        "competency_units",
        "ksa_items",
        "performance_criteria",
    )
    assert bundle.warnings == ()
    assert not hasattr(bundle.unit, "review_status")
    assert not hasattr(bundle.elements[0], "accepted")
    assert not hasattr(bundle.elements[0].ksa[0], "reviewed")
    assert not hasattr(bundle.source_audit, "source_payload")


def test_partial_evidence_is_preserved_with_structured_warnings(
    responses: dict[str, object],
) -> None:
    transport = RecordingTransport(
        tool_responses={"ncs_unit_detail": [responses["partial_unit_detail"]]}
    )
    bundle = asyncio.run(_adapter(transport).load_unit_evidence(UNIT_CODE))

    assert [item.element_id for item in bundle.elements] == ["31", "32"]
    assert bundle.elements[0].criteria == ()
    assert bundle.elements[0].ksa == ()
    assert bundle.source_audit.retrieved_at == FIXED_NOW
    assert {warning.code for warning in bundle.warnings} == {
        "partial_unit_evidence",
        "unsupported_ksa_type",
    }
    assert {warning.location for warning in bundle.warnings} >= {
        "elements[0].criteria",
        "elements[0].ksa[0]",
        "elements[1].criteria",
        "elements[1].ksa",
    }


@pytest.mark.parametrize(
    ("response_name", "mutate", "method"),
    [
        (
            "search",
            lambda payload: payload["results"][0].pop("id"),
            lambda adapter: adapter.search_scope_candidates("인사"),
        ),
        (
            "unit_detail",
            lambda payload: payload["elements"][0]["performance_criteria"][0].pop("criteria_id"),
            lambda adapter: adapter.load_unit_evidence(UNIT_CODE),
        ),
        (
            "unit_detail",
            lambda payload: payload["elements"][0]["ksa"][0].pop("ksa_id"),
            lambda adapter: adapter.load_unit_evidence(UNIT_CODE),
        ),
    ],
)
def test_required_source_identifiers_raise_contract_error(
    responses: dict[str, object],
    response_name: str,
    mutate: object,
    method: object,
) -> None:
    payload = copy.deepcopy(responses[response_name])
    mutate(payload)  # type: ignore[operator]
    tool_name = "ncs_search" if response_name == "search" else "ncs_unit_detail"
    transport = RecordingTransport(tool_responses={tool_name: [payload]})

    with pytest.raises(NcsSourceContractError, match="required identifier") as captured:
        asyncio.run(method(_adapter(transport)))  # type: ignore[operator]

    assert captured.value.code == "ncs_source_contract_error"
    assert captured.value.operation == tool_name


def test_timeout_is_retried_at_most_once_and_raised_structurally(
    responses: dict[str, object],
) -> None:
    transport = RecordingTransport(
        tool_responses={"ncs_search": [TimeoutError(), TimeoutError(), responses["search"]]}
    )
    adapter = _adapter(transport, timeout_seconds=0.1)

    with pytest.raises(NcsSourceTimeoutError) as captured:
        asyncio.run(adapter.search_scope_candidates("인사"))

    assert captured.value.code == "ncs_source_timeout"
    assert captured.value.retryable is True
    assert len(transport.tool_calls) == 2


def test_optional_references_use_only_allowed_analysis_modes_and_reference_grade(
    responses: dict[str, object],
) -> None:
    transport = RecordingTransport(
        tool_responses={
            "ncs_analysis": [responses["qualification"], responses["job_base"]],
        }
    )
    references = asyncio.run(
        _adapter(transport).load_optional_references(
            UNIT_CODE,
            ("job_base", "qualification"),
        )
    )

    assert transport.tool_calls == [
        ("ncs_analysis", {"mode": "qualification", "unit_code": UNIT_CODE, "limit": 20}),
        ("ncs_analysis", {"mode": "job_base", "unit_code": UNIT_CODE, "limit": 20}),
    ]
    assert [(item.kind, item.reference_id, item.text_raw) for item in references] == [
        ("qualification", "42", "직업상담사"),
        ("job_base", "52", "의사소통능력 > 경청능력"),
    ]
    assert all(item.evidence_grade == "reference" for item in references)
    assert all(not hasattr(item, "review_status") for item in references)
    assert {name for name, _arguments in transport.tool_calls} == {"ncs_analysis"}


def test_health_and_readiness_urls_are_checked_with_one_timeout_retry() -> None:
    health_url = "http://127.0.0.1:8766/health"
    ready_url = "http://127.0.0.1:8766/ready"
    transport = RecordingTransport(
        probe_responses={
            health_url: [TimeoutError(), HttpProbe(200, {"status": "ok", "secret": "ignored"})],
            ready_url: [HttpProbe(200, {"status": "ready", "source_payload": {"ignored": True}})],
        }
    )
    adapter = _adapter(transport, health_url=health_url, ready_url=ready_url, timeout_seconds=0.1)

    status = asyncio.run(adapter.check_readiness())

    assert status.healthy is True
    assert status.ready is True
    assert status.message == "ready"
    assert status.error_code is None
    assert transport.probe_calls == [health_url, health_url, ready_url]


def test_degraded_readiness_is_returned_without_leaking_response_payload() -> None:
    health_url = "http://127.0.0.1:8766/health"
    ready_url = "http://127.0.0.1:8766/ready"
    transport = RecordingTransport(
        probe_responses={
            health_url: [HttpProbe(200, {"status": "ok"})],
            ready_url: [HttpProbe(503, {"status": "not_ready", "source_payload": "private"})],
        }
    )

    status = asyncio.run(_adapter(transport, health_url=health_url, ready_url=ready_url).check_readiness())

    assert status.healthy is True
    assert status.ready is False
    assert status.error_code == "readiness_check_failed"
    assert "private" not in status.message
