"""Smoke + targeted tests for the markdown renderer, including the new
stream-error / unknown-event surfacing.
"""

from __future__ import annotations

from claude_proxy.render import render


def _base_record(resp_body: dict) -> dict:
    return {
        "seq": 1,
        "ts": "2026-06-05T00:00:00Z",
        "elapsed_s": 1.0,
        "method": "POST",
        "path": "/v1/messages",
        "request": {"headers": {"anthropic-version": "2023-06-01"}, "body": {"model": "claude", "messages": []}},
        "response": {"status": 200, "headers": {"Content-Type": "text/event-stream"}, "body": resp_body},
    }


def test_render_normal_response_does_not_crash():
    rec = _base_record(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 1},
        }
    )
    md = render(rec)
    assert "# Request 001" in md
    assert "hello" in md


def test_render_surfaces_stream_errors():
    rec = _base_record(
        {
            "type": "message",
            "role": "assistant",
            "content": [],
            "errors": [{"type": "overloaded_error", "message": "Overloaded"}],
        }
    )
    md = render(rec)
    assert "Stream errors" in md
    assert "overloaded_error" in md
    assert "Overloaded" in md


def test_render_surfaces_unknown_events():
    rec = _base_record(
        {"type": "message", "role": "assistant", "content": [], "unknown_event_types": {"something_new": 3}}
    )
    md = render(rec)
    assert "Unrecognized SSE events" in md
    assert "something_new" in md


def test_render_redacted_metadata_shows_marker():
    rec = _base_record({"type": "message", "role": "assistant", "content": []})
    rec["request"]["body"]["metadata"] = {"user_id": "<redacted>"}
    md = render(rec)
    assert "<redacted>" in md
