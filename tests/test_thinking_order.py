"""Tests for the stateful interleaved-thinking repair, including the guard that
an errored stream is never treated as a canonical successful turn.
"""

from __future__ import annotations

from clawback.thinking_order import ThinkingOrderCache


def _canonical_content():
    # Interleaved: thinking, tool, thinking, tool — the shape the client's
    # ensureToolResultPairing path corrupts by regrouping thinking to the front.
    return [
        {"type": "thinking", "thinking": "a", "signature": "sig-1"},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"x": 1}},
        {"type": "thinking", "thinking": "b", "signature": "sig-2"},
        {"type": "tool_use", "id": "t2", "name": "Glob", "input": {"y": 2}},
    ]


def _response(content, **extra):
    return {"type": "message", "role": "assistant", "content": content, **extra}


def _reordered_to_front(content):
    thinking = [b for b in content if b["type"] == "thinking"]
    others = [b for b in content if b["type"] != "thinking"]
    return thinking + others


def test_reordered_resend_is_repaired_to_canonical_order():
    cache = ThinkingOrderCache()
    content = _canonical_content()
    cache.record_response("sess", _response(content), 200)

    corrupted = _reordered_to_front(content)
    assert corrupted != content  # sanity: the resend really is reordered
    req = {"messages": [{"role": "assistant", "content": corrupted}]}

    new_body, repaired = cache.repair_request("sess", req)
    assert repaired == 1
    assert new_body["messages"][0]["content"] == content


def test_unknown_turn_fails_open():
    cache = ThinkingOrderCache()
    content = _canonical_content()
    corrupted = _reordered_to_front(content)
    req = {"messages": [{"role": "assistant", "content": corrupted}]}

    new_body, repaired = cache.repair_request("sess", req)
    assert repaired == 0
    assert new_body is req  # untouched


def test_errored_stream_is_not_recorded():
    """A 200 stream that carried an `error` event must not seed the cache, so a
    later resend of that (incomplete) turn is not 'repaired' from bad data."""
    cache = ThinkingOrderCache()
    content = _canonical_content()
    cache.record_response("sess", _response(content, errors=[{"type": "api_error"}]), 200)

    corrupted = _reordered_to_front(content)
    req = {"messages": [{"role": "assistant", "content": corrupted}]}
    _new_body, repaired = cache.repair_request("sess", req)
    assert repaired == 0  # nothing was recorded -> fail open


def test_non_2xx_is_not_recorded():
    cache = ThinkingOrderCache()
    content = _canonical_content()
    cache.record_response("sess", _response(content), 400)

    corrupted = _reordered_to_front(content)
    req = {"messages": [{"role": "assistant", "content": corrupted}]}
    _new_body, repaired = cache.repair_request("sess", req)
    assert repaired == 0


def test_sessions_are_isolated():
    cache = ThinkingOrderCache()
    content = _canonical_content()
    cache.record_response("sess-A", _response(content), 200)

    corrupted = _reordered_to_front(content)
    req = {"messages": [{"role": "assistant", "content": corrupted}]}
    _new_body, repaired = cache.repair_request("sess-B", req)
    assert repaired == 0  # different session must not see A's canonical order
