"""Tests for the SSE assembler, focused on:
* faithful capture of out-of-band events (error / unknown), so a stream is
  never logged as an empty success;
* UTF-8 correctness across chunk boundaries (incremental decoder).
"""

from __future__ import annotations

import json

from claude_proxy.sse import SSEAssembler


def _event(name: str, data: dict) -> bytes:
    # ensure_ascii=False so non-ASCII text is emitted as real multi-byte UTF-8
    # (matching production `transforms._event`), which is what the chunk-boundary
    # test actually needs to exercise.
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _feed_all(assembler: SSEAssembler, *events: bytes) -> dict:
    for ev in events:
        assembler.feed(ev)
    return assembler.assembled()


def test_normal_text_message_assembles():
    a = SSEAssembler()
    msg = _feed_all(
        a,
        _event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "model": "claude",
                    "type": "message",
                    "role": "assistant",
                    "usage": {"input_tokens": 5},
                },
            },
        ),
        _event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        _event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        ),
        _event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
        ),
        _event("message_stop", {"type": "message_stop"}),
    )
    assert msg["id"] == "msg_1"
    assert msg["content"] == [{"type": "text", "text": "Hello"}]
    assert msg["stop_reason"] == "end_turn"
    # Clean stream: no diagnostics keys.
    assert "errors" not in msg
    assert "unknown_event_types" not in msg


def test_error_only_stream_is_captured_not_empty():
    a = SSEAssembler()
    msg = _feed_all(
        a,
        _event(
            "error",
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Overloaded"},
            },
        ),
    )
    assert msg["errors"] == [{"type": "overloaded_error", "message": "Overloaded"}]


def test_error_mid_stream_recorded_alongside_content():
    a = SSEAssembler()
    msg = _feed_all(
        a,
        _event(
            "message_start",
            {
                "type": "message_start",
                "message": {"id": "msg_2", "model": "c", "type": "message", "role": "assistant"},
            },
        ),
        _event("error", {"type": "error", "error": {"type": "api_error", "message": "boom"}}),
    )
    assert msg["id"] == "msg_2"
    assert msg["errors"] == [{"type": "api_error", "message": "boom"}]


def test_ping_and_message_stop_are_benign():
    a = SSEAssembler()
    msg = _feed_all(
        a,
        _event("ping", {"type": "ping"}),
        _event("message_stop", {"type": "message_stop"}),
    )
    assert "unknown_event_types" not in msg
    assert "errors" not in msg


def test_unknown_event_type_is_counted():
    a = SSEAssembler()
    msg = _feed_all(
        a,
        _event("something_new", {"type": "something_new"}),
        _event("something_new", {"type": "something_new"}),
    )
    assert msg["unknown_event_types"] == {"something_new": 2}


def test_done_sentinel_ignored():
    a = SSEAssembler()
    a.feed(b"data: [DONE]\n\n")
    msg = a.assembled()
    assert msg["content"] == []
    assert "unknown_event_types" not in msg


def test_multibyte_char_split_across_chunks():
    """A 3-byte char ('—') split mid-sequence must reassemble intact, not as
    replacement chars — the identity hash used by the repair depends on it."""
    a = SSEAssembler()
    full = _event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )
    full += _event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "a—b"},
        },
    )
    full += _event("content_block_stop", {"type": "content_block_stop", "index": 0})

    # Guard the test itself: the em dash must be present as a 3-byte UTF-8
    # sequence, otherwise no boundary split would ever exercise the decoder.
    assert "—".encode() in full
    assert b"\\u2014" not in full

    # Split at every byte offset; each split must yield identical clean output.
    for cut in range(1, len(full)):
        a = SSEAssembler()
        a.feed(full[:cut])
        a.feed(full[cut:])
        msg = a.assembled()
        assert msg["content"] == [{"type": "text", "text": "a—b"}], f"cut={cut}"
        assert "�" not in msg["content"][0]["text"]


def test_final_event_without_blank_line_terminator_is_flushed():
    """An abruptly-closed stream whose last event lacks the trailing blank line
    must still be captured by assembled(), not silently dropped."""
    a = SSEAssembler()
    ev = _event("error", {"type": "error", "error": {"type": "api_error", "message": "cut off"}})
    a.feed(ev.rstrip(b"\n"))  # drop the terminating blank line
    msg = a.assembled()
    assert msg["errors"] == [{"type": "api_error", "message": "cut off"}]


def test_tool_use_input_json_split_across_chunks():
    a = SSEAssembler()
    events = (
        _event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
            },
        )
        + _event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
            },
        )
        + _event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '"/tmp"}'},
            },
        )
        + _event("content_block_stop", {"type": "content_block_stop", "index": 0})
    )
    a.feed(events)
    msg = a.assembled()
    assert msg["content"] == [{"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"path": "/tmp"}}]
