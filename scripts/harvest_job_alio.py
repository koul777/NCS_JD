"""Harvest job.alio recruiting notices, download attachments, and run learning.

Workflow:
1. Crawl recruit.do pages and collect announcement indexes.
2. Download attachment files from recruitview pages.
3. Optionally execute batch_extract_cases.py for offline learning input.
4. Optionally run relation analysis to map notice-JD links.
5. Optionally build notice-JD case library from relation output.
6. Optionally execute suggest_announcement_aliases.py for unresolved-label feedback.

Modes:
- `--run-learning`: extract + alias suggestion only.
- `--run-relation`: run relation analysis.
- `--run-case-library`: run relation + case library build.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Sequence

import html
import requests

BASE_URL = "https://job.alio.go.kr"
LIST_URL = f"{BASE_URL}/recruit.do"
VIEW_URL = f"{BASE_URL}/recruitview.do"
DOWNLOAD_HOST_PREFIX = "https://www.alio.go.kr/download/download.json"

RE_DETAIL_LINK = re.compile(
    r'<a[^>]+href="(/recruitview\.do\?idx=(\d+))"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

RE_DOWNLOAD_LINK = re.compile(
    r'<a[^>]+href="([^"]*download\.json\?fileNo=\d+[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

RE_PAGE_ANCHOR = re.compile(r"goPage\((\d+)\)")

RE_TAG = re.compile(r"<[^>]+>")

NOTICE_KEYWORDS = tuple(keyword.replace(" ", "") for keyword in ("recruit", "notice", "posting", "채용"))
JD_KEYWORDS = tuple(keyword.replace(" ", "") for keyword in ("jobdescription", "jobspec", "직무", "JD"))

CONTENT_TYPE_EXT = {
    "application/pdf": ".pdf",
    "application/haansofthwp": ".hwp",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/zip": ".zip",
}


@dataclass(frozen=True)
class ParsedNotice:
    idx: str
    title: str
    source_url: str


@dataclass(frozen=True)
class ParsedAttachment:
    idx: str
    label: str
    url: str
    category: str


@dataclass(frozen=True)
class HarvestStats:
    total_pages: int = 0
    scanned_announcements: int = 0
    new_announcements: int = 0
    downloaded_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    failed_notices: int = 0


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\u200b", "")
    text = RE_TAG.sub("", text)
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _slug(text: str, max_len: int = 90) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n]+', " ", text or "").strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len] if len(text) > max_len else text


def _extract_notices(html_text: str) -> list[ParsedNotice]:
    notices: list[ParsedNotice] = []
    for match in RE_DETAIL_LINK.finditer(html_text):
        href, idx, raw_title = match.groups()
        title = _clean_text(raw_title)
        title = title or f"notice-{idx}"
        source_url = f"{BASE_URL}{href}"
        notices.append(ParsedNotice(idx=idx, title=title, source_url=source_url))
    return notices


def _extract_attachments(html_text: str, idx: str) -> list[ParsedAttachment]:
    attachments: list[ParsedAttachment] = []
    for match in RE_DOWNLOAD_LINK.finditer(html_text):
        href, raw_label = match.groups()
        label = _clean_text(raw_label)
        if not label:
            continue
        url = urllib.parse.urljoin(BASE_URL, href)
        if not url.startswith(DOWNLOAD_HOST_PREFIX):
            continue
        text = (label.replace(" ", "").lower())
        if any(keyword in text for keyword in NOTICE_KEYWORDS):
            category = "notice"
        elif any(keyword in text for keyword in JD_KEYWORDS):
            category = "job_description"
        else:
            category = "other"
        attachments.append(ParsedAttachment(idx=idx, label=label, url=url, category=category))
    return attachments


def _guess_extension(path_name: str, content_type: str | None, label: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[ct]
    for ext in (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".txt", ".xls", ".xlsx"):
        if label.lower().endswith(ext):
            return ext
    if ".json" in path_name.lower():
        return ".bin"
    return ".bin"


def _extract_filename_from_disposition(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None

    match = re.search(
        r"filename\*=(?:UTF-8''|utf-8'')(?P<name>[^;]+)",
        content_disposition,
        re.IGNORECASE,
    )
    if match:
        value = match.group("name").strip().strip('"').strip("'")
        value = urllib.parse.unquote(value)
        return _slug(value)

    match = re.search(r'filename="(?P<name>[^"]+)"', content_disposition, re.IGNORECASE)
    if match:
        value = match.group("name").strip().strip('"')
        value = urllib.parse.unquote(value)
        return _slug(value)

    match = re.search(r"filename=(?P<name>[^;]+)", content_disposition, re.IGNORECASE)
    if not match:
        return None
    value = match.group("name").strip().strip('"').strip("'")
    value = urllib.parse.unquote(value)
    return _slug(value)


def fetch_notice_list(session: requests.Session, params: dict[str, str]) -> tuple[list[ParsedNotice], int]:
    r = session.get(LIST_URL, params=params, timeout=30)
    r.raise_for_status()
    html_text = r.text
    notices = _extract_notices(html_text)
    page_candidates = [int(p) for p in RE_PAGE_ANCHOR.findall(html_text)]
    max_page = max(page_candidates) if page_candidates else 1
    return notices, max_page


def fetch_detail(session: requests.Session, idx: str) -> str:
    url = f"{VIEW_URL}?idx={idx}"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def download_file(session: requests.Session, url: str, out_dir: Path, idx: str, attachment: ParsedAttachment, *, force: bool) -> Path | None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ref_url = f"{VIEW_URL}?idx={idx}"
    r = session.get(
        url,
        timeout=60,
        headers={
            "Referer": ref_url,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        stream=True,
    )
    if r.status_code != 200:
        return None
    content_type = r.headers.get("content-type", "")
    filename = _extract_filename_from_disposition(r.headers.get("content-disposition")) or ""
    if not filename:
        parsed = urllib.parse.urlparse(r.url)
        query = urllib.parse.parse_qs(parsed.query)
        file_no = query.get("fileNo", ["file"])[0]
        ext = _guess_extension(parsed.path, content_type, attachment.label)
        file_no = urllib.parse.unquote(file_no)
        label_slug = _slug(attachment.label or "attachment")
        filename = f"{idx}-{label_slug}-{file_no}{ext}"
        if len(filename) > 220:
            filename = f"{idx}_{_slug(label_slug)[:80]}-{file_no}{ext}"
    else:
        if "." not in filename:
            ext = _guess_extension(r.url, content_type, attachment.label)
            filename = f"{filename}{ext}"
    target = out_dir / filename
    if target.exists() and not force:
        return target
    tmp = target.with_suffix(target.suffix + ".part")
    with open(tmp, "wb") as fp:
        for chunk in r.iter_content(chunk_size=1 << 16):
            if chunk:
                fp.write(chunk)
    tmp.replace(target)
    return target


def _run_subprocess(cmd: list[str], *, description: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    return proc.returncode, stdout, stderr


def run_learning(
    files_dir: Path,
    *,
    learning_output: Path,
    alias_output: Path,
    alias_markdown: Path | None,
    min_alias_count: int,
    run_relation: bool = False,
    run_case_library: bool = False,
    relation_output: Path | None = None,
    relation_min_score: float = 0.35,
    relation_max_matches: int = 3,
    case_library_output: Path | None = None,
    case_library_jsonl: Path | None = None,
    case_library_max_cases: int = 0,
    case_library_max_cases_per_notice: int = 2,
) -> None:
    rc, stdout, stderr = _run_subprocess(
        [sys.executable, str(Path(__file__).resolve().parent / "batch_extract_cases.py"), str(files_dir), "--recursive", "--output", str(learning_output)],
        description="batch_extract_cases",
    )
    if stdout:
        print(stdout)
    if stderr:
        print(stderr)
    if rc != 0:
        print(f"[warn] batch_extract_cases failed with exit={rc}")
        return

    relation_output_path: Path | None = relation_output or (
        Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_relation.json"
    )

    if run_relation:
        relation_cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent / "analyze_notice_jd_relation.py"),
            "--input-dir",
            str(files_dir),
            "--output",
            str(relation_output_path),
            "--min-score",
            str(relation_min_score),
            "--max-matches",
            str(relation_max_matches),
        ]
        rc, stdout, stderr = _run_subprocess(relation_cmd, description="analyze_notice_jd_relation")
        if stdout:
            print(stdout)
        if stderr:
            print(stderr)
        if rc != 0:
            print(f"[warn] analyze_notice_jd_relation failed with exit={rc}")
            relation_output_path = None
        elif not relation_output_path.is_file():
            print(f"[warn] relation output missing after analyze: {relation_output_path}")
            relation_output_path = None

    if run_case_library and relation_output_path is not None and not relation_output_path.is_file():
        print(f"[warn] case library build skipped: relation output unavailable ({relation_output_path})")
        relation_output_path = None

    if run_case_library:
        if relation_output_path is None:
            print("[warn] case library build skipped: relation output unavailable")
        else:
            case_library_output = case_library_output or (
                Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_case_library.json"
            )
            build_cmd = [
                sys.executable,
                str(Path(__file__).resolve().parent / "build_notice_jd_case_library.py"),
                "--relation-file",
                str(relation_output_path),
                "--output",
                str(case_library_output),
                "--max-cases-per-notice",
                str(max(0, case_library_max_cases_per_notice)),
            ]
            if case_library_max_cases > 0:
                build_cmd.extend(["--max-cases", str(case_library_max_cases)])
            if case_library_jsonl is not None:
                build_cmd.extend(["--output-jsonl", str(case_library_jsonl)])
            rc, stdout, stderr = _run_subprocess(build_cmd, description="build_notice_jd_case_library")
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)
            if rc != 0:
                print(f"[warn] build_notice_jd_case_library failed with exit={rc}")

    rc, stdout, stderr = _run_subprocess(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "suggest_announcement_aliases.py"),
            str(learning_output),
            "--min-count",
            str(min_alias_count),
            "--output",
            str(alias_output),
        ],
        description="suggest_announcement_aliases",
    )
    if stdout:
        print(stdout)
    if stderr:
        print(stderr)
    if rc != 0:
        print(f"[warn] suggest_announcement_aliases failed with exit={rc}")
    

    if alias_markdown:
        rc2, stdout2, stderr2 = _run_subprocess(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "suggest_announcement_aliases.py"),
                str(learning_output),
                "--min-count",
                str(min_alias_count),
                "--output",
                str(alias_output),
                "--markdown",
                str(alias_markdown),
            ],
            description="suggest_announcement_aliases_markdown",
        )
        if stdout2:
            print(stdout2)
        if stderr2:
            print(stderr2)


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.is_file():
        return {"seen_idxs": [], "processed_files": {}}
    data = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"seen_idxs": [], "processed_files": {}}
    data.setdefault("seen_idxs", [])
    data.setdefault("processed_files", {})
    return data


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Harvest and learn from job.alio postings.")
    parser.add_argument("--output-dir", default=str(Path(__file__).resolve().parents[1] / "build" / "alio_announcements"))
    parser.add_argument("--state-path", default=str(Path(__file__).resolve().parents[1] / "build" / "alio_harvest_state.json"))
    parser.add_argument("--fresh", action="store_true", help="start with empty state and reprocess all.")
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="scan from a distant past date for legacy notices.",
    )
    parser.add_argument(
        "--ignore-state",
        action="store_true",
        help="ignore seen idx tracking and re-evaluate existing notices.",
    )
    parser.add_argument("--page-start", type=int, default=1)
    parser.add_argument("--page-end", type=int, default=0, help="0 means auto-detect from first page and cap.")
    parser.add_argument("--max-pages", type=int, default=0, help="optional hard cap for page crawling.")
    parser.add_argument("--page-size", type=int, default=10, choices=(10, 20, 30, 40, 50))
    parser.add_argument("--s-date", default="")
    parser.add_argument("--e-date", default="")
    parser.add_argument("--order", default="REG_DATE")
    parser.add_argument("--sort", default="DESC")
    parser.add_argument("--run-hours", type=float, default=0, help="continuous loop duration in hours.")
    parser.add_argument("--cycle-delay-seconds", type=float, default=15.0)
    parser.add_argument("--attachment-keywords", default="채용,직무,직무기술서", help="comma-separated keywords used to keep attachments.")
    parser.add_argument("--include-all-attachments", action="store_true")
    parser.add_argument("--max-new-files-per-cycle", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="re-download files even if already exists.")
    parser.add_argument(
        "--run-learning",
        action="store_true",
        help="run batch extraction and alias suggestion.",
    )
    parser.add_argument("--learning-output", default=str(Path(__file__).resolve().parents[1] / "build" / "announcement-learning-report.json"))
    parser.add_argument("--alias-output", default=str(Path(__file__).resolve().parents[1] / "build" / "announcement-alias-suggestions.json"))
    parser.add_argument("--alias-markdown", default="")
    parser.add_argument("--alias-min-count", type=int, default=2)
    parser.add_argument("--run-relation", action="store_true", help="derive notice->JD relations for analysis output.")
    parser.add_argument("--relation-output", default=str(Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_relation.json"))
    parser.add_argument("--relation-min-score", type=float, default=0.35)
    parser.add_argument("--relation-max-matches", type=int, default=3)
    parser.add_argument("--run-case-library", action="store_true", help="build notice->JD case library from relation output.")
    parser.add_argument("--case-library-output", default=str(Path(__file__).resolve().parents[1] / "build" / "alio_notice_jd_case_library.json"))
    parser.add_argument("--case-library-max-cases", type=int, default=0)
    parser.add_argument("--case-library-max-cases-per-notice", type=int, default=2)
    parser.add_argument("--case-library-jsonl", default="")
    parser.add_argument("--no-report", action="store_true")
    parsed = parser.parse_args(argv)
    if not parsed.s_date:
        parsed.s_date = (dt.date.today() - dt.timedelta(days=60)).strftime("%Y.%m.%d")
    if not parsed.e_date:
        parsed.e_date = dt.date.today().strftime("%Y.%m.%d")
    if parsed.full_history:
        parsed.s_date = "2010.01.01"
    return parsed


def _select_attachments(
    attachments: Iterable[ParsedAttachment],
    include_all: bool,
    keyword_set: set[str],
) -> list[ParsedAttachment]:
    if include_all:
        return list(attachments)
    selected: list[ParsedAttachment] = []
    for item in attachments:
        label_low = item.label.lower()
        if item.category == "other":
            if any(keyword.lower() in label_low for keyword in keyword_set):
                selected.append(item)
            continue
        selected.append(item)
    return selected


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    output_dir = Path(args.output_dir)
    state_path = Path(args.state_path)
    learning_output = Path(args.learning_output)
    alias_output = Path(args.alias_output)
    alias_markdown = Path(args.alias_markdown) if args.alias_markdown else None

    state = _load_state(state_path)
    if args.fresh:
        state = {"seen_idxs": [], "processed_files": {}}
    if args.ignore_state:
        state = {"seen_idxs": [], "processed_files": {}}

    seen_idxs = set(state.get("seen_idxs", []))
    keyword_set = set(k.strip() for k in args.attachment_keywords.split(",") if k.strip())

    base_params = {
        "_csrf": "",
        "idx": "",
        "s_date": args.s_date,
        "e_date": args.e_date,
        "keyword": "",
        "org_type": "",
        "org_name": "",
        "search_type": "",
        "order": args.order,
        "sort": args.sort,
        "pageSet": str(args.page_size),
    }

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    start_time = time.time()
    run_deadline = (
        start_time + args.run_hours * 3600 if args.run_hours and args.run_hours > 0 else None
    )

    cycle = 0
    total_stats = HarvestStats()

    while True:
        cycle += 1
        cycle_new_files = 0
        cycle_stats = HarvestStats()

        first_page_params = dict(base_params)
        first_page_params["pageNo"] = str(args.page_start)
        try:
            first_notices, max_page = fetch_notice_list(session, first_page_params)
        except Exception as exc:
            print(f"[error] failed to fetch page {args.page_start}: {exc}")
            break

        page_end = args.page_end if args.page_end > 0 else max_page
        if args.max_pages and args.max_pages > 0:
            page_end = min(page_end, args.page_start + args.max_pages - 1)

        all_notices = first_notices
        if page_end > args.page_start:
            for page_no in range(args.page_start + 1, page_end + 1):
                params = dict(base_params)
                params["pageNo"] = str(page_no)
                try:
                    notices, _ = fetch_notice_list(session, params)
                except Exception as exc:
                    print(f"[warn] failed page {page_no}: {exc}")
                    continue
                if not notices:
                    break
                all_notices.extend(notices)

        # process notices
        for notice in all_notices:
            cycle_stats = HarvestStats(
                total_pages=cycle_stats.total_pages,
                scanned_announcements=cycle_stats.scanned_announcements + 1,
                new_announcements=cycle_stats.new_announcements,
                downloaded_files=cycle_stats.downloaded_files,
                skipped_files=cycle_stats.skipped_files,
                failed_files=cycle_stats.failed_files,
                failed_notices=cycle_stats.failed_notices,
            )
            if notice.idx in seen_idxs:
                continue

            seen_idxs.add(notice.idx)
            cycle_stats = HarvestStats(
                total_pages=cycle_stats.total_pages,
                scanned_announcements=cycle_stats.scanned_announcements,
                new_announcements=cycle_stats.new_announcements + 1,
                downloaded_files=cycle_stats.downloaded_files,
                skipped_files=cycle_stats.skipped_files,
                failed_files=cycle_stats.failed_files,
                failed_notices=cycle_stats.failed_notices,
            )
            try:
                detail_html = fetch_detail(session, notice.idx)
            except Exception as exc:
                cycle_stats = HarvestStats(
                    total_pages=cycle_stats.total_pages,
                    scanned_announcements=cycle_stats.scanned_announcements,
                    new_announcements=cycle_stats.new_announcements,
                    downloaded_files=cycle_stats.downloaded_files,
                    skipped_files=cycle_stats.skipped_files,
                    failed_files=cycle_stats.failed_files,
                    failed_notices=cycle_stats.failed_notices + 1,
                )
                print(f"[warn] detail failed idx={notice.idx}: {exc}")
                continue

            detail_attachments = _extract_attachments(detail_html, notice.idx)
            target_attachments = _select_attachments(detail_attachments, args.include_all_attachments, keyword_set)

            notice_dir = output_dir / notice.idx
            (notice_dir / "raw").mkdir(parents=True, exist_ok=True)

            # save source html for reproducibility
            source_file = notice_dir / "raw" / f"{_slug(notice.title)}.html"
            source_file.write_text(detail_html, encoding="utf-8")

            for attachment in target_attachments:
                target = None
                try:
                    target = download_file(session, attachment.url, notice_dir, notice.idx, attachment, force=args.force)
                except Exception as exc:
                    cycle_stats = HarvestStats(
                        total_pages=cycle_stats.total_pages,
                        scanned_announcements=cycle_stats.scanned_announcements,
                        new_announcements=cycle_stats.new_announcements,
                        downloaded_files=cycle_stats.downloaded_files,
                        skipped_files=cycle_stats.skipped_files,
                        failed_files=cycle_stats.failed_files + 1,
                        failed_notices=cycle_stats.failed_notices,
                    )
                    print(f"[warn] download failed idx={notice.idx} {attachment.url}: {exc}")
                    continue

                if target is None:
                    cycle_stats = HarvestStats(
                        total_pages=cycle_stats.total_pages,
                        scanned_announcements=cycle_stats.scanned_announcements,
                        new_announcements=cycle_stats.new_announcements,
                        downloaded_files=cycle_stats.downloaded_files,
                        skipped_files=cycle_stats.skipped_files + 1,
                        failed_files=cycle_stats.failed_files,
                        failed_notices=cycle_stats.failed_notices,
                    )
                    continue
                cycle_new_files += 1
                cycle_stats = HarvestStats(
                    total_pages=cycle_stats.total_pages,
                    scanned_announcements=cycle_stats.scanned_announcements,
                    new_announcements=cycle_stats.new_announcements,
                    downloaded_files=cycle_stats.downloaded_files + 1,
                    skipped_files=cycle_stats.skipped_files,
                    failed_files=cycle_stats.failed_files,
                    failed_notices=cycle_stats.failed_notices,
                )
                state.setdefault("processed_files", {})[attachment.url] = str(target)

            if args.max_new_files_per_cycle and cycle_new_files >= args.max_new_files_per_cycle:
                break

        total_stats = HarvestStats(
            total_pages=total_stats.total_pages + (page_end - args.page_start + 1),
            scanned_announcements=total_stats.scanned_announcements + cycle_stats.scanned_announcements,
            new_announcements=total_stats.new_announcements + cycle_stats.new_announcements,
            downloaded_files=total_stats.downloaded_files + cycle_stats.downloaded_files,
            skipped_files=total_stats.skipped_files + cycle_stats.skipped_files,
            failed_files=total_stats.failed_files + cycle_stats.failed_files,
            failed_notices=total_stats.failed_notices + cycle_stats.failed_notices,
        )

        state["seen_idxs"] = sorted(seen_idxs)
        state["last_cycle"] = dt.datetime.now().isoformat(timespec="seconds")
        _save_state(state_path, state)

        print(
            "cycle="
            f"{cycle} scanned={cycle_stats.scanned_announcements} new={cycle_stats.new_announcements} "
            f"downloaded={cycle_stats.downloaded_files} skipped={cycle_stats.skipped_files} "
            f"failed={cycle_stats.failed_files}"
        )

        if (args.run_learning or args.run_relation or args.run_case_library) and cycle_stats.downloaded_files > 0:
            run_learning(
                output_dir,
                learning_output=learning_output,
                alias_output=alias_output,
                alias_markdown=alias_markdown,
                min_alias_count=args.alias_min_count,
                run_relation=args.run_relation or args.run_case_library,
                run_case_library=args.run_case_library,
                relation_output=Path(args.relation_output),
                relation_min_score=args.relation_min_score,
                relation_max_matches=args.relation_max_matches,
                case_library_output=Path(args.case_library_output),
                case_library_jsonl=Path(args.case_library_jsonl) if args.case_library_jsonl else None,
                case_library_max_cases=args.case_library_max_cases,
                case_library_max_cases_per_notice=args.case_library_max_cases_per_notice,
            )

        if run_deadline is None:
            break
        if cycle_new_files == 0 and cycle_stats.failed_notices == 0:
            # no new data in this cycle; avoid busy loops
            if args.run_hours and (time.time() >= run_deadline):
                break
            time.sleep(args.cycle_delay_seconds)
            if time.time() >= run_deadline:
                break
            # continue polling
            continue
        if time.time() >= run_deadline:
            break
        time.sleep(args.cycle_delay_seconds)

    if not args.no_report:
        summary = {
            "summary": {
                "cycles": cycle,
                "total_pages_scanned": total_stats.total_pages,
                "announcements_scanned": total_stats.scanned_announcements,
                "announcements_new": total_stats.new_announcements,
                "files_downloaded": total_stats.downloaded_files,
                "files_skipped": total_stats.skipped_files,
                "files_failed": total_stats.failed_files,
                "notices_failed": total_stats.failed_notices,
            },
            "seen_count": len(seen_idxs),
            "output_dir": str(output_dir),
            "state_path": str(state_path),
        }
        summary_path = output_dir / "harvest-report.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))

    print(f"done output_dir={output_dir} state={state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




