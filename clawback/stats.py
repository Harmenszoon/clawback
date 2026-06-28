"""Aggregate a run's `index.jsonl` into a savings and hit-rate summary.

Usage:
    python -m clawback.stats                 # latest run under logs/
    python -m clawback.stats <run-dir>       # a specific run directory
    python -m clawback.stats <index.jsonl>   # an explicit index file

Reads only the one-line-per-request index, so it is cheap and works on a live
run. The summary answers what the per-request files can't show at a glance:

  * how often each transform and short-circuit fired — a hit rate falling to
    zero while the traffic still looks the same is the drift signal that a
    detection needs updating;
  * how much was removed or never sent, in bytes and approximate tokens —
    the per-run version of the README's receipt, from your own traffic;
  * what the run actually billed upstream (summed `usage`).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .config import LOGS_ROOT

# The README's working approximation for prose-heavy JSON payloads.
_CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------------------
# Aggregation (pure, testable)
# ---------------------------------------------------------------------------


def summarize_index(lines: list[str]) -> dict[str, Any]:
    """Fold index.jsonl lines into one summary dict. Malformed lines are skipped."""
    requests = 0
    statuses: Counter[str] = Counter()
    shortcuts: Counter[str] = Counter()
    transforms: Counter[str] = Counter()
    usage_totals: Counter[str] = Counter()
    bytes_removed = 0
    bytes_unsent = 0
    errors = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue

        requests += 1
        statuses[str(rec.get("status"))] += 1
        if rec.get("error"):
            errors += 1

        short_circuited = rec.get("short_circuited")
        if isinstance(short_circuited, str):
            shortcuts[short_circuited] += 1
        if isinstance(rec.get("bytes_unsent"), int):
            bytes_unsent += rec["bytes_unsent"]

        for name in rec.get("transforms_applied") or []:
            if isinstance(name, str):
                transforms[name] += 1
        if isinstance(rec.get("bytes_removed"), int):
            bytes_removed += rec["bytes_removed"]

        usage = rec.get("usage")
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(value, int):
                    usage_totals[key] += value

    return {
        "requests": requests,
        "statuses": dict(statuses),
        "errors": errors,
        "shortcuts": dict(shortcuts),
        "transforms": dict(transforms),
        "bytes_removed": bytes_removed,
        "bytes_unsent": bytes_unsent,
        "usage_totals": dict(usage_totals),
    }


def format_summary(summary: dict[str, Any], source: str = "") -> str:
    """Render a summary dict as the human-readable report."""
    out: list[str] = []
    if source:
        out.append(f"Run: {source}")

    # Console output stays ASCII: Windows consoles often run cp1252/cp437,
    # where fancier glyphs raise UnicodeEncodeError instead of printing.
    statuses = ", ".join(f"{status} x{n}" for status, n in sorted(summary["statuses"].items()))
    line = f"Requests: {summary['requests']}"
    if statuses:
        line += f"  ({statuses})"
    if summary["errors"]:
        line += f"  — {summary['errors']} with errors"
    out.append(line)

    if summary["shortcuts"]:
        out.append("")
        out.append("Short-circuited locally (never sent upstream):")
        out.extend(_counter_lines(summary["shortcuts"]))
        if summary["bytes_unsent"]:
            out.append(f"  {_fmt_bytes_tokens(summary['bytes_unsent'])} of requests unsent")

    if summary["transforms"]:
        out.append("")
        out.append("Transforms applied to forwarded requests:")
        out.extend(_counter_lines(summary["transforms"]))
        if summary["bytes_removed"]:
            out.append(f"  {_fmt_bytes_tokens(summary['bytes_removed'])} removed")

    if summary["usage_totals"]:
        out.append("")
        out.append("Billed usage (summed upstream `usage`):")
        width = max(len(k) for k in summary["usage_totals"])
        for key in sorted(summary["usage_totals"]):
            out.append(f"  {key.ljust(width)}  {summary['usage_totals'][key]:>12,}")

    out.append("")
    out.append(
        "Drift check: a transform or short-circuit whose count falls to zero while"
        " Claude Code traffic continues is the signal that its detection needs"
        " updating - open the run's .md files to see what passed through."
    )
    return "\n".join(out)


def _counter_lines(counts: dict[str, int]) -> list[str]:
    width = max(len(name) for name in counts)
    return [f"  {name.ljust(width)}  {counts[name]:>5}" for name in sorted(counts, key=counts.get, reverse=True)]


def _fmt_bytes_tokens(n: int) -> str:
    return f"~{n:,} bytes (~{n // _CHARS_PER_TOKEN:,} tokens at ~{_CHARS_PER_TOKEN} chars/token)"


# ---------------------------------------------------------------------------
# CLI entry: `python -m clawback.stats [path]`
# ---------------------------------------------------------------------------


def _resolve_index(arg: str | None) -> Path | None:
    """Find the index.jsonl to read: explicit file, run dir, or the latest run."""
    if arg:
        p = Path(arg)
        if p.is_file():
            return p
        if p.is_dir():
            candidate = p / "index.jsonl"
            return candidate if candidate.is_file() else None
        return None
    runs = sorted(d for d in LOGS_ROOT.glob("*") if (d / "index.jsonl").is_file())
    return (runs[-1] / "index.jsonl") if runs else None


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("usage: python -m clawback.stats [run-dir-or-index.jsonl]", file=sys.stderr)
        return 2
    index_path = _resolve_index(argv[0] if argv else None)
    if index_path is None:
        target = argv[0] if argv else f"{LOGS_ROOT} (no runs with an index.jsonl)"
        print(f"no index.jsonl found at: {target}", file=sys.stderr)
        return 1
    lines = index_path.read_text(encoding="utf-8").splitlines()
    summary = summarize_index(lines)
    print(format_summary(summary, source=str(index_path.parent)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
