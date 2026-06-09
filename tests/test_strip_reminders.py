"""Tests for strip-system-reminders, focused on the fix that stops the
unanchored inline regex from rewriting user-authored text. The contract:

  * a user text block that is *wholly* a reminder        -> dropped;
  * reminder(s) appended at the END of tool_result text  -> tail excised;
  * a reminder quoted inside ordinary user prose         -> left untouched;
  * tags quoted mid-content (incl. inside tool_results)  -> left untouched;
  * a reminder anywhere but the tail of a tool_result    -> left untouched.

The last three are the fail-open cases: when in doubt, forward verbatim.
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


def test_tool_result_quoting_tags_mid_content_is_preserved():
    """The live bug this guards against: Read-ing a file that *contains* the
    literal tags (this proxy's own source) once let the unanchored DOTALL
    regex span from a tag quoted in a docstring to one quoted in a regex
    literal ~460 lines later, silently excising the source in between. A
    quoted pair mid-content must never be rewritten."""
    quoted = (
        "line 1: docs mention <system-reminder> here\n"
        "line 2: real content that must survive\n"
        "line 3: and a closing </system-reminder> quoted later\n"
        "line 4: trailing real content"
    )
    new_body, applied = _transform(
        [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": quoted}],
            },
        ]
    )
    assert "strip-system-reminders" not in applied
    assert new_body["messages"][0]["content"][0]["content"] == quoted


def test_tool_result_mid_content_reminder_fails_open():
    """A reminder that is NOT at the tail passes through (fail-open). Claude
    Code appends reminders; anything else is unrecognized territory."""
    content = f"before\n{REMINDER}\nafter - real output continues"
    new_body, applied = _transform(
        [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": content}],
            },
        ]
    )
    assert "strip-system-reminders" not in applied
    assert new_body["messages"][0]["content"][0]["content"] == content


def test_tool_result_stacked_trailing_reminders_all_excised():
    second = "<system-reminder>Another nudge.</system-reminder>"
    new_body, applied = _transform(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": f"real output\n{REMINDER}\n{second}\n",
                    },
                ],
            },
        ]
    )
    assert "strip-system-reminders" in applied
    assert new_body["messages"][0]["content"][0]["content"] == "real output"


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
