"""Per-run logging.

Each proxy startup creates a `logs/<iso-timestamp>/` directory. For every
request the proxy handles, three artifacts are written:

    NNN_<path-slug>.json    full structured request + response record
    NNN_<path-slug>.md      paired human-readable rendering (see render.py)
    index.jsonl             one-line summary appended per request

The JSON files are the lossless source of truth; the markdown is for
top-to-bottom review. The index gives a session-level overview that is
cheap to scan or grep.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import LOGS_ROOT, SENSITIVE_HEADERS
from .render import render as render_markdown


class RunLogger:
    """Writes per-request log files into a single run directory."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.index_path = run_dir / "index.jsonl"
        self._seq = 0
        self._seq_lock = asyncio.Lock()

    # ------------------------------------------------------------------ API

    @classmethod
    def create(cls) -> RunLogger:
        """Create a fresh run directory tagged with the current UTC timestamp."""
        LOGS_ROOT.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
        run_dir = LOGS_ROOT / ts
        # Disambiguate if the same second is hit on restart.
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = LOGS_ROOT / f"{ts}-{suffix}"
        run_dir.mkdir()
        return cls(run_dir)

    async def record(
        self,
        *,
        started_at: datetime,
        elapsed_s: float,
        method: str,
        path: str,
        request_headers: dict[str, str],
        request_body: Any,
        response_status: int,
        response_headers: dict[str, str],
        response_body: Any,
        error: str | None = None,
        short_circuited: str | None = None,
        transforms_applied: list[str] | None = None,
        bytes_removed: int | None = None,
        bytes_unsent: int | None = None,
    ) -> None:
        """Write one request/response record to disk (non-blocking)."""
        async with self._seq_lock:
            self._seq += 1
            seq = self._seq

        slug = _slugify_path(path)
        record_path = self.run_dir / f"{seq:03d}_{slug}.json"

        record = {
            "seq": seq,
            "ts": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elapsed_s": round(elapsed_s, 3),
            "method": method,
            "path": path,
            "request": {
                "headers": _sanitize_headers(request_headers),
                "body": _sanitize_request_body(request_body),
            },
            "response": {
                "status": response_status,
                "headers": _sanitize_headers(response_headers),
                "body": response_body,
            },
        }
        if error:
            record["error"] = error
        if short_circuited:
            record["short_circuited"] = short_circuited
        if transforms_applied:
            record["transforms_applied"] = transforms_applied
        if bytes_removed is not None:
            record["bytes_removed"] = bytes_removed
        if bytes_unsent is not None:
            record["bytes_unsent"] = bytes_unsent

        index_entry = _summarize(record)

        md_path = record_path.with_suffix(".md")
        await asyncio.to_thread(
            _write_record,
            record_path,
            record,
            md_path,
            self.index_path,
            index_entry,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify_path(path: str) -> str:
    """Turn a request path into a safe filename fragment.

    Drops query string, strips a leading /v1/, replaces remaining slashes
    with underscores, and falls back to 'root' for an empty path.
    """
    p = path.split("?", 1)[0].lstrip("/")
    if p.startswith("v1/"):
        p = p[3:]
    p = p.replace("/", "_") or "root"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in p)[:60]


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Replace sensitive header values with a redaction marker."""
    return {k: ("<redacted>" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}


def _sanitize_request_body(body: Any) -> Any:
    """Return a log-safe copy of the request body with telemetry redacted.

    `metadata.user_id` is a JSON-string blob carrying the device id, account
    uuid, and session id. It is forwarded upstream unchanged (billing and
    rate-limit reconciliation depend on it) but redacted in the on-disk log,
    which is the only copy likely to be pasted, zipped, or shared. The whole
    value is replaced rather than parsed: the device and account ids are more
    durable identifiers than the session id, so a half-redaction would be
    little better than none.

    Copy-on-write: only the top-level body and its `metadata` dict are copied,
    so the parsed body the server forwarded (and any aliased subtrees) is never
    mutated. Anything that isn't the expected shape is returned untouched.
    """
    if not isinstance(body, dict):
        return body
    metadata = body.get("metadata")
    if not isinstance(metadata, dict) or "user_id" not in metadata:
        return body
    return {**body, "metadata": {**metadata, "user_id": "<redacted>"}}


def _summarize(record: dict) -> dict:
    """Extract a compact one-line index entry from a full record."""
    req_body = record["request"]["body"] if isinstance(record["request"]["body"], dict) else {}
    resp_body = record["response"]["body"] if isinstance(record["response"]["body"], dict) else {}
    usage = resp_body.get("usage") if isinstance(resp_body, dict) else None
    return {
        "seq": record["seq"],
        "ts": record["ts"],
        "elapsed_s": record["elapsed_s"],
        "method": record["method"],
        "path": record["path"],
        "status": record["response"]["status"],
        "model": req_body.get("model"),
        "stream": bool(req_body.get("stream")),
        "stop_reason": resp_body.get("stop_reason") if isinstance(resp_body, dict) else None,
        "usage": usage,
        **({"error": record["error"]} if "error" in record else {}),
        **({"short_circuited": record["short_circuited"]} if "short_circuited" in record else {}),
        **({"transforms_applied": record["transforms_applied"]} if "transforms_applied" in record else {}),
        **({"bytes_removed": record["bytes_removed"]} if "bytes_removed" in record else {}),
        **({"bytes_unsent": record["bytes_unsent"]} if "bytes_unsent" in record else {}),
    }


def _write_record(
    record_path: Path,
    record: dict,
    md_path: Path,
    index_path: Path,
    index_entry: dict,
) -> None:
    """Synchronous disk I/O — runs in a worker thread via asyncio.to_thread."""
    record_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=_json_fallback) + "\n",
        encoding="utf-8",
    )
    try:
        md_path.write_text(render_markdown(record), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — rendering must never break logging
        md_path.write_text(f"# Render failed\n\n{type(exc).__name__}: {exc}\n", encoding="utf-8")
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(index_entry, ensure_ascii=False, default=_json_fallback) + "\n")


def _json_fallback(obj: Any) -> str:
    """Last-resort encoder for anything json can't serialize natively."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    return repr(obj)
