"""Tests for log-only redaction: sensitive values must be scrubbed from the
on-disk record without ever mutating what was forwarded upstream.
"""

from __future__ import annotations

import copy

from clawback.config import SENSITIVE_HEADERS
from clawback.log import _sanitize_headers, _sanitize_request_body


def test_session_id_header_is_redacted():
    assert "x-claude-code-session-id" in SENSITIVE_HEADERS
    headers = {
        "Authorization": "Bearer secret",
        "X-Claude-Code-Session-Id": "4dcf4198-7f57-4009-ace2-cddf...",
        "anthropic-version": "2023-06-01",
    }
    out = _sanitize_headers(headers)
    assert out["X-Claude-Code-Session-Id"] == "<redacted>"
    assert out["Authorization"] == "<redacted>"
    assert out["anthropic-version"] == "2023-06-01"  # untouched


def test_metadata_user_id_redacted_in_log_copy():
    body = {
        "model": "claude",
        "metadata": {"user_id": '{"device_id":"abc","account_uuid":"def","session_id":"ghi"}'},
        "messages": [],
    }
    out = _sanitize_request_body(body)
    assert out["metadata"]["user_id"] == "<redacted>"
    # Non-telemetry fields preserved.
    assert out["model"] == "claude"


def test_redaction_is_copy_on_write_does_not_mutate_forwarded_body():
    """The server logs the same parsed body it forwarded; redaction must not
    touch it (or any aliased subtree)."""
    body = {
        "model": "claude",
        "metadata": {"user_id": "SECRET", "other": "keep"},
    }
    before = copy.deepcopy(body)
    out = _sanitize_request_body(body)

    assert body == before  # original untouched
    assert out is not body  # new top-level dict
    assert out["metadata"] is not body["metadata"]  # new metadata dict
    assert out["metadata"]["other"] == "keep"  # sibling fields survive


def test_body_without_metadata_returned_unchanged():
    body = {"model": "claude", "messages": []}
    assert _sanitize_request_body(body) is body


def test_non_dict_body_passthrough():
    assert _sanitize_request_body(None) is None
    assert _sanitize_request_body("raw text") == "raw text"


def test_metadata_without_user_id_untouched():
    body = {"metadata": {"something_else": 1}}
    assert _sanitize_request_body(body) is body
