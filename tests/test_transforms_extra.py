"""Coverage for the transforms not exercised elsewhere: reduce-main-system,
the title-gen / recap short-circuits, and the synthetic SSE round-trip.
"""

from __future__ import annotations

import copy
import json

from clawback.sse import SSEAssembler
from clawback.transforms import (
    _NARRATION_TAIL_GUARD,
    apply_request_transforms,
    maybe_shortcut,
    to_sse_bytes,
)


def _main_system_block() -> dict:
    # > 5000 chars, carries the "# Environment" landmark and all three env labels.
    filler = "background guidance line that pads the behavioral prompt\n" * 200
    text = (
        "# Environment\n"
        + filler
        + "Primary working directory: /home/u/project\n"
        + "Platform: linux\n"
        + "OS Version: Ubuntu 24.04\n"
    )
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


# --- reduce-main-system ------------------------------------------------------


def test_reduce_main_system_replaces_block_with_env_plus_directive():
    body = {"system": [_main_system_block()]}
    out, applied = apply_request_transforms(body)
    assert "reduce-main-system" in applied
    new_text = out["system"][0]["text"]
    assert "Primary working directory: /home/u/project" in new_text
    assert "Platform: linux" in new_text
    assert "OS Version: Ubuntu 24.04" in new_text
    assert "NEVER use shells" in new_text  # the behavioral directive
    assert len(new_text) < 2000  # the 27K block is gone
    # cache_control is preserved on the rewritten block.
    assert out["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_reduce_main_system_is_copy_on_write():
    body = {"system": [_main_system_block()]}
    before = copy.deepcopy(body)
    apply_request_transforms(body)
    assert body == before  # original untouched


def test_reduce_main_system_fails_open_on_short_block():
    # Has the labels but is far too short to be the real behavioral prompt.
    block = {
        "type": "text",
        "text": "Primary working directory: /x\nPlatform: linux\n# Environment",
        "cache_control": {"type": "ephemeral"},
    }
    out, applied = apply_request_transforms({"system": [block]})
    assert "reduce-main-system" not in applied
    assert out["system"][0]["text"] == block["text"]


def test_reduce_main_system_fails_open_without_cache_control():
    block = _main_system_block()
    del block["cache_control"]
    out, applied = apply_request_transforms({"system": [block]})
    assert "reduce-main-system" not in applied


# --- inject-narration-tail ---------------------------------------------------


def test_inject_narration_tail_appends_trailing_system_message():
    body = {
        "tools": [{"name": "Read"}],
        "messages": [{"role": "user", "content": "do the thing"}],
    }
    before = copy.deepcopy(body)
    out, applied = apply_request_transforms(body)
    assert "inject-narration-tail" in applied
    last = out["messages"][-1]
    assert last["role"] == "system"
    assert last["content"] == _NARRATION_TAIL_GUARD
    # exactly one guard, appended after the real messages
    assert sum(m.get("role") == "system" for m in out["messages"]) == 1
    assert out["messages"][:-1] == before["messages"]
    # copy-on-write: the caller's original body is untouched
    assert body == before


def test_inject_narration_tail_skipped_without_tools():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    _out, applied = apply_request_transforms(body)
    assert "inject-narration-tail" not in applied


def test_inject_narration_tail_skipped_without_messages():
    body = {"tools": [{"name": "Read"}]}
    _out, applied = apply_request_transforms(body)
    assert "inject-narration-tail" not in applied


# --- title-gen ---------------------------------------------------------------


def test_title_gen_shortcut_detected_and_synthesized():
    body = {
        "model": "claude-opus-4",
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {"required": ["title"], "properties": {"title": {"type": "string"}}},
            }
        },
    }
    result = maybe_shortcut(body)
    assert result is not None
    resp, reason = result
    assert reason == "title-gen"
    payload = json.loads(resp["content"][0]["text"])
    assert payload["title"].startswith("CONVERSATION_")


def test_title_gen_not_triggered_without_title_schema():
    body = {"output_config": {"format": {"type": "json_schema", "schema": {"required": ["summary"]}}}}
    assert maybe_shortcut(body) is None


def test_title_gen_not_triggered_with_extra_properties():
    """A schema asking for more than a bare title (say, a future PR-title +
    description call) is a different feature and must not be hijacked with a
    synthetic conversation label."""
    body = {
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "required": ["title"],
                    "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
                },
            }
        }
    }
    assert maybe_shortcut(body) is None


# --- recap -------------------------------------------------------------------


def test_recap_shortcut_detected_and_synthesized():
    body = {
        "model": "claude-opus-4",
        "messages": [{"role": "user", "content": "The user stepped away and is coming back. Recap in 40 words."}],
    }
    result = maybe_shortcut(body)
    assert result is not None
    resp, reason = result
    assert reason == "recap"
    assert resp["content"][0]["text"] == "Continuing."


def test_recap_not_triggered_on_block_content():
    # Same prefix but as a content block list (not a bare string) -> not a recap.
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "The user stepped away and is coming back. Recap"}]}
        ],
    }
    assert maybe_shortcut(body) is None


# --- strip-1m-model-suffix -----------------------------------------------------


def test_1m_suffix_stripped_from_quota_probe():
    # Exact shape observed live from claude-cli 2.1.170: the max_tokens=1
    # "quota" probe carries the [1m] alias verbatim and upstream 404s it.
    body = {
        "model": "claude-fable-5[1m]",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "quota"}],
    }
    out, applied = apply_request_transforms(body)
    assert "strip-1m-model-suffix" in applied
    assert out["model"] == "claude-fable-5"
    assert body["model"] == "claude-fable-5[1m]"  # copy-on-write


def test_plain_model_name_untouched():
    body = {"model": "claude-fable-5", "messages": [{"role": "user", "content": "hi"}]}
    out, applied = apply_request_transforms(body)
    assert "strip-1m-model-suffix" not in applied
    assert out["model"] == "claude-fable-5"


def test_1m_suffix_only_stripped_at_end():
    # A bracket elsewhere in the name is not the alias — leave it alone.
    body = {"model": "claude[1m]-custom", "messages": []}
    out, applied = apply_request_transforms(body)
    assert "strip-1m-model-suffix" not in applied
    assert out["model"] == "claude[1m]-custom"


# --- synthetic SSE round-trips through the assembler --------------------------


def test_to_sse_bytes_roundtrips_through_assembler():
    _resp, _reason = maybe_shortcut(
        {"messages": [{"role": "user", "content": "The user stepped away and is coming back. Recap"}]}
    )
    sse = to_sse_bytes(_resp)
    # Mirrors the real API: Anthropic streams end on message_stop, with no
    # OpenAI-style [DONE] sentinel.
    assert b"[DONE]" not in sse
    a = SSEAssembler()
    a.feed(sse)
    rebuilt = a.assembled()
    assert rebuilt["content"] == [{"type": "text", "text": "Continuing."}]
    assert rebuilt["stop_reason"] == "end_turn"
    assert "errors" not in rebuilt
