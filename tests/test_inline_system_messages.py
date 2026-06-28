"""Tests for form-3 reminder handling: stand-alone `role:"system"` messages
injected into the messages array (the mid-conversation-system beta).

The contract under test, keyed off the post-filter tool list:

  * recognized transient nudges                      -> dropped whole;
  * `## <server>` MCP subsections                    -> kept only while that
                                                        server has an enabled tool;
  * the skills catalog                               -> kept only while `Skill`
                                                        is enabled;
  * anything unrecognized                            -> kept verbatim;
  * no `tools` field / ToolSearch present (deferred
    tools — the array is not the full reachable set) -> keep tool guidance;
  * catalog-before-MCP layout (unexpected shape)     -> kept verbatim.
"""

from __future__ import annotations

import pytest

from clawback import tool_filter
from clawback.transforms import _NARRATION_TAIL_GUARD, apply_request_transforms


@pytest.fixture(autouse=True)
def _isolated_tools_json(tmp_path, monkeypatch):
    """These tests drive the full pipeline, and filter-tools runs before the
    stripper — without isolation it would read (and write discoveries into)
    the developer's real tools.json. An empty config means allow-all, so the
    enabled set is exactly each test's tools list."""
    monkeypatch.setattr(tool_filter, "_CONFIG_PATH", tmp_path / "tools.json")
    monkeypatch.setattr(tool_filter, "_TMP_PATH", tmp_path / "tools.json.tmp")


MCP_TEXT = (
    "# MCP Server Instructions\n"
    "\n"
    "Instructions from connected MCP servers.\n"
    "\n"
    "## codex\n"
    "Codex guidance line.\n"
    "\n"
    "## ida-pro\n"
    "IDA guidance line.\n"
)

SKILLS_TEXT = (
    "The following skills are available for use with the Skill tool:\n"
    "- pdf-tools: extract text and tables from PDF files\n"
)

NUDGE = "The task tools haven't been used recently. Consider using TaskCreate."


def _transform(system_content: str, tool_names: list[str] | None):
    body: dict = {
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "hi"},
        ],
    }
    if tool_names is not None:
        body["tools"] = [{"name": n} for n in tool_names]
    return apply_request_transforms(body)


def _system_messages(body: dict) -> list[str]:
    # Exclude the no-narration tail guard (a separate transform appends it as a
    # trailing role:"system" message); these tests cover form-3 reminder
    # stripping only.
    return [
        m["content"]
        for m in body["messages"]
        if m.get("role") == "system" and m["content"] != _NARRATION_TAIL_GUARD
    ]


def test_known_nudge_message_dropped():
    new_body, applied = _transform(NUDGE, ["Read"])
    assert "strip-system-reminders" in applied
    assert _system_messages(new_body) == []
    # user message intact (the tail guard, if any, is appended after it)
    assert any(m.get("role") == "user" and m.get("content") == "hi" for m in new_body["messages"])


def test_mcp_sections_trimmed_to_enabled_servers():
    new_body, applied = _transform(MCP_TEXT, ["mcp__codex__codex", "Read"])
    assert "strip-system-reminders" in applied
    (content,) = _system_messages(new_body)
    assert "## codex" in content
    assert "Codex guidance line." in content
    assert "## ida-pro" not in content
    assert content.startswith("# MCP Server Instructions")  # preamble preserved


def test_mcp_block_dropped_when_every_server_disabled():
    new_body, applied = _transform(MCP_TEXT, ["Read"])
    assert "strip-system-reminders" in applied
    assert _system_messages(new_body) == []


def test_skills_catalog_gated_on_skill_tool():
    combined = MCP_TEXT + "\n" + SKILLS_TEXT

    with_skill, _ = _transform(combined, ["mcp__codex__codex", "Skill"])
    (content,) = _system_messages(with_skill)
    assert "available for use with the Skill tool" in content
    assert "## codex" in content

    without_skill, applied = _transform(combined, ["mcp__codex__codex"])
    assert "strip-system-reminders" in applied
    (content,) = _system_messages(without_skill)
    assert "available for use with the Skill tool" not in content
    assert "## codex" in content  # MCP half survives independently


def test_unrecognized_system_message_kept_verbatim():
    note = "Some new harness note Clawback has never seen before."
    new_body, applied = _transform(note, ["Read"])
    assert "strip-system-reminders" not in applied
    assert _system_messages(new_body) == [note]


def test_missing_tools_field_keeps_tool_guidance():
    """No `tools` in the request at all -> the enabled set is unknown -> every
    server section must survive (we only drop what we can prove dead)."""
    new_body, _applied = _transform(MCP_TEXT, None)
    (content,) = _system_messages(new_body)
    assert "## codex" in content
    assert "## ida-pro" in content


def test_toolsearch_presence_keeps_mcp_sections():
    """Deferred tools: with ToolSearch in the request, an MCP server's absence
    from the tools array no longer proves it is unreachable, so its
    instructions must be kept."""
    new_body, _applied = _transform(MCP_TEXT, ["ToolSearch", "Read"])
    (content,) = _system_messages(new_body)
    assert "## codex" in content
    assert "## ida-pro" in content


def test_catalog_before_mcp_layout_fails_open():
    """If the skills-catalog marker appears *before* the MCP heading, the
    assumed layout doesn't hold; gating the combined half on `Skill` alone
    could drop MCP guidance for enabled servers. Keep the message verbatim."""
    flipped = SKILLS_TEXT + "\n" + MCP_TEXT
    new_body, applied = _transform(flipped, ["mcp__codex__codex"])
    assert "strip-system-reminders" not in applied
    assert _system_messages(new_body) == [flipped]
