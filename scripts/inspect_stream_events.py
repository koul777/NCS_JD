"""Summarize `claude -p --output-format stream-json` events.

The progress UI depends on knowing which event carries a tool call and which
carries its result, so this prints the shape of a captured stream rather than
leaving the adapter to guess at it.  Capture a sample first:

    claude -p "..." --output-format stream-json --verbose > build/stream-sample.jsonl
    python scripts/inspect_stream_events.py build/stream-sample.jsonl
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = _PROJECT_ROOT / "build" / "stream-sample.jsonl"


def describe(event: dict, index: int) -> str:
    parts = [f"{index:2} type={event.get('type')}"]
    if event.get("subtype"):
        parts.append(f"subtype={event['subtype']}")

    message = event.get("message")
    if isinstance(message, dict):
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                arguments = json.dumps(block.get("input"), ensure_ascii=False)[:90]
                parts.append(f"TOOL_USE name={block.get('name')} input={arguments}")
            elif kind == "tool_result":
                content = block.get("content")
                size = len(json.dumps(content, ensure_ascii=False)) if content is not None else 0
                parts.append(f"TOOL_RESULT bytes={size} is_error={block.get('is_error')}")
            elif kind == "text":
                parts.append(f"TEXT {block.get('text', '')[:70]!r}")
            else:
                parts.append(f"{kind}")

    for key in ("num_turns", "duration_ms", "total_cost_usd", "is_error"):
        if key in event:
            parts.append(f"{key}={event[key]}")
    return "  ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the shape of a captured Claude CLI stream.")
    parser.add_argument(
        "sample",
        nargs="?",
        type=Path,
        default=DEFAULT_SAMPLE,
        help=f"stream-json capture to read (default: {DEFAULT_SAMPLE})",
    )
    path = parser.parse_args(argv).sample
    if not path.is_file():
        print(f"stream capture not found: {path}")
        print("Record one with `claude -p ... --output-format stream-json --verbose`.")
        return 1

    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line.startswith("{"):
            continue
        print(describe(json.loads(line), index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
