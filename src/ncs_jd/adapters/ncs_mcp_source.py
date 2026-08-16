"""Read-only streamable HTTP adapter for the public NCS MCP surface."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from ncs_jd.application.ncs_source import (
    EvidenceCriterion,
    EvidenceElement,
    EvidenceKsa,
    EvidenceUnit,
    KsaKind,
    NcsSourceContractError,
    NcsSourceError,
    NcsSourcePort,
    NcsSourceTimeoutError,
    NcsSourceToolError,
    NcsSourceUnavailableError,
    NcsSourceWarning,
    OptionalReference,
    OptionalReferenceKind,
    ReadinessStatus,
    ScopeCandidate,
    SourceAudit,
    UnitEvidenceBundle,
    UnitEvidenceNotFoundError,
)


DEFAULT_MCP_URL = "http://127.0.0.1:8766/mcp"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8766/health"
DEFAULT_READY_URL = "http://127.0.0.1:8766/ready"
DEFAULT_TIMEOUT_SECONDS = 30.0
_DETAIL_INCLUDE = ("elements", "criteria", "ksa")
_OPTIONAL_KIND_ORDER = {"career_path": 0, "qualification": 1, "job_base": 2}


@dataclass(frozen=True, slots=True)
class HttpProbe:
    """Minimal HTTP result used for injectable health/readiness checks."""

    status_code: int
    payload: Mapping[str, object] | None


@runtime_checkable
class NcsMcpTransport(Protocol):
    """Small transport seam that keeps MCP SDK types out of the application."""

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object: ...

    async def get_json(self, url: str) -> HttpProbe: ...


class SdkStreamableHttpTransport:
    """Python MCP SDK transport, imported lazily so this module stays optional."""

    def __init__(self, mcp_url: str, *, request_timeout_seconds: float) -> None:
        self._mcp_url = mcp_url
        self._request_timeout_seconds = request_timeout_seconds

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> object:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError("Python MCP SDK is not installed") from exc

        async with streamablehttp_client(
            self._mcp_url,
            timeout=self._request_timeout_seconds,
        ) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, dict(arguments))
        return _decode_sdk_result(result)

    async def get_json(self, url: str) -> HttpProbe:
        return await asyncio.to_thread(self._get_json_sync, url)

    def _get_json_sync(self, url: str) -> HttpProbe:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self._request_timeout_seconds) as response:
                body = response.read()
                status_code = int(response.status)
        except HTTPError as exc:
            body = exc.read()
            status_code = int(exc.code)
        payload: Mapping[str, object] | None = None
        if body:
            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, Mapping):
                payload = decoded
        return HttpProbe(status_code=status_code, payload=payload)


class NcsMcpSourceAdapter(NcsSourcePort):
    """Normalize the three allowed public NCS MCP tools into application DTOs."""

    def __init__(
        self,
        *,
        mcp_url: str | None = None,
        health_url: str | None = None,
        ready_url: str | None = None,
        timeout_seconds: float | None = None,
        max_retries: int = 1,
        transport: NcsMcpTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        configured_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("NCS_MCP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
        )
        if configured_timeout <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_retries not in {0, 1}:
            raise ValueError("max_retries must be 0 or 1")

        self.mcp_url = mcp_url or os.getenv("NCS_MCP_URL", DEFAULT_MCP_URL)
        self.health_url = health_url or os.getenv("NCS_MCP_HEALTH_URL", DEFAULT_HEALTH_URL)
        self.ready_url = ready_url or os.getenv("NCS_MCP_READY_URL", DEFAULT_READY_URL)
        self.timeout_seconds = configured_timeout
        self.max_retries = max_retries
        self._transport = transport or SdkStreamableHttpTransport(
            self.mcp_url,
            request_timeout_seconds=configured_timeout,
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    async def search_scope_candidates(self, query: str, limit: int = 20) -> list[ScopeCandidate]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        payload = await self._call_tool(
            "ncs_search",
            {"query": normalized_query, "scope": "all", "limit": limit},
        )
        if self._tool_failed(payload):
            if self._is_not_found(payload):
                return []
            self._raise_tool_error("ncs_search", payload)

        rows = self._required_sequence(payload, "results", operation="ncs_search")
        candidates: dict[str, ScopeCandidate] = {}
        for index, raw_row in enumerate(rows):
            row = self._required_mapping(raw_row, f"results[{index}]", "ncs_search")
            result_type = self._optional_text(row, "type")
            if result_type not in {"unit", "element", "criteria", "ksa"}:
                continue
            self._required_identifier(row, "id", f"results[{index}]", "ncs_search")
            path = self._required_mapping(row.get("path"), f"results[{index}].path", "ncs_search")

            if result_type == "unit":
                unit_code = self._required_identifier(row, "id", f"results[{index}]", "ncs_search")
                unit_name = self._required_text(row, ("text", "unit_name"), f"results[{index}]", "ncs_search")
            else:
                unit_code = self._required_identifier(
                    path,
                    "unit_code",
                    f"results[{index}].path",
                    "ncs_search",
                )
                unit_name = self._required_text(
                    path,
                    ("unit_name",),
                    f"results[{index}].path",
                    "ncs_search",
                )

            candidate = ScopeCandidate(
                classification_path=self._classification_path(path),
                duty_definition=self._optional_text(path, "duty_definition"),
                unit_code=unit_code,
                unit_name=unit_name,
                unit_level=self._first_optional_text(row, path, keys=("unit_level", "level")),
                unit_definition=self._first_optional_text(
                    row,
                    path,
                    keys=("unit_definition", "api_definition", "definition"),
                ),
                major_code=self._identifier(path.get("major_code")),
                major_name=self._first_optional_text(path, keys=("major", "major_name")),
                middle_code=self._identifier(path.get("middle_code")),
                middle_name=self._first_optional_text(path, keys=("middle", "middle_name")),
                small_code=self._identifier(path.get("small_code")),
                small_name=self._first_optional_text(path, keys=("small", "small_name")),
                sub_code=self._identifier(path.get("sub_code")),
                sub_name=self._first_optional_text(path, keys=("sub", "sub_name")),
            )
            previous = candidates.get(unit_code)
            if previous is None or self._candidate_richness(candidate) > self._candidate_richness(previous):
                candidates[unit_code] = candidate

        return sorted(
            candidates.values(),
            key=lambda item: (item.classification_path, item.unit_code, item.unit_name),
        )[:limit]

    async def load_unit_evidence(self, unit_code: str) -> UnitEvidenceBundle:
        normalized_code = unit_code.strip()
        if not normalized_code:
            raise ValueError("unit_code must not be empty")

        payload = await self._call_tool(
            "ncs_unit_detail",
            {"unit_code": normalized_code, "include": list(_DETAIL_INCLUDE)},
        )
        if self._tool_failed(payload):
            if self._is_not_found(payload):
                raise UnitEvidenceNotFoundError(
                    f"unit evidence not found: {normalized_code}",
                    operation="ncs_unit_detail",
                )
            self._raise_tool_error("ncs_unit_detail", payload)

        unit_payload = self._required_mapping(
            self._field(payload, "unit"),
            "unit",
            "ncs_unit_detail",
        )
        returned_code = self._required_identifier(unit_payload, "unit_code", "unit", "ncs_unit_detail")
        if returned_code != normalized_code:
            raise NcsSourceContractError(
                "ncs_unit_detail returned a different unit_code",
                operation="ncs_unit_detail",
            )
        unit_name = self._required_text(
            unit_payload,
            ("unit_name",),
            "unit",
            "ncs_unit_detail",
        )
        classification = self._optional_mapping(unit_payload.get("classification"))
        unit = EvidenceUnit(
            unit_code=returned_code,
            unit_name=unit_name,
            unit_level=self._first_optional_text(unit_payload, keys=("unit_level", "level")),
            unit_definition=self._first_optional_text(
                unit_payload,
                keys=("unit_definition", "api_definition", "definition"),
            ),
            classification_path=self._classification_path(classification) or None,
            duty_definition=self._optional_text(classification, "duty_definition"),
        )

        warnings: list[NcsSourceWarning] = []
        raw_elements = self._field(payload, "elements")
        if raw_elements is None:
            raw_elements = []
            warnings.append(self._partial_warning("elements", "Element evidence is missing."))
        elif not self._is_sequence(raw_elements):
            raise NcsSourceContractError(
                "ncs_unit_detail.elements must be an array",
                operation="ncs_unit_detail",
            )
        elif not raw_elements:
            warnings.append(self._partial_warning("elements", "No element evidence was returned."))

        elements: list[EvidenceElement] = []
        for index, raw_element in enumerate(raw_elements):
            location = f"elements[{index}]"
            element_payload = self._required_mapping(raw_element, location, "ncs_unit_detail")
            element_id = self._required_identifier(
                element_payload,
                "element_id",
                location,
                "ncs_unit_detail",
            )
            element_name = self._first_optional_text(element_payload, keys=("element_name", "name"))
            if element_name is None:
                warnings.append(self._partial_warning(location, "Element name is missing; the element was omitted."))
                continue
            criteria = self._map_criteria(element_payload, location, warnings)
            ksa = self._map_ksa(element_payload, location, warnings)
            elements.append(
                EvidenceElement(
                    element_id=element_id,
                    element_name=element_name,
                    criteria=criteria,
                    ksa=ksa,
                )
            )

        audit_payload = self._optional_mapping(payload.get("audit"))
        retrieved_at = self._parse_timestamp(audit_payload.get("generated_at")) or self._normalized_now()
        raw_sources = audit_payload.get("data_sources")
        data_sources = (
            tuple(sorted({str(item).strip() for item in raw_sources if str(item).strip()}))
            if self._is_sequence(raw_sources)
            else ()
        )
        return UnitEvidenceBundle(
            unit=unit,
            elements=tuple(sorted(elements, key=lambda item: (item.element_id, item.element_name))),
            source_audit=SourceAudit(
                source_system="NCS_MCP",
                retrieved_at=retrieved_at,
                unit_code=normalized_code,
                data_sources=data_sources,
            ),
            warnings=tuple(sorted(set(warnings), key=lambda item: (item.location, item.code, item.message))),
        )

    async def load_optional_references(
        self,
        unit_code: str,
        kinds: tuple[OptionalReferenceKind, ...],
    ) -> list[OptionalReference]:
        normalized_code = unit_code.strip()
        if not normalized_code:
            raise ValueError("unit_code must not be empty")
        if len(kinds) != len(set(kinds)):
            raise ValueError("optional reference kinds must be unique")
        invalid = set(kinds) - set(_OPTIONAL_KIND_ORDER)
        if invalid:
            raise ValueError("unsupported optional reference kind")

        references: list[OptionalReference] = []
        for kind in sorted(kinds, key=_OPTIONAL_KIND_ORDER.__getitem__):
            payload = await self._call_tool(
                "ncs_analysis",
                {"mode": kind, "unit_code": normalized_code, "limit": 20},
            )
            if self._tool_failed(payload):
                if self._is_not_found(payload):
                    continue
                self._raise_tool_error("ncs_analysis", payload)
            references.extend(self._map_optional_references(payload, normalized_code, kind))

        return sorted(
            references,
            key=lambda item: (item.unit_code, _OPTIONAL_KIND_ORDER[item.kind], item.reference_id, item.text_raw),
        )

    async def check_readiness(self) -> ReadinessStatus:
        health, health_error = await self._safe_probe(self.health_url, "health")
        ready, ready_error = await self._safe_probe(self.ready_url, "readiness")

        healthy = (
            health is not None
            and 200 <= health.status_code < 300
            and self._probe_status(health) in {"ok", "healthy"}
        )
        is_ready = (
            healthy
            and ready is not None
            and 200 <= ready.status_code < 300
            and self._probe_status(ready) == "ready"
        )
        if health_error is not None:
            return ReadinessStatus(False, False, "NCS MCP health check failed.", health_error.code)
        if not healthy:
            return ReadinessStatus(False, False, "NCS MCP health endpoint is degraded.", "health_check_failed")
        if ready_error is not None:
            return ReadinessStatus(True, False, "NCS MCP readiness check failed.", ready_error.code)
        if not is_ready:
            return ReadinessStatus(True, False, "NCS MCP is not ready.", "readiness_check_failed")
        return ReadinessStatus(True, True, "ready")

    async def _call_tool(self, name: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        result = await self._run_with_timeout_retry(
            name,
            lambda: self._transport.call_tool(name, arguments),
        )
        return self._required_mapping(result, "response", name)

    async def _safe_probe(
        self,
        url: str,
        operation: str,
    ) -> tuple[HttpProbe | None, NcsSourceError | None]:
        try:
            result = await self._run_with_timeout_retry(
                operation,
                lambda: self._transport.get_json(url),
            )
        except NcsSourceError as exc:
            return None, exc
        if not isinstance(result, HttpProbe):
            return None, NcsSourceContractError(
                f"{operation} response has an invalid shape",
                operation=operation,
            )
        return result, None

    async def _run_with_timeout_retry(
        self,
        operation: str,
        call: Callable[[], Awaitable[object]],
    ) -> object:
        for attempt in range(self.max_retries + 1):
            try:
                return await asyncio.wait_for(call(), timeout=self.timeout_seconds)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                if attempt < self.max_retries:
                    continue
                raise NcsSourceTimeoutError(
                    f"NCS MCP operation timed out: {operation}",
                    operation=operation,
                ) from exc
            except NcsSourceError:
                raise
            except Exception as exc:
                raise NcsSourceUnavailableError(
                    f"NCS MCP operation is unavailable: {operation}",
                    operation=operation,
                ) from exc
        raise AssertionError("timeout retry loop exhausted unexpectedly")

    def _map_criteria(
        self,
        element: Mapping[str, object],
        element_location: str,
        warnings: list[NcsSourceWarning],
    ) -> tuple[EvidenceCriterion, ...]:
        raw_criteria = element.get("performance_criteria", element.get("criteria"))
        location = f"{element_location}.criteria"
        if raw_criteria is None:
            warnings.append(self._partial_warning(location, "Performance criteria are missing."))
            return ()
        if not self._is_sequence(raw_criteria):
            raise NcsSourceContractError(
                f"{location} must be an array",
                operation="ncs_unit_detail",
            )
        if not raw_criteria:
            warnings.append(self._partial_warning(location, "No performance criteria were returned."))

        criteria: list[EvidenceCriterion] = []
        for index, raw_item in enumerate(raw_criteria):
            item_location = f"{location}[{index}]"
            item = self._required_mapping(raw_item, item_location, "ncs_unit_detail")
            criteria_id = self._required_identifier(
                item,
                "criteria_id",
                item_location,
                "ncs_unit_detail",
            )
            text_raw = self._first_optional_text(item, keys=("criteria_text_raw", "text"))
            if text_raw is None:
                warnings.append(
                    self._partial_warning(item_location, "Criterion raw text is missing; the item was omitted.")
                )
                continue
            criteria.append(EvidenceCriterion(criteria_id=criteria_id, criteria_text_raw=text_raw))
        return tuple(sorted(criteria, key=lambda item: (item.criteria_id, item.criteria_text_raw)))

    def _map_ksa(
        self,
        element: Mapping[str, object],
        element_location: str,
        warnings: list[NcsSourceWarning],
    ) -> tuple[EvidenceKsa, ...]:
        raw_ksa = element.get("ksa")
        location = f"{element_location}.ksa"
        if raw_ksa is None:
            warnings.append(self._partial_warning(location, "KSA evidence is missing."))
            return ()
        if not self._is_sequence(raw_ksa):
            raise NcsSourceContractError(
                f"{location} must be an array",
                operation="ncs_unit_detail",
            )
        if not raw_ksa:
            warnings.append(self._partial_warning(location, "No KSA evidence was returned."))

        ksa_items: list[EvidenceKsa] = []
        for index, raw_item in enumerate(raw_ksa):
            item_location = f"{location}[{index}]"
            item = self._required_mapping(raw_item, item_location, "ncs_unit_detail")
            ksa_id = self._required_identifier(item, "ksa_id", item_location, "ncs_unit_detail")
            raw_kind = self._first_optional_text(item, keys=("ksa_type", "ksa_type_name", "type"))
            kind = self._normalize_ksa_kind(raw_kind)
            if kind is None:
                warnings.append(
                    NcsSourceWarning(
                        code="unsupported_ksa_type",
                        message="KSA type is missing or unsupported; the item was omitted.",
                        location=item_location,
                    )
                )
                continue
            text_raw = self._first_optional_text(item, keys=("ksa_text_raw", "text"))
            if text_raw is None:
                warnings.append(
                    self._partial_warning(item_location, "KSA raw text is missing; the item was omitted.")
                )
                continue
            ksa_items.append(EvidenceKsa(ksa_id=ksa_id, ksa_type=kind, ksa_text_raw=text_raw))
        return tuple(sorted(ksa_items, key=lambda item: (item.ksa_type, item.ksa_id, item.ksa_text_raw)))

    def _map_optional_references(
        self,
        payload: Mapping[str, object],
        unit_code: str,
        kind: OptionalReferenceKind,
    ) -> list[OptionalReference]:
        field_names = {
            "career_path": ("career_paths",),
            "qualification": ("qualification_links",),
            "job_base": ("job_base_links",),
        }[kind]
        rows: Sequence[object] | None = None
        for field_name in field_names:
            candidate = self._field(payload, field_name)
            if candidate is not None:
                if not self._is_sequence(candidate):
                    raise NcsSourceContractError(
                        f"ncs_analysis.{field_name} must be an array",
                        operation="ncs_analysis",
                    )
                rows = candidate
                break
        if rows is None:
            raise NcsSourceContractError(
                f"ncs_analysis response is missing {field_names[0]}",
                operation="ncs_analysis",
            )

        references: list[OptionalReference] = []
        for index, raw_row in enumerate(rows):
            location = f"{field_names[0]}[{index}]"
            row = self._required_mapping(raw_row, location, "ncs_analysis")
            row_unit_code = self._first_optional_text(row, keys=("unit_code", "matched_unit_code"))
            if row_unit_code != unit_code:
                continue
            reference_id = self._optional_reference_id(row, kind, location)
            text_raw = self._optional_reference_text(row, kind, location)
            references.append(
                OptionalReference(
                    reference_id=reference_id,
                    unit_code=unit_code,
                    kind=kind,
                    text_raw=text_raw,
                )
            )
        return references

    def _optional_reference_id(
        self,
        row: Mapping[str, object],
        kind: OptionalReferenceKind,
        location: str,
    ) -> str:
        keys = {
            "career_path": ("reference_id", "career_path_id", "id"),
            "qualification": ("reference_id", "link_id", "jm_cd", "qualification_code", "id"),
            "job_base": (
                "reference_id",
                "link_id",
                "job_base_factor_id",
                "job_base_competency_id",
                "id",
            ),
        }[kind]
        for key in keys:
            value = self._identifier(row.get(key))
            if value is not None:
                return value
        raise NcsSourceContractError(
            f"ncs_analysis response is missing a required identifier at {location}",
            operation="ncs_analysis",
        )

    def _optional_reference_text(
        self,
        row: Mapping[str, object],
        kind: OptionalReferenceKind,
        location: str,
    ) -> str:
        direct = self._first_optional_text(row, keys=("text_raw", "text"))
        if direct is not None:
            return direct
        keys = {
            "career_path": ("job_name", "competency_name"),
            "qualification": ("jm_nm", "qualification_name"),
            "job_base": ("competency_name", "factor_name"),
        }[kind]
        parts = [value for key in keys if (value := self._optional_text(row, key)) is not None]
        if parts:
            return " > ".join(parts)
        raise NcsSourceContractError(
            f"ncs_analysis response is missing reference text at {location}",
            operation="ncs_analysis",
        )

    @staticmethod
    def _normalize_ksa_kind(value: str | None) -> KsaKind | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        mapping: dict[str, KsaKind] = {
            "k": "knowledge",
            "knowledge": "knowledge",
            "지식": "knowledge",
            "s": "skill",
            "skill": "skill",
            "skills": "skill",
            "기술": "skill",
            "a": "attitude",
            "attitude": "attitude",
            "태도": "attitude",
        }
        return mapping.get(normalized)

    @staticmethod
    def _partial_warning(location: str, message: str) -> NcsSourceWarning:
        return NcsSourceWarning(code="partial_unit_evidence", message=message, location=location)

    @staticmethod
    def _candidate_richness(candidate: ScopeCandidate) -> tuple[int, int, int, int]:
        return (
            int(bool(candidate.classification_path)),
            int(candidate.duty_definition is not None),
            int(candidate.unit_level is not None),
            int(candidate.unit_definition is not None),
        )

    @staticmethod
    def _classification_path(payload: Mapping[str, object]) -> str:
        explicit = NcsMcpSourceAdapter._optional_text(payload, "classification_path")
        if explicit is not None:
            return explicit
        names = []
        for key in ("major", "middle", "small", "sub"):
            value = NcsMcpSourceAdapter._optional_text(payload, key)
            if value is None:
                value = NcsMcpSourceAdapter._optional_text(payload, f"{key}_name")
            if value is not None:
                names.append(value)
        return " > ".join(names)

    @staticmethod
    def _field(payload: Mapping[str, object], name: str) -> object | None:
        if name in payload:
            return payload[name]
        data = payload.get("data")
        if isinstance(data, Mapping):
            return data.get(name)
        return None

    def _required_sequence(
        self,
        payload: Mapping[str, object],
        name: str,
        *,
        operation: str,
    ) -> Sequence[object]:
        value = self._field(payload, name)
        if not self._is_sequence(value):
            raise NcsSourceContractError(
                f"{operation} response is missing array field: {name}",
                operation=operation,
            )
        return value

    @staticmethod
    def _required_mapping(value: object, location: str, operation: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise NcsSourceContractError(
                f"{operation} response is missing object field: {location}",
                operation=operation,
            )
        return value

    def _required_identifier(
        self,
        payload: Mapping[str, object],
        key: str,
        location: str,
        operation: str,
    ) -> str:
        value = self._identifier(payload.get(key))
        if value is None:
            raise NcsSourceContractError(
                f"{operation} response is missing required identifier: {location}.{key}",
                operation=operation,
            )
        return value

    def _required_text(
        self,
        payload: Mapping[str, object],
        keys: tuple[str, ...],
        location: str,
        operation: str,
    ) -> str:
        value = self._first_optional_text(payload, keys=keys)
        if value is None:
            raise NcsSourceContractError(
                f"{operation} response is missing required text: {location}.{keys[0]}",
                operation=operation,
            )
        return value

    @staticmethod
    def _identifier(value: object) -> str | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (str, int)):
            normalized = str(value).strip()
            return normalized or None
        return None

    @staticmethod
    def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
        value = payload.get(key)
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (str, int, float)):
            normalized = str(value).strip()
            return normalized or None
        return None

    @classmethod
    def _first_optional_text(
        cls,
        *payloads: Mapping[str, object],
        keys: tuple[str, ...],
    ) -> str | None:
        for payload in payloads:
            for key in keys:
                value = cls._optional_text(payload, key)
                if value is not None:
                    return value
        return None

    @staticmethod
    def _optional_mapping(value: object) -> Mapping[str, object]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _is_sequence(value: object) -> bool:
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))

    @staticmethod
    def _tool_failed(payload: Mapping[str, object]) -> bool:
        error = payload.get("error")
        return payload.get("ok") is False or (error is not None and error is not False)

    @staticmethod
    def _error_code(payload: Mapping[str, object]) -> str:
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
        else:
            code = error
        return str(code or "TOOL_ERROR")

    @classmethod
    def _is_not_found(cls, payload: Mapping[str, object]) -> bool:
        return "not_found" in cls._error_code(payload).casefold()

    def _raise_tool_error(self, tool_name: str, payload: Mapping[str, object]) -> None:
        code = self._error_code(payload)
        raise NcsSourceToolError(
            f"NCS MCP tool failed: {tool_name} ({code})",
            operation=tool_name,
        )

    @staticmethod
    def _probe_status(probe: HttpProbe) -> str:
        if probe.payload is None:
            return ""
        return str(probe.payload.get("status", "")).strip().casefold()

    def _normalized_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)


def _decode_sdk_result(result: object) -> object:
    """Extract JSON from an SDK CallToolResult without exporting SDK models."""

    if isinstance(result, Mapping):
        return result
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, Mapping):
        return _normalize_sdk_mapping(structured, is_error=is_error)

    for content in getattr(result, "content", ()) or ():
        text = getattr(content, "text", None)
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, Mapping):
            return _normalize_sdk_mapping(decoded, is_error=is_error)
    if is_error:
        raise RuntimeError("MCP tool reported an error")
    return result


def _normalize_sdk_mapping(payload: Mapping[str, object], *, is_error: bool) -> Mapping[str, object]:
    wrapped = payload.get("result")
    if len(payload) == 1 and isinstance(wrapped, Mapping):
        payload = wrapped
    if is_error and payload.get("ok") is not False and payload.get("error") is None:
        return {"ok": False, "error": {"code": "MCP_TOOL_ERROR"}}
    return payload


__all__ = [
    "HttpProbe",
    "NcsMcpSourceAdapter",
    "NcsMcpTransport",
    "SdkStreamableHttpTransport",
]
