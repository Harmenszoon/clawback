"""Render a captured request/response record into a readable markdown view.

The JSON log is the source of truth; the markdown is a sibling view designed
for top-to-bottom review without digging through structured data. Each
record file (`NNN_*.json`) is paired with a matching `NNN_*.md`.

Sections, in order: meta · request headers · request body summary · system
blocks · tool index + per-tool detail · messages · response usage + content.

Run retroactively as: `python -m claude_proxy.render <path-or-dir>`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# Long string content goes inside fences of this length so nested triple
# backticks in tool / prompt text do not terminate the fence early.
_FENCE = "````"


# ---------------------------------------------------------------------------
# Top-level renderer
# ---------------------------------------------------------------------------

def render(record: dict) -> str:
    """Return a markdown rendering of a single request/response record."""
    sections: list[str] = []
    sections.append(_render_header(record))
    sections.append(_render_request_meta(record))
    sections.append(_render_system(record))
    sections.append(_render_tools(record))
    sections.append(_render_messages(record))
    sections.append(_render_response(record))
    return "\n\n".join(s for s in sections if s).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _render_header(rec: dict) -> str:
    seq = rec.get("seq", "?")
    method = rec.get("method", "?")
    path = rec.get("path", "?")
    ts = rec.get("ts", "?")
    elapsed = rec.get("elapsed_s", "?")
    status = rec.get("response", {}).get("status", "?")
    err = rec.get("error")
    short_circuited = rec.get("short_circuited")
    transforms_applied = rec.get("transforms_applied") or []
    lines = [
        f"# Request {seq:>03} — {method} {path}",
        "",
        f"`{ts}` · `elapsed {elapsed}s` · `status {status}`",
    ]
    if short_circuited:
        lines.append(f"\n> **Short-circuited locally — never sent upstream.** Reason: `{short_circuited}`")
    if transforms_applied:
        joined = ", ".join(f"`{t}`" for t in transforms_applied)
        lines.append(f"\n> **Request mutated before forwarding.** Transforms: {joined}")
    if err:
        lines.append(f"\n> **Error:** {err}")
    return "\n".join(lines)


def _render_request_meta(rec: dict) -> str:
    req = rec.get("request", {})
    headers = req.get("headers") or {}
    body = req.get("body") if isinstance(req.get("body"), dict) else None

    out = ["## Request"]

    out.append("\n### Headers\n")
    out.append(_fmt_headers(headers))

    if body is None:
        raw = req.get("body", "")
        if raw:
            out.append("\n### Body (non-JSON)\n")
            out.append(_fenced(str(raw)))
        return "\n".join(out)

    out.append("\n### Body summary\n")
    out.append(_fmt_body_summary(body))
    return "\n".join(out)


def _render_system(rec: dict) -> str:
    body = rec.get("request", {}).get("body")
    if not isinstance(body, dict):
        return ""
    system = body.get("system")
    if not isinstance(system, list) or not system:
        return ""

    total_chars = sum(_block_text_len(b) for b in system)
    out = [f"## System prompt — {len(system)} blocks, {total_chars:,} chars total"]

    for i, block in enumerate(system, 1):
        out.append("")
        out.append(_fmt_text_block(block, position=f"{i} of {len(system)}"))
    return "\n".join(out)


def _render_tools(rec: dict) -> str:
    body = rec.get("request", {}).get("body")
    if not isinstance(body, dict):
        return ""
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return ""

    total_chars = sum(len(t.get("description") or "") for t in tools if isinstance(t, dict))
    out = [f"## Tools — {len(tools)} definitions, {total_chars:,} chars of descriptions"]

    out.append("\n### Index\n")
    out.append("| # | Name | Description chars | Params |")
    out.append("|---|------|-------------------|--------|")
    for i, tool in enumerate(tools, 1):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name", "?")
        desc_len = len(tool.get("description") or "")
        params = list((tool.get("input_schema") or {}).get("properties", {}).keys())
        params_str = ", ".join(params) if params else "—"
        out.append(f"| {i:>02} | `{name}` | {desc_len:,} | {params_str} |")

    out.append("\n### Detail\n")
    for i, tool in enumerate(tools, 1):
        if not isinstance(tool, dict):
            continue
        out.append(_fmt_tool_detail(i, tool))
        out.append("")
    return "\n".join(out)


def _render_messages(rec: dict) -> str:
    body = rec.get("request", {}).get("body")
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return ""

    out = [f"## Messages — {len(messages)}"]
    for m_idx, msg in enumerate(messages, 1):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            content = []
        out.append("")
        out.append(f"### Message {m_idx} — role: `{role}`, {len(content)} content block(s)")
        for b_idx, block in enumerate(content, 1):
            out.append("")
            out.append(_fmt_message_block(b_idx, block))
    return "\n".join(out)


def _render_response(rec: dict) -> str:
    resp = rec.get("response", {})
    status = resp.get("status", "?")
    body = resp.get("body")
    headers = resp.get("headers") or {}

    out = [f"## Response — status {status}"]

    out.append("\n### Headers\n")
    out.append(_fmt_headers(headers))

    if isinstance(body, dict):
        stop = body.get("stop_reason", "—")
        usage = body.get("usage") or {}
        out.append(f"\n**stop_reason:** `{stop}`")
        if usage:
            out.append("\n**Usage:**")
            for line in _fmt_usage(usage).splitlines():
                out.append(line)
        content = body.get("content")
        if isinstance(content, list) and content:
            out.append(f"\n### Content — {len(content)} block(s)")
            for b_idx, block in enumerate(content, 1):
                out.append("")
                out.append(_fmt_message_block(b_idx, block))
        # Out-of-band SSE events captured by the assembler (errors / unknown
        # event shapes). Surfaced here so an error-only stream is visible
        # rather than reading as an empty success.
        stream_errors = body.get("errors")
        if isinstance(stream_errors, list) and stream_errors:
            out.append("\n**Stream errors:**")
            for e in stream_errors:
                if isinstance(e, dict):
                    out.append(f"- type: `{e.get('type', '?')}` — {e.get('message', e)}")
                else:
                    out.append(f"- {e}")
        unknown_events = body.get("unknown_event_types")
        if isinstance(unknown_events, dict) and unknown_events:
            joined = ", ".join(f"`{k}` ×{v}" for k, v in unknown_events.items())
            out.append(f"\n**Unrecognized SSE events:** {joined}")
        # Error responses
        if body.get("type") == "error":
            err = body.get("error", {})
            out.append("\n**Error:**")
            out.append(f"- type: `{err.get('type', '?')}`")
            out.append(f"- message: {err.get('message', '?')}")
    elif body:
        out.append("\n### Body (non-JSON)\n")
        out.append(_fenced(str(body)))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Block / tool / param formatting
# ---------------------------------------------------------------------------

def _fmt_text_block(block: dict, position: str) -> str:
    if not isinstance(block, dict):
        return _fenced(str(block))
    btype = block.get("type", "?")
    text = block.get("text", "") if btype == "text" else ""
    cache = _fmt_cache_control(block.get("cache_control"))
    flags = []
    if "<system-reminder>" in text:
        flags.append("**contains `<system-reminder>`**")
    flag_str = (" · " + " · ".join(flags)) if flags else ""

    header = f"#### Block {position} — type: `{btype}`, {len(text):,} chars, cache: {cache}{flag_str}"
    if btype != "text":
        # For non-text blocks (rare in system), dump the whole thing.
        return header + "\n\n" + _fenced(json.dumps(block, indent=2, ensure_ascii=False))
    return header + "\n\n" + _fenced(text)


def _fmt_message_block(idx: int, block: Any) -> str:
    if not isinstance(block, dict):
        return f"#### Block {idx}\n\n" + _fenced(str(block))
    btype = block.get("type", "?")
    cache = _fmt_cache_control(block.get("cache_control"))

    if btype == "text":
        text = block.get("text", "")
        flags = []
        if "<system-reminder>" in text:
            flags.append("**contains `<system-reminder>`**")
        flag_str = (" · " + " · ".join(flags)) if flags else ""
        return (
            f"#### Block {idx} — type: `text`, {len(text):,} chars, cache: {cache}{flag_str}\n\n"
            + _fenced(text)
        )

    if btype == "thinking":
        thinking = block.get("thinking", "")
        return (
            f"#### Block {idx} — type: `thinking`, {len(thinking):,} chars\n\n"
            + _fenced(thinking)
        )

    if btype == "tool_use":
        name = block.get("name", "?")
        tool_id = block.get("id", "?")
        inp = block.get("input", {})
        header = f"#### Block {idx} — type: `tool_use`, name: `{name}`, id: `{tool_id}`"
        body = _fenced(json.dumps(inp, indent=2, ensure_ascii=False))
        return header + "\n\n" + body

    if btype == "tool_result":
        tool_use_id = block.get("tool_use_id", "?")
        is_err = block.get("is_error")
        content = block.get("content")
        header = (
            f"#### Block {idx} — type: `tool_result`, tool_use_id: `{tool_use_id}`"
            + (" *(error)*" if is_err else "")
        )
        if isinstance(content, str):
            return header + "\n\n" + _fenced(content)
        return header + "\n\n" + _fenced(json.dumps(content, indent=2, ensure_ascii=False))

    # Fallback for unknown block types
    return f"#### Block {idx} — type: `{btype}`\n\n" + _fenced(json.dumps(block, indent=2, ensure_ascii=False))


def _fmt_tool_detail(idx: int, tool: dict) -> str:
    name = tool.get("name", "?")
    desc = tool.get("description") or ""
    schema = tool.get("input_schema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    lines = [
        f"#### {idx:>02}. `{name}` · {len(desc):,} chars · {len(props)} param(s)",
        "",
        "**Description:**",
        "",
        _fenced(desc) if desc else "_(none)_",
    ]
    if props:
        lines.append("")
        lines.append("**Parameters:**")
        lines.append("")
        for pname, pdef in props.items():
            lines.append(_fmt_param(pname, pdef or {}, pname in required))
    return "\n".join(lines)


def _fmt_param(name: str, schema: dict, required: bool) -> str:
    parts: list[str] = []
    ptype = schema.get("type", "any")
    parts.append(f"`{ptype}`")
    if required:
        parts.append("**required**")
    if "enum" in schema:
        enum = schema["enum"]
        parts.append(f"enum: {', '.join(repr(v) for v in enum)}")
    if "default" in schema:
        parts.append(f"default: `{schema['default']!r}`")
    type_info = " · ".join(parts)

    desc = (schema.get("description") or "").strip()
    if desc:
        # Inline if short, block-quoted if long
        if len(desc) <= 100 and "\n" not in desc:
            return f"- **{name}** *({type_info})* — {desc}"
        return f"- **{name}** *({type_info})*\n  > " + desc.replace("\n", "\n  > ")
    return f"- **{name}** *({type_info})*"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _fmt_headers(headers: dict) -> str:
    if not headers:
        return "_(none)_"
    width = max((len(k) for k in headers), default=0)
    lines = [f"{k.ljust(width)}  {v}" for k, v in headers.items()]
    return _fenced("\n".join(lines))


def _fmt_body_summary(body: dict) -> str:
    keys_of_interest = ["model", "stream", "max_tokens", "system", "messages", "tools", "metadata"]
    lines: list[str] = []
    for key in keys_of_interest:
        if key not in body:
            continue
        value = body[key]
        if key == "system" and isinstance(value, list):
            lines.append(f"- **system**: {len(value)} block(s)")
        elif key == "messages" and isinstance(value, list):
            lines.append(f"- **messages**: {len(value)}")
        elif key == "tools" and isinstance(value, list):
            lines.append(f"- **tools**: {len(value)}")
        elif key == "metadata" and isinstance(value, dict):
            lines.append(f"- **metadata**: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"- **{key}**: `{value}`")
    extras = [k for k in body if k not in keys_of_interest]
    if extras:
        lines.append("- **other keys**: " + ", ".join(f"`{k}`" for k in extras))
    return "\n".join(lines)


def _fmt_cache_control(cc: Any) -> str:
    if not isinstance(cc, dict):
        return "none"
    ttype = cc.get("type", "?")
    ttl = cc.get("ttl")
    return f"{ttype}/{ttl}" if ttl else str(ttype)


def _fmt_usage(usage: dict) -> str:
    fields = [
        ("input_tokens", "input"),
        ("cache_creation_input_tokens", "cache_create"),
        ("cache_read_input_tokens", "cache_read"),
        ("output_tokens", "output"),
    ]
    lines: list[str] = []
    for key, label in fields:
        if key in usage:
            lines.append(f"- {label}: {usage[key]:,}")
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        for k, v in cc.items():
            lines.append(f"- {k}: {v:,}")
    return "\n".join(lines) if lines else "_(none)_"


def _block_text_len(block: Any) -> int:
    if isinstance(block, dict) and block.get("type") == "text":
        return len(block.get("text", ""))
    return 0


def _fenced(text: str) -> str:
    """Wrap text in a long-backtick fence so nested ``` cannot escape it."""
    return f"{_FENCE}\n{text}\n{_FENCE}"


# ---------------------------------------------------------------------------
# CLI entry: `python -m claude_proxy.render <path>`
# ---------------------------------------------------------------------------

def _render_file(json_path: Path) -> Path:
    record = json.loads(json_path.read_text(encoding="utf-8"))
    md_path = json_path.with_suffix(".md")
    md_path.write_text(render(record), encoding="utf-8")
    return md_path


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m claude_proxy.render <path-or-dir> [...]", file=sys.stderr)
        return 2
    targets: list[Path] = []
    for arg in argv:
        p = Path(arg)
        if p.is_dir():
            targets.extend(sorted(p.glob("*.json")))
        elif p.is_file():
            targets.append(p)
        else:
            print(f"skip (not found): {arg}", file=sys.stderr)
    for path in targets:
        if path.name == "index.jsonl":
            continue
        try:
            out = _render_file(path)
            print(f"wrote {out}")
        except Exception as exc:
            print(f"failed {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
