"""Incremental SSE parser.

Reassembles streamed Anthropic-style content blocks (text, thinking, tool_use,
redacted_thinking) into a single message dict shaped like a non-streaming
response. Parsing is observation-only: the proxy forwards upstream bytes
verbatim; this class consumes a copy of those bytes purely for logging.
"""

from __future__ import annotations

import codecs
import json
import re
from typing import Any

# Events outside the content/message lifecycle that carry nothing for the
# assembled message and are safely ignored: pings are keepalives, and a
# message_stop payload is empty (the stop_reason arrives on message_delta).
_BENIGN_EVENT_TYPES = frozenset({"ping", "message_stop"})

# SSE record separator. Anthropic emits LF today; the spec also allows CRLF,
# and a server-side change to CRLF would otherwise silently blind the log
# (and the thinking-order recorder) without breaking the forwarded stream.
_EVENT_SEP = re.compile(r"\r?\n\r?\n")

# Unknown-event payloads kept verbatim for the log. They are the primary
# evidence when Anthropic ships a new stream shape; capped so a pathological
# stream cannot grow the assembled record without bound.
_MAX_UNKNOWN_SAMPLES = 10


class SSEAssembler:
    """Consume SSE bytes incrementally; expose the assembled message at the end."""

    def __init__(self) -> None:
        # A multi-byte UTF-8 character can straddle a chunk boundary. An
        # incremental decoder holds the partial sequence until the next chunk
        # completes it, so the assembled copy — and the identity hashes the
        # thinking-order repair derives from it — match the wire bytes exactly
        # rather than gaining a stray replacement character.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf: str = ""

        # Per-block accumulators (reset at each content_block_start)
        self._block: dict | None = None
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._signature_parts: list[str] = []
        self._input_json_parts: list[str] = []

        # Message-level state
        self.id: str = ""
        self.model: str = ""
        self.type: str = "message"
        self.role: str = "assistant"
        self.stop_reason: str | None = None
        self.stop_sequence: str | None = None
        self.usage: dict[str, Any] = {}
        self.content: list[dict] = []

        # Events outside the normal message lifecycle, captured so a stream that
        # is *only* an error (or carries a future/unknown event shape) is never
        # logged as an empty, success-shaped message.
        self.errors: list[dict] = []
        self.unknown_event_types: dict[str, int] = {}
        self.unknown_event_samples: list[dict] = []

    # ------------------------------------------------------------------ API

    def feed(self, chunk: bytes) -> None:
        """Parse any complete blank-line-terminated records in chunk; buffer the rest."""
        self._buf += self._decoder.decode(chunk)
        while (sep := _EVENT_SEP.search(self._buf)) is not None:
            event_str, self._buf = self._buf[: sep.start()], self._buf[sep.end() :]
            self._handle_event(event_str)

    def assembled(self) -> dict:
        """Return the assembled message as a non-streaming response dict.

        Called once, after the stream has fully drained. Flushes any final
        event that arrived without its terminating blank line (e.g. an abruptly
        closed connection) so a trailing `error` event is not lost; incomplete
        or garbled leftovers are ignored by `_parse_data_line`, so this is safe.
        """
        leftover = self._buf + self._decoder.decode(b"", final=True)
        self._buf = ""
        if leftover.strip():
            self._handle_event(leftover)

        message: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "role": self.role,
            "model": self.model,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "stop_sequence": self.stop_sequence,
            "usage": self.usage,
        }
        # Surface out-of-band events so the log reflects what actually crossed
        # the wire instead of an empty body. Only present when non-empty so a
        # normal response is unchanged.
        if self.errors:
            message["errors"] = self.errors
        if self.unknown_event_types:
            message["unknown_event_types"] = self.unknown_event_types
        if self.unknown_event_samples:
            message["unknown_event_samples"] = self.unknown_event_samples
        return message

    # ----------------------------------------------------------- internals

    def _handle_event(self, event_str: str) -> None:
        data = self._parse_data_line(event_str)
        if data is None:
            return
        etype = data.get("type", "")
        dispatch = {
            "message_start": self._on_message_start,
            "content_block_start": self._on_block_start,
            "content_block_delta": self._on_block_delta,
            "content_block_stop": self._on_block_stop,
            "message_delta": self._on_message_delta,
        }
        handler = dispatch.get(etype)
        if handler:
            handler(data)
            return
        # Anything else is captured rather than dropped. The `error` event is
        # the case that matters most: without this an error-only stream (HTTP
        # 200, then `event: error`) would be logged as an empty success.
        if etype == "error":
            err = data.get("error")
            self.errors.append(err if isinstance(err, dict) else {"raw": data})
        elif etype and etype not in _BENIGN_EVENT_TYPES:
            self.unknown_event_types[etype] = self.unknown_event_types.get(etype, 0) + 1
            if len(self.unknown_event_samples) < _MAX_UNKNOWN_SAMPLES:
                self.unknown_event_samples.append(data)

    @staticmethod
    def _parse_data_line(event_str: str) -> dict | None:
        """Return the JSON payload of the event's `data` line(s), or None for [DONE]/junk.

        Follows the SSE spec where it costs nothing to do so: `data:` with or
        without the optional following space, CRLF or LF line endings, and
        multiple `data` lines joined with newlines before parsing.
        """
        data_lines: list[str] = []
        for line in event_str.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:]
            data_lines.append(payload[1:] if payload.startswith(" ") else payload)
        if not data_lines:
            return None
        joined = "\n".join(data_lines)
        if joined.strip() == "[DONE]":
            return None
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return None

    def _on_message_start(self, data: dict) -> None:
        m = data.get("message", {})
        self.id = m.get("id", "")
        self.model = m.get("model", "")
        self.type = m.get("type", "message")
        self.role = m.get("role", "assistant")
        self.stop_reason = m.get("stop_reason")
        self.stop_sequence = m.get("stop_sequence")
        self.usage.update(m.get("usage", {}))

    def _on_block_start(self, data: dict) -> None:
        block = data.get("content_block", {})
        btype = block.get("type", "")
        self._text_parts.clear()
        self._thinking_parts.clear()
        self._signature_parts.clear()
        self._input_json_parts.clear()
        if btype == "text":
            self._block = {"type": "text", "text": ""}
        elif btype == "thinking":
            self._block = {"type": "thinking", "thinking": "", "signature": ""}
        elif btype == "redacted_thinking":
            self._block = {"type": "redacted_thinking", "data": block.get("data", "")}
        elif btype == "tool_use":
            self._block = {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": {},
            }
        else:
            self._block = {"type": btype, **block}

    def _on_block_delta(self, data: dict) -> None:
        delta = data.get("delta", {})
        dtype = delta.get("type", "")
        if dtype == "text_delta":
            self._text_parts.append(delta.get("text", ""))
        elif dtype == "thinking_delta":
            self._thinking_parts.append(delta.get("thinking", ""))
        elif dtype == "signature_delta":
            self._signature_parts.append(delta.get("signature", ""))
        elif dtype == "input_json_delta":
            self._input_json_parts.append(delta.get("partial_json", ""))

    def _on_block_stop(self, data: dict) -> None:
        if self._block is None:
            return
        btype = self._block["type"]
        if btype == "text":
            self._block["text"] = "".join(self._text_parts)
        elif btype == "thinking":
            self._block["thinking"] = "".join(self._thinking_parts)
            self._block["signature"] = "".join(self._signature_parts)
        elif btype == "tool_use":
            raw = "".join(self._input_json_parts)
            if raw:
                try:
                    self._block["input"] = json.loads(raw)
                except json.JSONDecodeError:
                    self._block["input"] = raw
        self.content.append(self._block)
        self._block = None

    def _on_message_delta(self, data: dict) -> None:
        delta = data.get("delta", {})
        if "stop_reason" in delta:
            self.stop_reason = delta["stop_reason"]
        if "stop_sequence" in delta:
            self.stop_sequence = delta["stop_sequence"]
        self.usage.update(data.get("usage", {}))
