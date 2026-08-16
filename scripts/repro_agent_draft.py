"""End-to-end agent-loop draft against the packaged serving DB.

External users get the derived ~112MB serving database and the packaged NCS MCP
sidecar, not the multi-gigabyte development database.  Running the agent against
that exact pair is the only way to see what the distribution can actually
produce, so this reuses the application's own sidecar resolution rather than
pointing at a hand-written path.

    python scripts/repro_agent_draft.py
    python scripts/repro_agent_draft.py --mcp-root release/NCS_JD-windows-x64-v0.1.4/NCS_MCP

Needs the `claude` CLI installed and logged in; the run takes minutes.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ncs_jd.application.agent_drafting import AgentProgress, AgentDraftRequest
from ncs_jd.web.app import _build_agent_runner

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

# The 13 labels the renderer supports, i.e. what the bundled template asks for.
TEMPLATE_LABELS = (
    "채용분야", "대분류", "중분류", "소분류", "세분류", "능력단위", "직무수행내용",
    "필요지식", "필요기술", "직무수행태도", "필요자격", "직업기초능력", "비고/근거",
)
# A real announcement whose wording sits far from NCS phrasing, which is the
# case the agent path exists for.
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
    "「국가기술자격법」에 따른 기술자격종목 중 방송·통신 분야 기능사 이상의 자격증 소지자"
    " (정보통신기능사, 방송통신기능사 등)",
    "연구원 「인사규정」 제13조에 해당하는 임용 결격 사유가 없는 자",
)

_started = time.monotonic()


def show(event: AgentProgress) -> None:
    elapsed = time.monotonic() - _started
    detail = f"  [{event.detail}]" if event.detail else ""
    print(f"  {elapsed:6.1f}s  {event.step:>3}. {event.kind:<12} {event.label}{detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent-loop draft against the packaged NCS MCP.")
    parser.add_argument(
        "--mcp-root",
        type=Path,
        default=None,
        help="NCS_MCP sidecar directory; defaults to the launcher's own search order",
    )
    args = parser.parse_args(argv)

    if args.mcp_root is not None:
        # The resolver already honours this variable, so overriding it keeps one
        # search order instead of a second copy that can drift.
        os.environ["NCS_MCP_ROOT"] = str(args.mcp_root.resolve())

    runner = _build_agent_runner()
    if runner is None:
        print("No usable NCS MCP sidecar was found.")
        print("Build one with scripts/package_windows_portable.ps1, or pass --mcp-root.")
        return 1

    result = runner.run_draft(
        AgentDraftRequest(
            job_title=JOB_TITLE,
            duties=DUTIES,
            template_labels=TEMPLATE_LABELS,
            qualifications=QUALIFICATIONS,
            organization_context="정부출연연구원. 병원·항공·선박 등 업종 특화 기관이 아님.",
        ),
        on_progress=show,
    )

    print(f"\n{'=' * 78}")
    print(f"turns={result.turns}  tool_calls={result.tool_calls}  "
          f"duration={result.duration_ms / 1000:.0f}s  units={len(result.unit_codes)}")
    print(f"unit_codes: {', '.join(result.unit_codes)}")
    print("=" * 78)
    for label, value in result.field_values:
        print(f"\n### {label}\n{value}")
    if result.notes:
        print("\n### notes")
        for note in result.notes:
            print(f"  - {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
