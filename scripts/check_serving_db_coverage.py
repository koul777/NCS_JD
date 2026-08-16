"""Check whether the shipped serving DB carries the evidence a JD needs.

External users receive the derived ~112MB serving database, not the canonical
one.  A unit or KSA row that only exists upstream produces an empty column for
them and a complete document for us, so this compares the serving DB against the
evidence behind the telecom/broadcast job description before a release goes out.

    python scripts/check_serving_db_coverage.py
    python scripts/check_serving_db_coverage.py path/to/ncs_jd_serving.db

Exits 2 when the shipped database cannot reproduce the document.
"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = _PROJECT_ROOT / "build" / "portable-data" / "ncs_jd_serving.db"

# The units the telecom/broadcast document is written from, curated against the
# canonical database.  Losing any of them in the export breaks that document.
EXPECTED_UNITS = {
    "1901060317_25v2": "정보통신설비 운영",
    "2002010210_25v5": "구내통신 운영관리",
    "2002010416_21v1": "구내통신설비 유지보수",
    "2002010403_21v2": "영상정보처리기기(CCTV)설비공사",
    "2002010409_21v2": "주차관제설비공사",
    "2002010212_25v3": "구내 방송통신설계",
    "2002010204_25v5": "구내통신구축 공사관리",
    "0202010108_25v3": "총무문서관리",
}

# Distinctive KSA/element strings that must survive the export for the
# 필요지식·필요기술·직무수행태도 columns to be reproducible.
EXPECTED_TEXT_PROBES = (
    "주차관제설비 점검하기",
    "음향설비 점검하기",
    "영상정보처리기기(CCTV)설비 설치하기",
    "비상방송설비의 화재안전기준(NFSC 202)",
    "주차관제시스템 프로그램 운영 능력",
    "무정전 전원 공급장치(UPS) 기능 및 특성",
    "우편물 수발신하기",
)

# Single-noun queries the pool sweep depends on, with the literal matcher.
EXPECTED_SEARCH_TERMS = ("구내통신", "영상정보처리기기", "주차관제", "전관방송", "우편물")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the shipped serving DB covers a known JD.")
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        default=DEFAULT_DB,
        help=f"serving database to inspect (default: {DEFAULT_DB})",
    )
    db_path = parser.parse_args(argv).database
    if not db_path.is_file():
        print(f"serving DB not found: {db_path}")
        print("Export one with scripts/package_windows_portable.ps1, or pass a path.")
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    size_mb = db_path.stat().st_size / (1024 * 1024)
    print(f"serving DB: {db_path}  ({size_mb:.1f} MB)\n")

    print("=== tables ===")
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"  {table:<26}{count:>10,}")

    print("\n=== units the telecom/broadcast JD is written from ===")
    missing_units = []
    for code, expected_name in EXPECTED_UNITS.items():
        row = conn.execute(
            "SELECT unit_name_raw FROM competency_units WHERE unit_code = ?", (code,)
        ).fetchone()
        if row is None:
            missing_units.append(code)
            print(f"  MISSING  {code}  ({expected_name})")
            continue
        elements = conn.execute(
            "SELECT COUNT(*) FROM competency_elements WHERE unit_code = ?", (code,)
        ).fetchone()[0]
        ksa = conn.execute(
            "SELECT COUNT(*) FROM ksa_items k "
            "JOIN competency_elements e ON k.element_id = e.element_id "
            "WHERE e.unit_code = ?",
            (code,),
        ).fetchone()[0]
        flag = "OK     " if elements and ksa else "NO KSA "
        if not (elements and ksa):
            missing_units.append(code)
        print(f"  {flag}  {code}  elements={elements:<3} ksa={ksa:<4} {row[0]}")

    print("\n=== KSA / element text probes ===")
    missing_text = []
    for probe in EXPECTED_TEXT_PROBES:
        hit = conn.execute(
            "SELECT 1 FROM ksa_items WHERE ksa_text_raw = ? "
            "UNION ALL SELECT 1 FROM competency_elements WHERE element_name_raw = ? LIMIT 1",
            (probe, probe),
        ).fetchone()
        if hit is None:
            missing_text.append(probe)
        print(f"  {'OK     ' if hit else 'MISSING'}  {probe}")

    print("\n=== single-noun recall (unit-name / definition match) ===")
    weak_terms = []
    for term in EXPECTED_SEARCH_TERMS:
        pattern = f"%{term}%"
        units = conn.execute(
            "SELECT COUNT(*) FROM competency_units "
            "WHERE unit_name_raw LIKE ? OR IFNULL(api_definition, '') LIKE ?",
            (pattern, pattern),
        ).fetchone()[0]
        if units == 0:
            weak_terms.append(term)
        print(f"  {term:<20} units={units}")

    print("\n=== verdict ===")
    ok = not missing_units and not missing_text and not weak_terms
    if missing_units:
        print(f"  units missing or without KSA: {', '.join(missing_units)}")
    if missing_text:
        print(f"  text probes missing: {len(missing_text)}")
    if weak_terms:
        print(f"  terms with no unit-level recall: {', '.join(weak_terms)}")
    print("  REPRODUCIBLE from the shipped serving DB" if ok else "  NOT fully reproducible")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
