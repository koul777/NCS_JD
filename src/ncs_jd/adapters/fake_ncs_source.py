"""Deterministic in-memory NCS source used by tests and local development."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ncs_jd.application.ncs_source import (
    NcsSourcePort,
    OptionalReference,
    OptionalReferenceKind,
    ReadinessStatus,
    ScopeCandidate,
    SourceCall,
    UnitEvidenceBundle,
    UnitEvidenceNotFoundError,
)


class FakeNcsSourceAdapter(NcsSourcePort):
    """A side-effect-free fake that preserves stable ordering and records calls."""

    def __init__(
        self,
        *,
        candidates: Iterable[ScopeCandidate] = (),
        unit_bundles: Mapping[str, UnitEvidenceBundle] | None = None,
        optional_references: Iterable[OptionalReference] = (),
        readiness: ReadinessStatus | None = None,
    ) -> None:
        self._candidates = tuple(
            sorted(candidates, key=lambda item: (item.classification_path, item.unit_code, item.unit_name))
        )
        self._unit_bundles = dict(unit_bundles or {})
        self._optional_references = tuple(
            sorted(optional_references, key=lambda item: (item.unit_code, item.kind, item.reference_id))
        )
        self._readiness = readiness or ReadinessStatus(healthy=True, ready=True, message="ready")
        self.calls: list[SourceCall] = []

    async def search_scope_candidates(self, query: str, limit: int = 20) -> list[ScopeCandidate]:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self.calls.append(SourceCall("search_scope_candidates", (query, limit)))
        matches = [
            item
            for item in self._candidates
            if normalized_query
            in " ".join(
                filter(
                    None,
                    (
                        item.classification_path,
                        item.duty_definition,
                        item.unit_code,
                        item.unit_name,
                        item.unit_definition,
                    ),
                )
            ).casefold()
        ]
        return matches[:limit]

    async def load_unit_evidence(self, unit_code: str) -> UnitEvidenceBundle:
        normalized_code = unit_code.strip()
        if not normalized_code:
            raise ValueError("unit_code must not be empty")
        self.calls.append(SourceCall("load_unit_evidence", (normalized_code,)))
        try:
            return self._unit_bundles[normalized_code]
        except KeyError as exc:
            raise UnitEvidenceNotFoundError(f"unit evidence not found: {normalized_code}") from exc

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
        self.calls.append(SourceCall("load_optional_references", (normalized_code, kinds)))
        selected = set(kinds)
        return [
            item
            for item in self._optional_references
            if item.unit_code == normalized_code and item.kind in selected
        ]

    async def check_readiness(self) -> ReadinessStatus:
        self.calls.append(SourceCall("check_readiness"))
        return self._readiness
__all__ = ["FakeNcsSourceAdapter"]
