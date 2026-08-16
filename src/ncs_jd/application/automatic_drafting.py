"""Deterministic announcement-to-NCS scope planning for the simple workflow."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from ncs_jd.application.announcement_extraction import AnnouncementExtraction, RoleCandidate
from ncs_jd.application.llm_scope_selection import (
    ScopeSelectionCandidate,
    ScopeSelectionRequest,
    ScopeSelectionResult,
    ScopeSelectorPort,
)
from ncs_jd.application.ncs_source import NcsSourcePort, ScopeCandidate
from ncs_jd.domain.job_profile import ClassificationPath, IncludedUnit


_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_MAX_DUTY_QUERIES = 12
_MAX_SEARCH_VARIANTS_PER_DUTY = 8
_SEARCH_LIMIT = 40
_EXPANSION_LIMIT = 100
_MAX_SUBCATEGORIES = 3
_MAX_UNITS_PER_SUBCATEGORY = 15
_MAX_TOTAL_UNITS = 25
_LLM_POOL_LIMIT = 160
_MAX_POOL_QUERIES = 32
_MAX_ROSTER_QUERIES = 6
_MAX_SELECTION_REASON_LENGTH = 400
_PARTICLE_SUFFIXES = (
    "에서",
    "으로",
    "에게",
    "부터",
    "까지",
    "처럼",
    "보다",
    "이나",
    "거나",
    "하고",
    "이며",
    "와",
    "과",
    "의",
    "을",
    "를",
    "은",
    "는",
    "이",
    "가",
    "에",
    "로",
    "도",
    "만",
)
_SEARCH_STOPWORDS = frozenset(
    {
        "및",
        "등",
        "등의",
        "업무",
        "수행",
        "담당",
        "관련",
        "해당",
        "전반",
    }
)
_SEARCH_SYNONYMS = {
    "소방설비": ("소방시설",),
    "분전반설비": ("배전반설비",),
}
_SEMANTIC_SEARCH_EXPANSIONS = {
    # These are search-only bridges to named NCS occupations. They never add a
    # duty or evidence statement to the generated document.
    "고충상담": ("심리상담안내",),
    "성희롱": ("심리상담", "위기상담"),
    "성폭력": ("심리상담", "위기상담"),
    "인권침해": ("심리상담", "위기상담"),
    "예방교육": ("심리교육",),
    "인권보호교육": ("심리교육",),
    "자살예방교육": ("위기상담", "심리교육"),
}
_SEMANTIC_SEARCH_VALUES = frozenset(
    value for values in _SEMANTIC_SEARCH_EXPANSIONS.values() for value in values
)
_ACTION_TERMS = frozenset(
    {
        "점검",
        "정기점검",
        "자체점검",
        "자율점검",
        "검사",
        "유지보수",
        "유지관리",
        "정비",
        "설치",
        "시공",
        "시운전",
        "운영",
        "운용",
        "관리",
        "감독",
        "계획",
        "설계",
        "교체",
        "보수",
        "수리",
        "조치",
        "결함",
        "대응",
        "교육",
        "훈련",
    }
)
_MAINTENANCE_INTENT = frozenset(
    {"점검", "정기점검", "자체점검", "자율점검", "검사", "유지보수", "유지관리", "정비", "교체", "보수", "수리", "조치", "결함", "대응"}
)
_DESIGN_INTENT = frozenset({"설계", "기획", "계획"})
_INSTALLATION_INTENT = frozenset({"설치", "시공", "공사"})
_GENERIC_ASSIGNMENT_DUTY_RE = re.compile(
    r"(?:기타|그\s*밖의).*?(?:소속|부서|기관).*?(?:부여|지시|분장).*?(?:업무|사항)",
)
_ORGANIZATION_ONLY_DUTY_RES = (
    re.compile(r"(?=.*(?:성희롱|성폭력|인권침해))(?=.*(?:조사|처리))"),
    re.compile(r"(?:근무환경.*?)?실태조사"),
    re.compile(r"제반\s*업무.*?(?:기본계획|결과보고|행정업무)"),
)


class AutomaticDraftPlanningError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AutomaticScopePlan:
    role: RoleCandidate
    title: str
    duties: tuple[str, ...]
    classification_paths: tuple[ClassificationPath, ...]
    included_units: tuple[IncludedUnit, ...]
    match_notes: tuple[str, ...]
    # Callers surface this so a silent fallback to the bigram scorer is visible
    # rather than being mistaken for a poor LLM result.
    selection_mode: str = "deterministic"


@dataclass(slots=True)
class _UnitMatch:
    candidate: ScopeCandidate
    score: float = 0.0
    query_hits: int = 0


@dataclass(slots=True)
class _CategoryMatch:
    candidate: ScopeCandidate
    scores_by_duty: dict[int, float] = field(default_factory=dict)
    variants_by_duty: dict[int, set[str]] = field(default_factory=dict)
    units_by_duty: dict[int, set[str]] = field(default_factory=dict)
    evidence_by_duty: dict[int, set[str]] = field(default_factory=dict)

    @property
    def total_score(self) -> float:
        return sum(self.scores_by_duty.values())

    @property
    def duty_hits(self) -> int:
        return len(self.scores_by_duty)

    @property
    def unit_hits(self) -> int:
        return len(set().union(*self.units_by_duty.values())) if self.units_by_duty else 0


async def plan_automatic_scope(
    extraction: AnnouncementExtraction,
    source: NcsSourcePort,
    *,
    job_title_override: str | None = None,
    scope_selector: ScopeSelectorPort | None = None,
) -> AutomaticScopePlan:
    """Select subcategories first, then expand their competency units.

    With ``scope_selector`` supplied, an official CLI provider picks the units
    from an MCP-returned candidate pool instead of the bigram scorer.  Any
    selector failure falls back to the deterministic path below, so LLM-off
    behaviour and its determinism guarantee are unchanged.
    """

    override = (job_title_override or "").strip()
    role = _select_role(extraction, require_title=not bool(override))
    title = override or (role.role_title.text.strip() if role.role_title else "")
    duties = tuple(dict.fromkeys(item.text.strip() for item in role.duties if item.text.strip()))
    if not title or not duties:
        raise AutomaticDraftPlanningError(
            "announcement_job_details_missing",
            "공고문에서 직무명과 직무수행내역을 함께 찾지 못했습니다.",
        )

    matching_duties = _matching_duties(duties)
    ignored_generic_duties = tuple(duty for duty in duties if _is_ncs_matching_excluded_duty(duty))
    if not matching_duties:
        raise AutomaticDraftPlanningError(
            "announcement_job_details_missing",
            "공고문에서 NCS와 연결할 구체적인 직무수행내역을 찾지 못했습니다.",
        )

    if scope_selector is not None:
        selected_plan = await _plan_with_selector(
            source,
            scope_selector,
            role=role,
            title=title,
            duties=duties,
            matching_duties=matching_duties,
            ignored_generic_duties=ignored_generic_duties,
        )
        if selected_plan is not None:
            return selected_plan

    queries = matching_duties[:_MAX_DUTY_QUERIES]
    unit_matches: dict[str, _UnitMatch] = {}
    category_matches: dict[tuple[str, str, str, str], _CategoryMatch] = {}

    variants_by_duty = tuple(_search_variants(query) for query in queries)
    unique_variants = tuple(dict.fromkeys(variant for variants in variants_by_duty for variant in variants))
    search_results = await _search_many(source, unique_variants, _SEARCH_LIMIT)

    for query_index, query in enumerate(queries):
        seen_in_duty: set[str] = set()
        search_variants = variants_by_duty[query_index]
        duty_terms = tuple(dict.fromkeys(term for variant in search_variants for term in variant.split()))
        for variant in search_variants:
            candidates = search_results[variant]
            query_tokens = _tokens(variant)
            grouped: dict[tuple[str, str, str, str], list[ScopeCandidate]] = {}
            for candidate in candidates:
                category = _category_key(candidate)
                if category is None or not candidate.unit_code.strip() or not candidate.unit_name.strip():
                    continue
                grouped.setdefault(category, []).append(candidate)
                candidate_text = " ".join(
                    value or ""
                    for value in (
                        candidate.sub_name,
                        candidate.duty_definition,
                        candidate.unit_name,
                        candidate.unit_definition,
                    )
                )
                overlap = len(query_tokens.intersection(_tokens(candidate_text)))
                score = 1.0 + min(overlap, 4) * 0.15
                match = unit_matches.setdefault(candidate.unit_code, _UnitMatch(candidate))
                match.score += score
                if candidate.unit_code not in seen_in_duty:
                    match.query_hits += 1
                    seen_in_duty.add(candidate.unit_code)

            # Score a category once per search variant. This prevents a broad
            # category with many rows from drowning out a precise rare match.
            rarity_weight = 1.0 + (2.0 / (len(candidates) + 1))
            for category, category_candidates in grouped.items():
                representative = max(category_candidates, key=_candidate_richness)
                category_match = category_matches.setdefault(
                    category,
                    _CategoryMatch(representative),
                )
                if _candidate_richness(representative) > _candidate_richness(category_match.candidate):
                    category_match.candidate = representative
                unit_codes = {item.unit_code.strip() for item in category_candidates}
                unit_coverage = min(len(unit_codes), 4) * 0.08
                candidate_text = " ".join(
                    value or ""
                    for item in category_candidates
                    for value in (
                        item.sub_name,
                        item.duty_definition,
                        item.unit_name,
                        item.unit_definition,
                    )
                ).casefold()
                lexical_fit = min(sum(term in candidate_text for term in duty_terms), 4) * 0.25
                category_match.scores_by_duty[query_index] = (
                    category_match.scores_by_duty.get(query_index, 0.0)
                    + rarity_weight
                    + unit_coverage
                    + lexical_fit
                )
                category_match.variants_by_duty.setdefault(query_index, set()).add(variant)
                category_match.units_by_duty.setdefault(query_index, set()).update(unit_codes)
                category_match.evidence_by_duty.setdefault(query_index, set()).update(
                    " ".join(
                        value or ""
                        for value in (
                            item.classification_path,
                            item.duty_definition,
                            item.unit_name,
                            item.unit_definition,
                        )
                    )
                    for item in category_candidates
                )

    selected_categories = _select_categories(category_matches, queries, title)
    if not selected_categories:
        raise AutomaticDraftPlanningError(
            "ncs_scope_not_found",
            "직무수행내역과 연결되는 NCS 세분류를 찾지 못했습니다.",
        )

    # Once a subcategory is selected, query it explicitly so the scope is not
    # limited to only the few units returned for an individual duty sentence.
    expansion_queries = {
        category: (
            category_matches[category].candidate.sub_name
            or category_matches[category].candidate.classification_path
        ).strip()[:500]
        for category in selected_categories
    }
    expansion_results = await _search_many(
        source,
        tuple(dict.fromkeys(expansion_queries.values())),
        _EXPANSION_LIMIT,
    )
    category_budgets = _category_unit_budgets(len(selected_categories))
    for category_index, category in enumerate(selected_categories):
        representative = category_matches[category].candidate
        expanded = expansion_results[expansion_queries[category]]
        baseline = category_matches[category].total_score / max(1, category_matches[category].duty_hits)
        for rank, candidate in enumerate(expanded):
            if _category_key(candidate) != category or not candidate.unit_code or not candidate.unit_name:
                continue
            match = unit_matches.setdefault(candidate.unit_code, _UnitMatch(candidate))
            match.score += baseline / (rank + 2)

    paths: list[ClassificationPath] = []
    units: list[IncludedUnit] = []
    notes: list[str] = []
    if ignored_generic_duties:
        notes.append(
            "NCS 매칭 제외 — '"
            + "', '".join(ignored_generic_duties)
            + "'은 조직 고유 지시 업무로 보존하되 NCS 세분류·능력단위 선정 근거로 사용하지 않았습니다."
        )
    for category_index, category in enumerate(selected_categories):
        matches = [item for item in unit_matches.values() if _category_key(item.candidate) == category]
        category_variants = {
            variant
            for variants in category_matches[category].variants_by_duty.values()
            for variant in variants
        }
        if category_variants and category_variants.issubset(_SEMANTIC_SEARCH_VALUES):
            # Search bridges identify candidate units, not permission to copy
            # an entire occupation. Keep only units returned by the bounded
            # duty queries; expansion remains available to normal searches.
            matches = [item for item in matches if item.query_hits > 0]
        matches.sort(key=lambda item: (-item.query_hits, -item.score, item.candidate.unit_code))
        matches = matches[:_MAX_UNITS_PER_SUBCATEGORY]
        if not matches:
            continue
        representative = matches[0].candidate
        paths.append(
            ClassificationPath(
                major_code=category[0],
                middle_code=category[1],
                small_code=category[2],
                sub_code=category[3],
                label=_classification_label(representative),
            )
        )
        remaining = min(category_budgets[category_index], _MAX_TOTAL_UNITS - len(units))
        for match in matches[:remaining]:
            candidate = match.candidate
            units.append(
                IncludedUnit(
                    unit_code=candidate.unit_code.strip(),
                    unit_name=candidate.unit_name.strip(),
                    unit_level=candidate.unit_level.strip() if candidate.unit_level else None,
                    selection_reason="공고문 직무수행내역 기반 자동 매칭·검토 필요",
                )
            )
        subcategory = representative.sub_name or representative.classification_path
        definition = (representative.duty_definition or "세분류 직무정의 없음").strip()
        matched_terms = sorted(
            {
                variant
                for variants in category_matches[category].variants_by_duty.values()
                for variant in variants
            },
            key=lambda value: (len(value), value),
        )
        notes.append(
            f"핵심 매칭 근거 — {category[3]} {subcategory}: 공고문 업무 "
            f"{category_matches[category].duty_hits}건의 핵심어 "
            f"'{', '.join(matched_terms[:8])}'와 NCS 검색 결과를 합산해 선정했습니다. "
            f"직무정의: {definition[:350]}"
        )
        if len(units) >= _MAX_TOTAL_UNITS:
            break

    if not paths or not units:
        raise AutomaticDraftPlanningError(
            "ncs_units_not_found",
            "선정 세분류에서 사용할 NCS 능력단위를 찾지 못했습니다.",
        )
    notes.append(
        f"선정 능력단위 {len(units)}개: "
        + ", ".join(f"{_subcategory_display_name(path)} 세분류" for path in paths)
        + ". 필요지식·기술·태도는 선택 능력단위요소의 NCS KSA 원문에서 구성합니다."
    )
    notes.append(
        "직업기초능력과 자격은 능력단위 KSA와 별도 체계이므로 NCS 직접근거로 자동 생성하지 않습니다."
    )
    return AutomaticScopePlan(role, title, duties, tuple(paths), tuple(units), tuple(notes))


async def _plan_with_selector(
    source: NcsSourcePort,
    selector: ScopeSelectorPort,
    *,
    role: RoleCandidate,
    title: str,
    duties: tuple[str, ...],
    matching_duties: tuple[str, ...],
    ignored_generic_duties: tuple[str, ...],
) -> AutomaticScopePlan | None:
    """Let the selector pick units, or return ``None`` to use the bigram plan."""

    pool = await _llm_candidate_pool(source, matching_duties)
    if not pool:
        return None
    result = await _select_scope_safely(selector, title, matching_duties, pool)
    if result is None:
        return None

    # The selector reports duties it could not cover.  Searching those terms
    # explicitly is the retrieval round a human would run next, and it is what
    # rescues a duty whose vocabulary never appeared in the first sweep.
    if result.unmatched_duties:
        extra = await _llm_candidate_pool(
            source,
            tuple(result.unmatched_duties),
            exclude=frozenset(candidate.unit_code for candidate in pool),
        )
        if extra:
            keep = max(0, _LLM_POOL_LIMIT - len(extra))
            widened = pool[:keep] + extra
            retried = await _select_scope_safely(selector, title, matching_duties, widened)
            if retried is not None:
                pool, result = widened, retried

    by_code = {candidate.unit_code: candidate for candidate in pool}
    chosen = [
        (by_code[choice.unit_code], choice.reason)
        for choice in result.choices
        if choice.unit_code in by_code
    ]
    if not chosen:
        return None

    paths: list[ClassificationPath] = []
    units: list[IncludedUnit] = []
    seen_categories: set[tuple[str, str, str, str]] = set()
    for candidate, reason in chosen[:_MAX_TOTAL_UNITS]:
        category = _category_key(candidate)
        if category is None:
            continue
        if category not in seen_categories:
            seen_categories.add(category)
            paths.append(
                ClassificationPath(
                    major_code=category[0],
                    middle_code=category[1],
                    small_code=category[2],
                    sub_code=category[3],
                    label=_classification_label(candidate),
                )
            )
        units.append(
            IncludedUnit(
                unit_code=candidate.unit_code.strip(),
                unit_name=candidate.unit_name.strip(),
                unit_level=candidate.unit_level.strip() if candidate.unit_level else None,
                selection_reason=(
                    f"CLI 보조 선정·검토 필요: {reason}"[:_MAX_SELECTION_REASON_LENGTH]
                ),
            )
        )
    if not paths or not units:
        return None

    notes: list[str] = [
        "능력단위 선정 방식 — 공식 CLI 제공자가 NCS MCP 검색 결과 "
        f"{len(pool)}개 후보 중에서 선정했습니다. 제공자는 검색으로 확인된 능력단위 코드만 "
        "고를 수 있으며 능력단위명·정의·분류·수준은 모두 NCS 원천 값을 그대로 사용합니다."
    ]
    if ignored_generic_duties:
        notes.append(
            "NCS 매칭 제외 — '"
            + "', '".join(ignored_generic_duties)
            + "'은 조직 고유 지시 업무로 보존하되 NCS 세분류·능력단위 선정 근거로 사용하지 않았습니다."
        )
    if result.unmatched_duties:
        notes.append(
            "NCS 근거 미확보 업무 — '"
            + "', '".join(result.unmatched_duties)
            + "'은 후보 능력단위로 연결하지 못했습니다. 조직 입력으로만 보존됩니다."
        )
    notes.append(
        f"선정 능력단위 {len(units)}개: "
        + ", ".join(f"{_subcategory_display_name(path)} 세분류" for path in paths)
        + ". 필요지식·기술·태도는 선택 능력단위요소의 NCS KSA 원문에서 구성합니다."
    )
    notes.append(
        "직업기초능력과 자격은 능력단위 KSA와 별도 체계이므로 NCS 직접근거로 자동 생성하지 않습니다."
    )
    return AutomaticScopePlan(
        role,
        title,
        duties,
        tuple(paths),
        tuple(units),
        tuple(notes),
        selection_mode="llm_assisted",
    )


async def _llm_candidate_pool(
    source: NcsSourcePort,
    duties: tuple[str, ...],
    *,
    exclude: frozenset[str] = frozenset(),
) -> tuple[ScopeCandidate, ...]:
    """Recall a wide pool in two phases, then let the selector be the filter.

    Phase one uses single nouns because ``ncs_search`` matches literal
    substrings: the space-joined pairs the deterministic planner builds mostly
    return nothing.  Phase two re-queries each subcategory *by name*, which
    matches the ``duty_definition`` carried on every unit in that subcategory
    and so returns its whole roster.  Without phase two the selector would
    choose from only the handful of units whose own name happened to contain a
    duty keyword, which is the recall gap that made keyword-only matching miss
    the right unit.
    """

    queries: list[str] = []
    for terms in (
        tuple(_domain_terms(duty) for duty in duties[:_MAX_DUTY_QUERIES]),
        tuple(_query_terms(duty) for duty in duties[:_MAX_DUTY_QUERIES]),
    ):
        for duty_terms in terms:
            for term in duty_terms:
                if term not in queries:
                    queries.append(term)
    if not queries:
        return ()

    pool: dict[str, ScopeCandidate] = {}
    keyword_results = await _search_many(
        source,
        tuple(queries[:_MAX_POOL_QUERIES]),
        _SEARCH_LIMIT,
    )
    _absorb_candidates(keyword_results.values(), pool, exclude)

    subcategory_frequency: Counter[str] = Counter()
    for candidate in pool.values():
        name = (candidate.sub_name or "").strip()
        if name:
            subcategory_frequency[name] += 1
    roster_queries = tuple(
        name for name, _ in subcategory_frequency.most_common(_MAX_ROSTER_QUERIES)
    )
    if roster_queries:
        roster_results = await _search_many(source, roster_queries, _EXPANSION_LIMIT)
        _absorb_candidates(roster_results.values(), pool, exclude)

    # Order by how strongly each subcategory was implicated so that trimming to
    # the pool limit drops fringe subcategories rather than roster members of a
    # clearly matched one.
    ordered = sorted(
        pool.values(),
        key=lambda item: (
            -subcategory_frequency.get((item.sub_name or "").strip(), 0),
            item.classification_path,
            item.unit_code,
        ),
    )
    return tuple(ordered[:_LLM_POOL_LIMIT])


def _absorb_candidates(
    result_groups: Iterable[list[ScopeCandidate]],
    pool: dict[str, ScopeCandidate],
    exclude: frozenset[str],
) -> None:
    """Merge search results into the pool, keeping the richest row per unit."""

    for candidates in result_groups:
        for candidate in candidates:
            code = candidate.unit_code.strip()
            if not code or code in exclude or not candidate.unit_name.strip():
                continue
            if _category_key(candidate) is None:
                continue
            previous = pool.get(code)
            if previous is None or _candidate_richness(candidate) > _candidate_richness(previous):
                pool[code] = candidate


async def _select_scope_safely(
    selector: ScopeSelectorPort,
    title: str,
    duties: tuple[str, ...],
    pool: tuple[ScopeCandidate, ...],
) -> ScopeSelectionResult | None:
    """Run the selector off the event loop, treating any failure as a fallback.

    Every failure mode here — provider missing, not logged in, usage exhausted,
    malformed output, unknown unit code — has the same correct response: use the
    deterministic plan instead of blocking the draft.
    """

    try:
        request = ScopeSelectionRequest(
            job_title=title,
            duties=duties,
            candidates=tuple(
                ScopeSelectionCandidate(
                    unit_code=candidate.unit_code.strip(),
                    unit_name=candidate.unit_name.strip(),
                    classification_label=_classification_label(candidate),
                    unit_definition=candidate.unit_definition,
                    duty_definition=candidate.duty_definition,
                )
                for candidate in pool
            ),
        )
    except ValueError:
        return None
    try:
        return await asyncio.to_thread(selector.select_scope, request)
    except Exception:
        return None


def _select_role(
    extraction: AnnouncementExtraction,
    *,
    require_title: bool = True,
) -> RoleCandidate:
    eligible = [
        (index, role)
        for index, role in enumerate(extraction.role_candidates)
        if (role.role_title is not None or not require_title) and role.duties
    ]
    if not eligible:
        raise AutomaticDraftPlanningError(
            "announcement_job_details_missing",
            "공고문에서 직무명과 직무수행내역을 찾지 못했습니다.",
        )
    # More concrete duties win; source order is the stable tie-breaker.
    return max(
        eligible,
        key=lambda item: (
            sum(not _is_generic_assignment_duty(duty.text) for duty in item[1].duties),
            len(item[1].duties),
            -item[0],
        ),
    )[1]


def _is_generic_assignment_duty(value: str) -> bool:
    normalized = " ".join(value.split())
    return bool(_GENERIC_ASSIGNMENT_DUTY_RE.search(normalized))


def _is_ncs_matching_excluded_duty(value: str) -> bool:
    """Keep broad or legally sensitive duties as organization input only."""

    normalized = " ".join(value.split())
    return _is_generic_assignment_duty(normalized) or any(
        pattern.search(normalized) for pattern in _ORGANIZATION_ONLY_DUTY_RES
    )


def _matching_duties(duties: tuple[str, ...]) -> tuple[str, ...]:
    expanded: list[str] = []
    for duty in duties:
        if _is_ncs_matching_excluded_duty(duty):
            continue
        for clause in _expand_compound_duty(duty):
            if clause and clause not in expanded:
                expanded.append(clause)
    return tuple(expanded)


def _expand_compound_duty(value: str) -> tuple[str, ...]:
    """Split a parenthesized equipment list while preserving stated intent."""

    normalized = " ".join(value.replace("유지 보수", "유지보수").split())
    intent = _stated_action_intent(normalized)
    if not intent:
        return (normalized,)

    groups = re.findall(r"\(([^()]*)\)", normalized)
    list_group = next(
        (
            group
            for group in groups
            if len(re.split(r"[,;·]", group)) >= 2
        ),
        None,
    )
    if list_group is None:
        return (normalized,)

    clauses: list[str] = []
    for raw_item in re.split(r"[,;·]", list_group):
        item = re.sub(r"(?:\s+등(?:의)?)$", "", raw_item).strip(" -")
        if item:
            clauses.append(f"{item} {intent}")

    # Keep a stated whole-facility target as the general electrical scope.
    outside = normalized.replace(f"({list_group})", " ")
    facility_match = re.search(
        rf"([0-9A-Za-z가-힣]+시설물)\s*(?:{re.escape(intent)})",
        outside,
    )
    if facility_match:
        clauses.append(f"{facility_match.group(1)} {intent}")
    return tuple(dict.fromkeys(clauses)) or (normalized,)


def _stated_action_intent(value: str) -> str:
    normalized = value.casefold()
    for phrase in (
        "유지보수",
        "유지관리",
        "정기점검",
        "자체점검",
        "자율점검",
        "점검",
        "정비",
        "시운전",
        "시공",
        "설치",
        "운영",
        "설계",
    ):
        if phrase in normalized:
            return phrase
    return ""


def _select_categories(
    matches: dict[tuple[str, str, str, str], _CategoryMatch],
    duties: tuple[str, ...],
    title: str,
) -> tuple[tuple[str, str, str, str], ...]:
    if not matches:
        return ()
    primary_by_duty: list[tuple[int, tuple[str, str, str, str]]] = []
    for duty_index, duty in enumerate(duties):
        candidates = [key for key, value in matches.items() if duty_index in value.scores_by_duty]
        if not candidates:
            continue
        domain_terms = _domain_terms(duty)
        prefix_fit = {
            key: max(
                (
                    _prefix_similarity(term, matches[key].candidate.sub_name or "")
                    for term in domain_terms
                ),
                default=0.0,
            )
            for key in candidates
        }
        strongest_prefix = max(prefix_fit.values(), default=0.0)
        if strongest_prefix >= 0.4:
            candidates = [
                key
                for key in candidates
                if prefix_fit[key] >= 0.4
            ]
            intent_name_fit = {
                key: _subcategory_intent_fit(duty, matches[key].candidate.sub_name or "")
                for key in candidates
            }
            strongest_intent = max(intent_name_fit.values(), default=0.0)
            if strongest_intent > 0:
                candidates = [
                    key for key in candidates if intent_name_fit[key] == strongest_intent
                ]

        def category_fit(key: tuple[str, str, str, str]) -> float:
            match = matches[key]
            domain_text = " ".join(domain_terms) or duty
            action_text = " ".join(
                term for term in _query_terms(duty) if term in _ACTION_TERMS
            )
            evidence_fit = max(
                (
                    _text_similarity(domain_text, evidence)
                    for evidence in match.evidence_by_duty.get(duty_index, set())
                ),
                default=0.0,
            )
            classification = " ".join(
                value or ""
                for value in (
                    match.candidate.classification_path,
                    match.candidate.sub_name,
                    match.candidate.duty_definition,
                )
            )
            subcategory_fit = _text_similarity(domain_text, match.candidate.sub_name or "")
            title_fit = _text_similarity(title, classification)
            action_fit = _text_similarity(action_text, classification) if action_text else 0.0
            intent_fit = _intent_alignment(duty, classification)
            variant_coverage = len(match.variants_by_duty.get(duty_index, set()))
            unit_coverage = len(match.units_by_duty.get(duty_index, set()))
            return (
                2.0 * subcategory_fit
                + 2.0 * action_fit
                + 2.5 * intent_fit
                + title_fit
                + 0.7 * evidence_fit
                + 0.20 * variant_coverage
                + 0.12 * min(unit_coverage, 6)
                + 0.15 * match.duty_hits
            )

        primary_by_duty.append(
            (
                duty_index,
                min(
                candidates,
                key=lambda key: (
                    -category_fit(key),
                    -matches[key].scores_by_duty[duty_index],
                    -matches[key].duty_hits,
                    -len(matches[key].units_by_duty.get(duty_index, set())),
                    -matches[key].unit_hits,
                    key,
                ),
                ),
            )
        )
    if not primary_by_duty:
        return ()

    # Preserve specialized maintenance occupations (for example elevator and
    # fire-safety work) before filling the remaining slot with the category
    # that best consolidates the overall role. This prevents two related
    # electrical duties from consuming separate slots and dropping fire work.
    specialized_scores: dict[tuple[str, str, str, str], tuple[float, float, float]] = {}
    for duty_index, duty in enumerate(duties):
        candidates = [key for key, value in matches.items() if duty_index in value.scores_by_duty]
        if not candidates:
            continue
        domain_terms = _domain_terms(duty)
        for key in candidates:
            subcategory = matches[key].candidate.sub_name or ""
            prefix_fit = max(
                (_prefix_similarity(term, subcategory) for term in domain_terms),
                default=0.0,
            )
            intent_fit = _subcategory_intent_fit(duty, subcategory)
            # A weak two-character overlap such as 전기시설물→전기설비운영
            # is a general-category signal, not a specialized asset match.
            if prefix_fit < 0.5 or intent_fit <= 0:
                continue
            score = (
                prefix_fit,
                intent_fit,
                matches[key].scores_by_duty[duty_index],
            )
            previous = specialized_scores.get(key)
            if previous is None or score > previous:
                specialized_scores[key] = score

    specialized = sorted(
        specialized_scores,
        key=lambda key: (
            -specialized_scores[key][0],
            -specialized_scores[key][1],
            -specialized_scores[key][2],
            key,
        ),
    )
    reserved = specialized[: _MAX_SUBCATEGORIES - 1]

    def overall_fit(key: tuple[str, str, str, str]) -> float:
        match = matches[key]
        classification = " ".join(
            value or ""
            for value in (
                match.candidate.classification_path,
                match.candidate.sub_name,
                match.candidate.duty_definition,
            )
        )
        intent_fit = sum(
            max(0.0, _intent_alignment(duty, classification))
            for duty in duties
        )
        all_evidence = " ".join(
            evidence
            for values in match.evidence_by_duty.values()
            for evidence in values
        ).casefold()
        distinct_domain_coverage = sum(
            term in all_evidence
            for term in dict.fromkeys(
                term for duty in duties for term in _domain_terms(duty)
            )
        )
        return (
            3.0 * _text_similarity(title, classification)
            + 1.5 * match.duty_hits
            + 0.25 * match.unit_hits
            + 4.0 * distinct_domain_coverage
            + intent_fit
        )

    general_ranked = sorted(
        (key for key in matches if key not in reserved),
        key=lambda key: (-overall_fit(key), -matches[key].total_score, key),
    )
    general_selected: list[tuple[str, str, str, str]] = []
    general_slots = _MAX_SUBCATEGORIES - len(reserved)
    for key in general_ranked:
        if key not in general_selected:
            general_selected.append(key)
        if len(general_selected) >= general_slots:
            break
    return tuple(general_selected + reserved)


def _category_unit_budgets(category_count: int) -> tuple[int, ...]:
    if category_count <= 0:
        return ()
    if category_count == 1:
        return (min(_MAX_UNITS_PER_SUBCATEGORY, _MAX_TOTAL_UNITS),)
    if category_count == 2:
        return (15, 10)
    return (14, 6, 5)


def _category_key(candidate: ScopeCandidate) -> tuple[str, str, str, str] | None:
    values = (candidate.major_code, candidate.middle_code, candidate.small_code, candidate.sub_code)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    return tuple(value.strip() for value in values)  # type: ignore[return-value]


def _classification_label(candidate: ScopeCandidate) -> str:
    names = (candidate.major_name, candidate.middle_name, candidate.small_name, candidate.sub_name)
    if all(isinstance(name, str) and name.strip() for name in names):
        return " > ".join(str(name).strip() for name in names)
    return candidate.classification_path.strip()


def _subcategory_display_name(path: ClassificationPath) -> str:
    parts = [part.strip() for part in path.label.split(">") if part.strip()]
    return parts[-1] if parts else path.sub_code


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(value)}


def _search_variants(value: str) -> tuple[str, ...]:
    terms = list(_query_terms(value))
    if not terms:
        normalized = value.strip()
        return (normalized,) if normalized else ()

    normalized_value = value.casefold()
    variants: list[str] = list(
        dict.fromkeys(
            expansion
            for trigger, expansions in _SEMANTIC_SEARCH_EXPANSIONS.items()
            if trigger in normalized_value
            for expansion in expansions
        )
    )
    if variants:
        # Sensitive-domain bridges are deliberately exclusive. Combining them
        # with generic fragments such as "사건 조사" or "교육 운영" can rank
        # unrelated occupations (for example aviation accident investigation).
        return tuple(variants[:_MAX_SEARCH_VARIANTS_PER_DUTY])
    domain_terms = [term for term in terms if term not in _ACTION_TERMS]
    # A noun/action pair is more precise when the MCP full sentence search is
    # too restrictive (for example, "소방시설 자체점검").
    if domain_terms:
        anchor = domain_terms[0]
        for term in terms:
            if term == anchor:
                continue
            pair = f"{anchor} {term}"
            if pair not in variants:
                variants.append(pair)
        # Generic actions such as "점검" must not be searched alone because
        # they match unrelated occupations. Domain terms are safe fallbacks.
        variants.extend(term for term in domain_terms if term not in variants)
    else:
        variants.extend(terms)
    for variant in tuple(variants):
        for source_term, replacements in _SEARCH_SYNONYMS.items():
            if source_term not in variant:
                continue
            for replacement in replacements:
                synonym_variant = variant.replace(source_term, replacement)
                if synonym_variant not in variants:
                    variants.append(synonym_variant)
    return tuple(variants[:_MAX_SEARCH_VARIANTS_PER_DUTY])


def _query_terms(value: str) -> tuple[str, ...]:
    terms: list[str] = []
    for raw_token in _TOKEN_RE.findall(value):
        token = _strip_particle(raw_token.casefold())
        if len(token) < 2 or token in _SEARCH_STOPWORDS or token in terms:
            continue
        terms.append(token)
    return tuple(terms)


def _domain_terms(value: str) -> tuple[str, ...]:
    return tuple(term for term in _query_terms(value) if term not in _ACTION_TERMS)


def _strip_particle(value: str) -> str:
    token = value
    for suffix in _PARTICLE_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def _candidate_richness(candidate: ScopeCandidate) -> tuple[int, int, int, int]:
    return (
        int(bool(candidate.sub_name)),
        int(bool(candidate.duty_definition)),
        int(bool(candidate.unit_definition)),
        len(candidate.classification_path),
    )


def _text_similarity(left: str, right: str) -> float:
    left_grams = _character_ngrams(left)
    right_grams = _character_ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams.intersection(right_grams)) / (
        (len(left_grams) * len(right_grams)) ** 0.5
    )


def _character_ngrams(value: str) -> set[str]:
    normalized = "".join(re.findall(r"[0-9A-Za-z가-힣]+", value.casefold()))
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def _prefix_similarity(left: str, right: str) -> float:
    normalized_left = "".join(re.findall(r"[0-9A-Za-z가-힣]+", left.casefold()))
    normalized_right = "".join(re.findall(r"[0-9A-Za-z가-힣]+", right.casefold()))
    common = 0
    for left_character, right_character in zip(normalized_left, normalized_right):
        if left_character != right_character:
            break
        common += 1
    if common < 2 or not normalized_left:
        return 0.0
    return common / len(normalized_left)


def _intent_alignment(duty: str, classification: str) -> float:
    duty_terms = set(_query_terms(duty))
    target = classification.casefold()
    score = 0.0
    for requested, positive, competing in (
        (_MAINTENANCE_INTENT, _MAINTENANCE_INTENT, _DESIGN_INTENT | _INSTALLATION_INTENT),
        (_DESIGN_INTENT, _DESIGN_INTENT, _MAINTENANCE_INTENT | _INSTALLATION_INTENT),
        (_INSTALLATION_INTENT, _INSTALLATION_INTENT, _MAINTENANCE_INTENT | _DESIGN_INTENT),
    ):
        if not duty_terms.intersection(requested):
            continue
        if any(term in target for term in positive):
            score += 1.0
        elif any(term in target for term in competing):
            score -= 1.0
    return score


def _subcategory_intent_fit(duty: str, subcategory: str) -> float:
    duty_terms = set(_query_terms(duty))
    target = subcategory.casefold()
    if duty_terms.intersection(_MAINTENANCE_INTENT):
        if any(term in target for term in ("정비", "유지", "운영", "안전관리", "점검")):
            return 2.0
        if any(term in target for term in ("설계", "공사", "시공", "설치")):
            return -1.0
    if duty_terms.intersection(_DESIGN_INTENT) and "설계" in target:
        return 2.0
    if duty_terms.intersection(_INSTALLATION_INTENT) and any(
        term in target for term in ("설치", "공사", "시공")
    ):
        return 2.0
    return 0.0


async def _search_many(
    source: NcsSourcePort,
    queries: tuple[str, ...],
    limit: int,
) -> dict[str, list[ScopeCandidate]]:
    results: dict[str, list[ScopeCandidate]] = {}
    for query in queries:
        results[query] = await source.search_scope_candidates(query, limit)
    return results


__all__ = [
    "AutomaticDraftPlanningError",
    "AutomaticScopePlan",
    "plan_automatic_scope",
]
