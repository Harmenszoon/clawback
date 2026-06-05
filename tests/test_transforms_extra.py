"""Coverage for the transforms not exercised elsewhere: reduce-main-system,
the title-gen / recap short-circuits, and the synthetic SSE round-trip.
"""

from __future__ import annotations

import copy
import json

from claude_proxy.sse import SSEAssembler
from claude_proxy.transforms import (
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
    assert len(new_text) < 600  # the 27K block is gone
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


# --- synthetic SSE round-trips through the assembler --------------------------


def test_to_sse_bytes_roundtrips_through_assembler():
    _resp, _reason = maybe_shortcut(
        {"messages": [{"role": "user", "content": "The user stepped away and is coming back. Recap"}]}
    )
    sse = to_sse_bytes(_resp)
    a = SSEAssembler()
    a.feed(sse)
    rebuilt = a.assembled()
    assert rebuilt["content"] == [{"type": "text", "text": "Continuing."}]
    assert rebuilt["stop_reason"] == "end_turn"
    assert "errors" not in rebuilt
