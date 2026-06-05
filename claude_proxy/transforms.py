"""The transform pipeline applied to each request before it is forwarded.

There are two entry points:

  * `maybe_shortcut(body)` — returns a synthetic response if the request can
    be answered locally without an upstream call, else `None`. The handler
    uses this to bypass the network entirely for known auxiliary calls.

  * `apply_request_transforms(body)` — returns `(modified_body, applied)`.
    The handler re-serializes the modified body and forwards it; `applied`
    is the list of transform names that fired, recorded in the log.

# The transforms

  Short-circuit:
    title-gen   Claude Code asks for `{"title": "..."}` via a json_schema
                output config. We return a synthetic `CONVERSATION_<hex>`
                title. The CLI uses session titles only to populate its
                `/resume` picker, so an opaque-but-unique label is fine.
    recap       When the user steps away and returns, Claude Code injects
                a fixed "recap in under 40 words" prompt. We return a
                static "Continuing." instead of paying Opus to summarize.

  Mutation:
    reduce-main-system        The main conversation carries a ~27K
                              behavioral prompt cached for an hour. We
                              replace its content with the three lines of
                              operational env info that matter (cwd,
                              platform, OS version) plus one behavioral
                              directive about tool selection.
    strip-system-reminders    Claude Code injects reminders three ways: as
                              stand-alone `<system-reminder>` text blocks, as
                              inline appendages inside `tool_result.content`
                              strings, and (since the mid-conversation-system
                              beta) as stand-alone `role:"system"` messages in
                              the messages array. The first two are removed
                              wholesale; the third is trimmed to drop only
                              recognized noise — transient nudges and guidance
                              for tools that aren't enabled — keying off the
                              post-filter tool list and passing anything
                              unrecognized through untouched. All decisions are
                              deterministic per turn so the cache prefix holds.
    filter-tools              Strips tools whose name is set to `false` in
                              `tools.json`. New (unseen) tools are added to
                              the file with `true` and persisted, so the
                              user always has visibility into what's
                              available. See `tool_filter.py`.

# Detection strategy

Every transform uses **structural** signals only — schema shape, Markdown
heading landmarks, tag boundaries, named tool entries — not prose wording.
Rewording does not break detection. Where multiple signals are available,
we require all of them so a single rewording or restructuring on
Anthropic's side cannot trigger a false positive.

# Fail-open behavior

If any transform's detection misses, the request flows through unchanged
and the un-stripped content surfaces in the log on the very next turn.
That is the signal that something upstream has shifted shape. The proxy
never silently corrupts a request: the worst case for a missed transform
is paying full price for the request we would have shrunk.

# Mutation safety

Mutating transforms are copy-on-write throughout: a transform that wants
to modify a block builds a new dict and a new parent list. The caller's
record of the unmodified request body is preserved for logging.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Any

from .tool_filter import filter_tools

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def maybe_shortcut(body: Any) -> tuple[dict, str] | None:
    """Return (synthetic_response_dict, reason) if the request should be
    short-circuited, else None to forward upstream.

    The synthetic response is shaped like a non-streaming Anthropic
    message response. The caller decides whether to emit it as JSON or
    serialize it via `to_sse_bytes` based on the request's `stream` flag.
    """
    if not isinstance(body, dict):
        return None
    if _is_title_gen(body):
        return _synthesize_title_response(body), "title-gen"
    if _is_recap_request(body):
        return _synthesize_recap_response(body), "recap"
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _is_title_gen(body: dict) -> bool:
    """True iff the request is asking for a JSON title back.

    Stable across changes to prompt wording, model, effort, and message
    shape — depends only on the call's declared output schema.
    """
    fmt = (body.get("output_config") or {}).get("format") or {}
    if fmt.get("type") != "json_schema":
        return False
    schema = fmt.get("schema") or {}
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    return "title" in required and "title" in properties


# Claude Code injects this exact prompt as a string-content user message
# when the user returns to the CLI after stepping away. It asks the model
# to produce a ~40-word recap of the conversation. We don't need it — the
# user can scroll up — and it costs Opus tokens each time.
_RECAP_PROMPT_PREFIX = "The user stepped away and is coming back. Recap"


def _is_recap_request(body: dict) -> bool:
    """True iff the request is Claude Code's 'user is returning' recap prompt.

    Detection looks at the last user message only: must be string content
    (not a list of blocks) and begin with the literal prefix Claude Code
    uses. Two structural signals together — string content + exact prefix —
    so a real user typing a similar sentence wouldn't be caught.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return False
    content = last.get("content")
    if not isinstance(content, str):
        return False
    return content.startswith(_RECAP_PROMPT_PREFIX)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def _synthesize_title_response(body: dict) -> dict:
    """Build a non-streaming response containing `{"title": "CONVERSATION_<hex>"}`."""
    title = f"CONVERSATION_{secrets.token_hex(4)}"
    payload = json.dumps({"title": title}, ensure_ascii=False)
    return {
        "id": f"msg_synth_{secrets.token_hex(8)}",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "synthetic"),
        "content": [{"type": "text", "text": payload}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": max(1, len(payload) // 4),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _synthesize_recap_response(body: dict) -> dict:
    """Build a minimal text response for a recap request.

    Returns a one-word "Continuing." — valid non-empty text content that
    won't break any CLI parser, costs nothing upstream, and signals to the
    user that they're back in the session without claiming any specific
    facts about the conversation state.
    """
    text = "Continuing."
    return {
        "id": f"msg_synth_{secrets.token_hex(8)}",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "synthetic"),
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 2,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


# ---------------------------------------------------------------------------
# SSE serialization
# ---------------------------------------------------------------------------


def to_sse_bytes(response: dict) -> bytes:
    """Serialize a response dict as Anthropic-style SSE event bytes.

    Emits the minimal sequence Claude Code expects for a single-text-block
    streaming response: message_start, content_block_start/delta/stop,
    message_delta, message_stop, then `data: [DONE]`.
    """
    parts: list[bytes] = []

    parts.append(
        _event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": response["id"],
                    "type": response["type"],
                    "role": response["role"],
                    "model": response["model"],
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {k: v for k, v in response.get("usage", {}).items() if k != "output_tokens"},
                },
            },
        )
    )

    for idx, block in enumerate(response.get("content", [])):
        if block.get("type") != "text":
            continue
        text = block.get("text", "")
        parts.append(
            _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
        if text:
            parts.append(
                _event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )
        parts.append(
            _event(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": idx,
                },
            )
        )

    parts.append(
        _event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": response.get("stop_reason"),
                    "stop_sequence": response.get("stop_sequence"),
                },
                "usage": {"output_tokens": response.get("usage", {}).get("output_tokens", 0)},
            },
        )
    )

    parts.append(_event("message_stop", {"type": "message_stop"}))
    parts.append(b"data: [DONE]\n\n")
    return b"".join(parts)


def _event(name: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n".encode()


# ---------------------------------------------------------------------------
# Request-mutation transforms
# ---------------------------------------------------------------------------


def apply_request_transforms(body: Any) -> tuple[Any, list[str]]:
    """Apply mutations to a request body.

    Returns (possibly-modified body, list of transform names that ran).
    An empty list means the body was untouched and the caller can forward
    the original bytes; a non-empty list means the body must be
    re-serialized before forwarding.
    """
    if not isinstance(body, dict):
        return body, []

    applied: list[str] = []

    reduced = _reduce_main_system_block(body)
    if reduced is not None:
        body = reduced
        applied.append("reduce-main-system")

    # filter-tools runs before strip-system-reminders: the stripper decides
    # whether to keep an injected MCP-instructions / skills-catalog block based
    # on which tools actually survive filtering, so it keys off the post-filter
    # tool list — never tools.json directly, which can disagree with what was
    # forwarded this request (e.g. a just-discovered tool, or a fail-open
    # allow-all when the file is malformed). filter_tools leaves the enabled
    # set in body["tools"] either way (it returns None only when nothing was
    # dropped, i.e. body["tools"] is already the full enabled list).
    filtered = filter_tools(body)
    if filtered is not None:
        body, _dropped, _discovered = filtered
        applied.append("filter-tools")

    tools_list = body.get("tools")
    enabled_tool_names = {
        t["name"]
        for t in (tools_list if isinstance(tools_list, list) else [])
        if isinstance(t, dict) and isinstance(t.get("name"), str)
    }
    stripped = _strip_system_reminders(body, enabled_tool_names, tools_known=isinstance(tools_list, list))
    if stripped is not None:
        body = stripped
        applied.append("strip-system-reminders")

    return body, applied


# Fields we keep from the env section of the main system prompt.
# Order here determines order in the rewritten block.
_KEEP_FIELDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Primary working directory", re.compile(r"Primary working directory:\s*(.+?)\s*$", re.MULTILINE)),
    ("Platform", re.compile(r"Platform:\s*(.+?)\s*$", re.MULTILINE)),
    ("OS Version", re.compile(r"OS Version:\s*(.+?)\s*$", re.MULTILINE)),
)

# Env-section landmarks Anthropic has used historically. Either format
# satisfies the heading check — we accept the current Markdown heading
# and the older sentence-style preamble. Used as the first of two
# independent gates that confirm "this block contains the env section."
_ENV_SECTION_LANDMARKS: tuple[str, ...] = (
    "# Environment",
    "useful information about the environment",
)

# Minimum field-label matches required to consider a block the main
# behavioral prompt. Two of three labels appearing together is structural
# (an actual env section); a single incidental mention is not enough.
_MIN_FIELD_MATCHES = 2

# Length floor for candidate blocks. The real main prompt is ~27K, so 5K
# is a comfortable margin while still ruling out short preambles.
_MAIN_BLOCK_MIN_CHARS = 5000

# Behavioral rules appended to the reduced block. Single-line, imperative,
# meant to nudge model defaults back where the discarded prompt would have.
# Add new rules sparingly — each one consumes prompt budget and the goal of
# the reduction is to let the model run on defaults.
_BEHAVIORAL_RULES = (
    "NEVER use shells (Bash, PowerShell) for an operation another available "
    "tool handles. Example: reading a file with cat/Get-Content when a Read "
    "tool is available."
)


def _reduce_main_system_block(body: dict) -> dict | None:
    """Replace the main 27K behavioral prompt with operational env info only.

    Returns the modified body, or None if nothing matched (fail-open).
    """
    system = body.get("system")
    if not isinstance(system, list):
        return None

    target_idx = _find_main_system_block(system)
    if target_idx is None:
        return None

    text = system[target_idx].get("text", "")
    new_lines: list[str] = []
    for label, pattern in _KEEP_FIELDS:
        match = pattern.search(text)
        if match:
            new_lines.append(f"{label}: {match.group(1).strip()}")

    if not new_lines:
        return None  # nothing extractable — leave the block alone

    # Copy-on-write: build a new body so the caller's record-of-original
    # (used for logging) is not mutated.
    new_block = dict(system[target_idx])
    new_block["text"] = "\n".join(new_lines) + "\n\n" + _BEHAVIORAL_RULES
    new_system = list(system)
    new_system[target_idx] = new_block
    return {**body, "system": new_system}


def _find_main_system_block(system: list) -> int | None:
    """Index of the longest cached text block that looks like the main
    behavioral prompt, or None if no candidate qualifies.

    A candidate must satisfy ALL of:
      - text block with cache_control
      - longer than _MAIN_BLOCK_MIN_CHARS
      - contains an env-section landmark (current or older format)
      - contains at least _MIN_FIELD_MATCHES of the env field labels

    The landmark and field-count checks are independent: a request type
    that incidentally mentions one of those labels would not also carry
    the `# Environment` heading. Both must fire for the transform to run.
    """
    target_idx: int | None = None
    target_len = _MAIN_BLOCK_MIN_CHARS
    for i, block in enumerate(system):
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        if not block.get("cache_control"):
            continue
        text = block.get("text", "")
        if len(text) <= target_len:
            continue
        if not _looks_like_main_system_block(text):
            continue
        target_idx, target_len = i, len(text)
    return target_idx


def _looks_like_main_system_block(text: str) -> bool:
    """Two-gate identity check: env-section landmark AND multiple field labels."""
    if not any(marker in text for marker in _ENV_SECTION_LANDMARKS):
        return False
    matches = sum(1 for _, pattern in _KEEP_FIELDS if pattern.search(text))
    return matches >= _MIN_FIELD_MATCHES


# ---------------------------------------------------------------------------
# strip-system-reminders
# ---------------------------------------------------------------------------
#
# Claude Code injects harness reminders in three distinct places:
#
#   1. As stand-alone <system-reminder> text content blocks inside user-role
#      messages, alongside the actual user prompt.
#   2. As inline <system-reminder> appendages to `tool_result` content — the
#      harness appends a reminder to the *string* returned by a tool, so it
#      rides along with the legitimate output of every tool call.
#   3. As stand-alone `role:"system"` messages in the messages array. The
#      `mid-conversation-system-2026-04-07` beta moved much of what used to be
#      a <system-reminder> text block into its own inline system message — the
#      "task tools haven't been used" nudge, injected MCP-server instructions,
#      the skills catalog, etc. These carry no <system-reminder> tag; the
#      signal is the `system` role appearing inside `messages` (the real system
#      prompt lives in the top-level `system` field, never here).
#
#      Form 3 is handled SELECTIVELY, not with a blanket drop, because some of
#      this content is the *only* copy of usage guidance for a tool — and a
#      tool the user disables today may be re-enabled tomorrow. The rule, keyed
#      off the post-filter tool list:
#        * recognized transient nudges (e.g. "task tools haven't been used") →
#          dropped;
#        * the skills catalog → kept only while the `Skill` tool is enabled;
#        * each "## <server>" MCP-instructions subsection → kept only while that
#          server still has an enabled `mcp__<server>__*` tool;
#        * anything we don't positively recognize → passed through unchanged
#          (fail-open: never silently drop an unknown injected message).
#
# Forms 1-2 and the recognized parts of form 3 are stripped consistently across
# every turn (the decision is a pure function of the message text and the
# enabled tool set, both stable turn-to-turn) so the cache prefix never drifts.

# Whole-block matcher: anchored, so only text blocks that are *entirely* a
# reminder qualify. Prose that mentions the tag in passing does not match.
_WHOLE_REMINDER_RE = re.compile(
    r"\A\s*<system-reminder>.*?</system-reminder>\s*\Z",
    re.DOTALL,
)

# Inline matcher: unanchored. Consumes adjacent newlines on either side so
# stripping doesn't leave stranded blank lines between real content blocks.
_INLINE_REMINDER_RE = re.compile(
    r"\n*<system-reminder>.*?</system-reminder>\n*",
    re.DOTALL,
)


def _is_whole_reminder_block(block: Any) -> bool:
    """True iff this text block's entire content is a reminder wrapper."""
    if not isinstance(block, dict) or block.get("type") != "text":
        return False
    return bool(_WHOLE_REMINDER_RE.match(block.get("text", "")))


def _clean_inline_reminders(block: Any) -> dict | None:
    """If this `tool_result` block carries embedded <system-reminder> tags in
    its content, return a copy with them excised in place. Else None.

    Only `tool_result` content is rewritten. Reminders never ride *inline* with
    a user's own words: in observed traffic a user-authored text block is either
    wholly a reminder (dropped upstream by `_is_whole_reminder_block`) or carries
    no reminder at all. Running an unanchored regex over real user prose would
    therefore buy nothing while risking the silent corruption of a message that
    merely quotes the tag (e.g. a question *about* `<system-reminder>` — exactly
    the case when using Claude Code on this proxy). A partial reminder inside
    user text fails open: it passes through and surfaces in the log.

    Handles two payload shapes for tool_result.content:
      - A string (most common — file reads, command output, etc.).
      - A list of sub-blocks (text/image) — walks the text ones only.
    """
    if not isinstance(block, dict):
        return None
    btype = block.get("type")

    if btype == "tool_result":
        inner = block.get("content")
        if isinstance(inner, str):
            if "<system-reminder>" not in inner:
                return None
            cleaned = _INLINE_REMINDER_RE.sub("", inner)
            if cleaned == inner:
                return None
            new_block = dict(block)
            new_block["content"] = cleaned
            return new_block
        if isinstance(inner, list):
            new_inner: list = []
            inner_changed = False
            for sub in inner:
                if isinstance(sub, dict) and sub.get("type") == "text" and "<system-reminder>" in sub.get("text", ""):
                    cleaned = _INLINE_REMINDER_RE.sub("", sub["text"])
                    if cleaned != sub["text"]:
                        new_sub = dict(sub)
                        new_sub["text"] = cleaned
                        new_inner.append(new_sub)
                        inner_changed = True
                        continue
                new_inner.append(sub)
            if not inner_changed:
                return None
            new_block = dict(block)
            new_block["content"] = new_inner
            return new_block

    return None


def _strip_system_reminders(
    body: dict,
    enabled_tool_names: set[str] | None = None,
    tools_known: bool = True,
) -> dict | None:
    """Strip reminders in all three forms (whole user text block, inline
    inside user content, and stand-alone `role:"system"` messages). Stable
    across turns so the cache prefix doesn't drift.

    `enabled_tool_names` is the post-filter set of tool names that survive to
    the upstream request; `tools_known` is False when the request carried no
    `tools` field at all. Together they gate form 3: a skills catalog is kept
    only while `Skill` is enabled, an MCP subsection only while its server has
    an enabled tool. When the tool set is unknown we keep everything (fail-open).

    Returns the modified body, or None if nothing was stripped. Never leaves a
    user message with empty content, and never empties the whole messages array
    (the API rejects both) — in those cases the affected item is left alone.
    """
    if enabled_tool_names is None:
        enabled_tool_names = set()

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None

    new_messages: list = []
    changed = False

    for msg in messages:
        # Form 3: a stand-alone inline system message (mid-conversation-system
        # beta). The real system prompt is the top-level `system` field, so any
        # `role:"system"` entry here is harness-injected context — trim it down
        # to only the parts whose tools are still enabled (see _trim_inline_
        # system_message). None means "nothing worth keeping" → drop it whole.
        if isinstance(msg, dict) and msg.get("role") == "system":
            trimmed = _trim_inline_system_message(msg.get("content"), enabled_tool_names, tools_known)
            if trimmed is None:
                changed = True
                continue  # drop the whole message
            if trimmed != msg.get("content"):
                new_msg = dict(msg)
                new_msg["content"] = trimmed
                new_messages.append(new_msg)
                changed = True
            else:
                new_messages.append(msg)  # recognized nothing → keep verbatim
            continue
        if not isinstance(msg, dict) or msg.get("role") != "user":
            new_messages.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            new_messages.append(msg)
            continue

        new_blocks: list = []
        msg_changed = False
        for block in content:
            if _is_whole_reminder_block(block):
                msg_changed = True
                continue  # drop the block entirely
            cleaned = _clean_inline_reminders(block)
            if cleaned is not None:
                new_blocks.append(cleaned)
                msg_changed = True
            else:
                new_blocks.append(block)

        if not msg_changed:
            new_messages.append(msg)
            continue
        if not new_blocks:
            new_messages.append(msg)  # never emit empty content
            continue

        new_msg = dict(msg)
        new_msg["content"] = new_blocks
        new_messages.append(new_msg)
        changed = True

    if not changed:
        return None
    if not new_messages:
        return None  # never forward an empty messages array — fail open
    return {**body, "messages": new_messages}


# --- form 3: selective trimming of inline `role:"system"` messages ----------

# Recognized transient nudges. An inline system message whose text begins with
# one of these is a throwaway reminder (not tool guidance) and is dropped whole.
# Match on the leading literal only — structural, not a fuzzy search — so an
# unrelated message that merely quotes the phrase later on is not caught. New
# nudge shapes fail open (they pass through) and surface in the log, which is
# the signal to add them here.
_KNOWN_SYSTEM_NUDGE_PREFIXES: tuple[str, ...] = (
    "The task tools haven't been used recently",
    "The user stepped away and is coming back",
)

# Landmarks that identify the two recognized instruction blocks.
_MCP_INSTRUCTIONS_HEADING = "# MCP Server Instructions"
_SKILLS_CATALOG_MARKER = "are available for use with the Skill tool"

# Server name out of an MCP tool name: `mcp__<server>__<tool>`. Non-greedy so
# the first `__` ends the server segment, but server names may contain single
# underscores (e.g. `claude_ai_Gmail`), which this preserves.
_MCP_TOOL_NAME_RE = re.compile(r"^mcp__(.+?)__.+$")


def _trim_inline_system_message(content: Any, enabled_tool_names: set[str], tools_known: bool) -> Any | None:
    """Return the trimmed content for an inline `role:"system"` message, the
    original content unchanged if nothing is recognized, or None to drop it.

    Only positively-recognized noise is removed; everything else passes
    through. This is the fail-open default — an unknown injected system message
    might matter, so we never drop it on a guess.
    """
    if not isinstance(content, str):
        return content  # non-string shape: leave it alone

    stripped = content.lstrip()

    # Recognized transient nudge → drop the whole message.
    if any(stripped.startswith(p) for p in _KNOWN_SYSTEM_NUDGE_PREFIXES):
        return None

    # MCP-instructions and/or skills-catalog block → trim sub-blocks by tool
    # enablement. Recognize it by either landmark (they often share a message).
    if stripped.startswith(_MCP_INSTRUCTIONS_HEADING) or _SKILLS_CATALOG_MARKER in content:
        trimmed = _trim_mcp_and_skills_block(content, enabled_tool_names, tools_known)
        if trimmed is None or not trimmed.strip():
            return None
        return trimmed

    # Unrecognized → keep verbatim.
    return content


def _trim_mcp_and_skills_block(text: str, enabled_tool_names: set[str], tools_known: bool) -> str | None:
    """Split an injected block into its MCP-instructions portion and its skills
    catalog, keep each only while its backing tool is enabled, and recombine.

    Returns the recombined text (deterministic for a given text + enabled set),
    or None if nothing survives. Unknown tool set (tools_known False) keeps
    everything — we only drop when we can positively confirm a tool is gone.
    """
    skill_enabled = (not tools_known) or ("Skill" in enabled_tool_names)
    enabled_servers = {m.group(1) for name in enabled_tool_names if (m := _MCP_TOOL_NAME_RE.match(name))}

    mcp_part, skills_part = _split_off_skills_catalog(text)

    kept: list[str] = []

    if mcp_part.strip():
        if mcp_part.lstrip().startswith(_MCP_INSTRUCTIONS_HEADING):
            trimmed_mcp = _trim_mcp_servers(mcp_part, enabled_servers, tools_known)
            if trimmed_mcp:
                kept.append(trimmed_mcp)
        else:
            kept.append(mcp_part.strip())  # leading content we don't parse → keep

    if skills_part.strip() and skill_enabled:
        kept.append(skills_part.strip())

    if not kept:
        return None
    return "\n\n".join(kept)


def _split_off_skills_catalog(text: str) -> tuple[str, str]:
    """Split `text` at the line that opens the skills catalog. Returns
    (before, catalog_to_end); the catalog half is "" if there is no catalog."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _SKILLS_CATALOG_MARKER in line:
            return "".join(lines[:i]), "".join(lines[i:])
    return text, ""


def _trim_mcp_servers(mcp_text: str, enabled_servers: set[str], tools_known: bool) -> str | None:
    """Keep only the `## <server>` subsections whose server still has an enabled
    tool. Returns the trimmed block, or None if no subsection survives.

    A subsection is dropped only when we can confirm its server is gone: if the
    text has no parseable `## ` subsections, or the tool set is unknown, the
    block is kept verbatim (fail-open)."""
    if not tools_known:
        return mcp_text.strip()

    lines = mcp_text.splitlines()
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur_name: str | None = None
    cur: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if cur_name is not None:
                sections.append((cur_name, cur))
            cur_name = line[3:].strip()
            cur = [line]
        elif cur_name is None:
            preamble.append(line)
        else:
            cur.append(line)
    if cur_name is not None:
        sections.append((cur_name, cur))

    if not sections:
        return mcp_text.strip()  # unrecognized structure → keep

    kept_section_lines: list[str] = []
    for name, sec_lines in sections:
        if name in enabled_servers:
            kept_section_lines.extend(sec_lines)

    if not kept_section_lines:
        return None  # every server disabled → drop the whole MCP block
    return "\n".join(preamble + kept_section_lines).strip()
