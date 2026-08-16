"""Reproduce the telecom/broadcast scope selection inside this program.

Runs the real NCS MCP adapter, optionally with the `claude` CLI selector, over a
fixed announcement whose wording sits far from NCS phrasing, then prints the
chosen scope so it can be compared against the manually curated result.  The
Kordoc parser is bypassed on purpose: this exercises scope selection, not
document parsing.

    python scripts/repro_telecom_scope.py            # with the CLI selector
    python scripts/repro_telecom_scope.py --mode deterministic

Needs the NCS MCP HTTP server running (run_ncs_mcp_http.cmd).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ncs_jd.adapters.cli_llm import CliLlmAdapter, CliScopeSelector
from ncs_jd.adapters.ncs_mcp_source import NcsMcpSourceAdapter
from ncs_jd.application.announcement_extraction import (
    AnnouncementExtraction,
    ExtractedAnnouncementItem,
    RoleCandidate,
)
from ncs_jd.application.automatic_drafting import plan_automatic_scope
from ncs_jd.application.document_parser import SourceLocator
from ncs_jd.application.llm_rewriter import LlmProvider

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

JOB_TITLE = "통신·방송설비 운영 관리"
DUTIES = (
    "구내 전화 설비 운영 및 유지·보수 관리",
    "통신설비 공사 발주·관리 및 관련 행정 업무",
    "개인정보영상장치(CCTV) 시스템 운영·관리",
    "차량 입출차시스템 관리 및 방문 차량 관리",
    "미디어 방송 설비(회의실 음향·영상 장비, 원내 안내 방송 장비 등) 운영·관리 및 기술 지원",
    "원내 시설 대관(게스트하우스 등) 및 관리",
    "우편물 관리 등 기타 소속 부서에서 부여한 업무",
)
QUALIFICATIONS = (
    "「국가기술자격법」에 따른 기술자격종목 중 방송·통신 분야 기능사 이상의 자격증 소지자",
    "연구원 「인사규정」 제13조에 해당하는 임용 결격 사유가 없는 자",
)


def _item(field: str, text: str, index: int) -> ExtractedAnnouncementItem:
    return ExtractedAnnouncementItem(
        field=field,  # type: ignore[arg-type]
        text=text,
        source_locator=SourceLocator(
            block_id=f"block-{index}", block_index=index, page_number=1
        ),
        extraction_method="explicit_label",
        confidence=0.99,
    )


def _extraction() -> AnnouncementExtraction:
    role = RoleCandidate(
        candidate_id="role-1",
        role_title=_item("role_title", JOB_TITLE, 0),
        duties=tuple(_item("duty", text, index) for index, text in enumerate(DUTIES, 1)),
        qualifications=tuple(
            _item("qualification", text, index)
            for index, text in enumerate(QUALIFICATIONS, len(DUTIES) + 1)
        ),
    )
    return AnnouncementExtraction("telecom-announcement.txt", (role,), ())


async def run(mode: str) -> int:
    source = NcsMcpSourceAdapter()

    readiness = await source.check_readiness()
    print(f"MCP readiness: healthy={readiness.healthy} ready={readiness.ready} "
          f"({readiness.message})")
    if not readiness.ready:
        print("NCS MCP is not ready; start run_ncs_mcp_http.cmd first.")
        return 1

    selector = None
    if mode == "llm":
        selector = CliScopeSelector(CliLlmAdapter(), LlmProvider.CLAUDE)
    print(f"mode: {mode}\n")

    plan = await plan_automatic_scope(
        _extraction(),
        source,
        job_title_override=JOB_TITLE,
        scope_selector=selector,
    )

    print(f"selection_mode : {plan.selection_mode}")
    print(f"job title      : {plan.title}")
    print(f"\n분류체계 ({len(plan.classification_paths)}개 세분류)")
    for path in plan.classification_paths:
        print(f"  - {path.major_code}.{path.middle_code}.{path.small_code}.{path.sub_code}  {path.label}")
    print(f"\n능력단위 ({len(plan.included_units)}개)")
    for unit in plan.included_units:
        level = f"L{unit.unit_level}" if unit.unit_level else "L?"
        print(f"  - {unit.unit_code:<20} {level:<4} {unit.unit_name}")
        print(f"      ↳ {unit.selection_reason}")
    print("\n비고/근거")
    for note in plan.match_notes:
        print(f"  * {note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the scope chosen for a fixed announcement.")
    parser.add_argument(
        "--mode",
        choices=("llm", "deterministic"),
        default="llm",
        help="whether to run the CLI scope selector (default: llm)",
    )
    return asyncio.run(run(parser.parse_args(argv).mode))


if __name__ == "__main__":
    raise SystemExit(main())
