"""Tests for strip-system-reminders, focused on the fix that stops the
unanchored inline regex from rewriting user-authored text. The contract:

  * a user text block that is *wholly* a reminder  -> dropped;
  * a reminder embedded in tool_result.content      -> excised in place;
  * a reminder quoted inside ordinary user prose    -> left untouched (fail open).
"""

from __future__ import annotations

from clawback.transforms import apply_request_transforms

REMINDER = "<system-reminder>Do the thing now.</system-reminder>"


def _transform(messages):
    body = {"messages": messages}
    new_body, applied = apply_request_transforms(body)
    return new_body, applied


def test_whole_user_text_reminder_block_dropped():
    new_body, applied = _transform(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Real question"},
                    {"type": "text", "text": REMINDER},
                ],
            },
        ]
    )
    assert "strip-system-reminders" in applied
    blocks = new_body["messages"][0]["content"]
    assert blocks == [{"type": "text", "text": "Real question"}]


def test_tool_result_inline_reminder_excised():
    new_body, applied = _transform(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": f"command output\n{REMINDER}"},
                ],
            },
        ]
    )
    assert "strip-system-reminders" in applied
    result = new_body["messages"][0]["content"][0]
    assert "<system-reminder>" not in result["content"]
    assert "command output" in result["content"]


def test_reminder_quoted_in_user_prose_is_preserved():
    """The fix: a partial user text block that merely mentions the tag must NOT
    be rewritten. (Asking a question *about* <system-reminder> is the real case
    when using Claude Code on this very proxy.)"""
    prose = f"Why does the proxy strip {REMINDER} from my messages?"
    new_body, applied = _transform(
        [
            {"role": "user", "content": [{"type": "text", "text": prose}]},
        ]
    )
    assert "strip-system-reminders" not in applied
    assert new_body["messages"][0]["content"][0]["text"] == prose


def test_tool_result_list_content_reminder_excised():
    new_body, applied = _transform(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "text", "text": f"out\n{REMINDER}"},
                        ],
                    },
                ],
            },
        ]
    )
    assert "strip-system-reminders" in applied
    sub = new_body["messages"][0]["content"][0]["content"][0]
    assert "<system-reminder>" not in sub["text"]
