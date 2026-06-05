"""Repair for Claude Code's interleaved-thinking reordering bug.

# The bug

With extended thinking + tool use, the API may return an assistant turn whose
`thinking` / `redacted_thinking` blocks are *interleaved* among the `tool_use`
blocks (e.g. block order: thinking, tool, tool, thinking, tool, ...). The API
signs each thinking block and requires it to be sent back **in its original
position** on the next request — "these blocks must remain as they were in the
original response."

Claude Code (observed on 2.1.x, via the `ensureToolResultPairing` path) instead
regroups all thinking blocks to the front of the turn when it reserializes the
conversation to send tool results back. Same blocks, same signatures, same
bytes — only the order changes — which is enough for the API to reject the
whole request with HTTP 400. The session is then stuck.

# The repair

The proxy already sees the *original* response, where the order is correct. We
remember the canonical block order for each assistant turn (keyed by its unique
thinking signatures), and when a later request sends that same turn back with
the blocks reordered, we put them back in the canonical order before forwarding.

This is deliberately **stateful** and lives outside the pure, fail-open request
transforms in `transforms.py`: it needs cross-request memory of responses. It is
owned by the proxy handler for the lifetime of the process.

# Safety properties

  * Reorder-only. We reorder the *request's own* block objects into the cached
    order — we never substitute the cached (assembler-reconstructed) blocks, so
    Claude Code's exact submitted bytes (incl. `tool_use.input` JSON) are
    preserved. The single thing we change is order.
  * Exact-match or nothing. A turn is repaired only if its blocks are the exact
    same multiset we recorded (by stable identity: thinking signature, redacted
    data, tool_use id, text hash — thinking also carries a text hash so tampered
    thinking can never match). Any mismatch, duplicate identity, unknown block
    type, or cache miss → leave the request untouched (fail open).
  * Deterministic. The canonical order for a given turn is fixed, so repairing
    it the same way every turn keeps the prompt-cache prefix stable; it also
    re-aligns history with what the API originally generated.
  * Session-scoped LRU. Entries are keyed by (session id, thinking key) and
    bounded, so memory stays flat and sessions can't read each other's orders.

A proxy restart or LRU eviction means a cache miss → the bad order passes
through and the API may 400, exactly as it would without the proxy. We never
make a request *worse*.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, OrderedDict
from typing import Any

# Block identity tokens. The first element is a one-letter kind tag; the rest
# uniquely and stably identify the block across the response→resend round trip.
Identity = tuple

_DEFAULT_MAX_ENTRIES = 256


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block_identity(block: Any) -> Identity | None:
    """Stable identity for one content block, or None if the block type is one
    we won't reorder around (forces the whole turn to fail open).

    Anchors are chosen to be byte-stable between the original response and
    Claude Code's resend:
      * thinking      — signature (unique) + a hash of the thinking text, so a
                        block whose text was altered can never match.
      * redacted      — a hash of the encrypted `data` payload.
      * tool_use      — id + name + a hash of the *canonical* input JSON. We
                        hash canonical JSON (sorted keys) rather than the raw
                        bytes: the assembler round-trips input through
                        json.loads, so only a key-order-independent hash matches
                        the resend, while still catching a genuinely altered
                        name or input.
      * text          — a hash of the text.

    Every text-bearing field is type-checked: a block of an expected type but
    an unexpected shape returns None, which forces the whole turn to fail open
    rather than letting a malformed value raise.
    """
    if not isinstance(block, dict):
        return None
    btype = block.get("type")
    if btype == "thinking":
        sig = block.get("signature")
        thinking = block.get("thinking", "")
        if not isinstance(sig, str) or not sig or not isinstance(thinking, str):
            return None
        return ("T", sig, _sha(thinking))
    if btype == "redacted_thinking":
        data = block.get("data")
        if not isinstance(data, str) or not data:
            return None
        return ("R", _sha(data))
    if btype == "tool_use":
        tid = block.get("id")
        name = block.get("name")
        if not isinstance(tid, str) or not tid or not isinstance(name, str):
            return None
        try:
            canon_input = json.dumps(
                block.get("input", {}),
                sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError):
            return None  # non-serializable input — don't reorder around it
        return ("U", tid, name, _sha(canon_input))
    if btype == "text":
        text = block.get("text", "")
        if not isinstance(text, str):
            return None
        return ("X", _sha(text))
    # server_tool_use, image, or anything new: don't risk reordering around it.
    return None


def _identities(content: Any) -> list[Identity] | None:
    """Identity sequence for an assistant message's content, or None if the
    content isn't a list or contains a block type we won't handle."""
    if not isinstance(content, list):
        return None
    out: list[Identity] = []
    for block in content:
        ident = _block_identity(block)
        if ident is None:
            return None
        out.append(ident)
    return out


def _thinking_key(identities: list[Identity]) -> tuple:
    """Cache key component: the thinking/redacted identities, sorted. Stable and
    order-independent (the resend reorders them, so the key must not depend on
    order)."""
    return tuple(sorted(i for i in identities if i[0] in ("T", "R")))


def _has_thinking(identities: list[Identity]) -> bool:
    return any(i[0] in ("T", "R") for i in identities)


def _has_tool_use(identities: list[Identity]) -> bool:
    return any(i[0] == "U" for i in identities)


class ThinkingOrderCache:
    """Process-wide, session-scoped LRU of canonical assistant-turn block orders."""

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._max = max_entries
        # (session_id, thinking_key) -> canonical identity sequence
        self._store: "OrderedDict[tuple, list[Identity]]" = OrderedDict()

    # ------------------------------------------------------------------ record

    def record_response(self, session_id: str, body: Any, status: int) -> None:
        """Remember the canonical block order of a successful assistant response.

        No-op unless the response is a 2xx assistant message that interleaves at
        least one thinking block with at least one tool_use (the only shape the
        reorder bug can corrupt). Never raises on bad input — callers fail open.
        """
        if not (200 <= status < 300):
            return
        if not isinstance(body, dict):
            return
        if body.get("type") != "message" or body.get("role") != "assistant":
            return
        # A stream that carried an `error` event (HTTP 200, then a mid-stream
        # failure) is not a canonical, complete turn — never treat its block
        # order as authoritative for a later repair.
        if body.get("errors"):
            return

        identities = _identities(body.get("content"))
        if identities is None:
            return
        if not _has_thinking(identities) or not _has_tool_use(identities):
            return
        # Duplicate identities make reordering ambiguous — don't record.
        if len(set(identities)) != len(identities):
            return

        key = (session_id or "", _thinking_key(identities))
        self._store[key] = identities
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    # ------------------------------------------------------------------ repair

    def repair_request(self, session_id: str, body: Any) -> tuple[Any, int]:
        """Restore canonical block order in any assistant message whose blocks
        match a recorded response. Returns (possibly-new body, repaired count).

        Copy-on-write: the original body is returned untouched when nothing is
        repaired. Fails open on every ambiguity.
        """
        if not isinstance(body, dict):
            return body, 0
        messages = body.get("messages")
        if not isinstance(messages, list):
            return body, 0

        new_messages: list = []
        repaired = 0
        for msg in messages:
            fixed = self._maybe_reorder(session_id, msg)
            if fixed is not None:
                new_messages.append(fixed)
                repaired += 1
            else:
                new_messages.append(msg)

        if repaired == 0:
            return body, 0
        return {**body, "messages": new_messages}, repaired

    def _maybe_reorder(self, session_id: str, msg: Any) -> dict | None:
        """Return a reordered copy of `msg` if it needs (and we can safely do)
        repair, else None to keep the original."""
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            return None
        content = msg.get("content")
        identities = _identities(content)
        if identities is None or not _has_thinking(identities):
            return None
        if len(set(identities)) != len(identities):
            return None  # duplicate identities — ambiguous, fail open

        key = (session_id or "", _thinking_key(identities))
        canonical = self._store.get(key)
        if canonical is None:
            return None  # never saw this turn — fail open
        self._store.move_to_end(key)  # refresh LRU on any hit, repaired or not

        if Counter(identities) != Counter(canonical):
            return None  # different block set than recorded — fail open
        if identities == canonical:
            return None  # already in canonical order — nothing to do

        by_identity = {ident: block for ident, block in zip(identities, content)}
        new_content = [by_identity[ident] for ident in canonical]

        new_msg = dict(msg)
        new_msg["content"] = new_content
        return new_msg
