"""Tests for the dynamic tool allowlist, focused on the `tool_choice`
reconciliation fix: a forced tool must keep its definition even when denied,
so the proxy never manufactures an upstream 400.
"""

from __future__ import annotations

import json

import pytest

from clawback import tool_filter


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Redirect tool_filter at an isolated tools.json under tmp_path."""
    path = tmp_path / "tools.json"
    monkeypatch.setattr(tool_filter, "_CONFIG_PATH", path)
    monkeypatch.setattr(tool_filter, "_TMP_PATH", tmp_path / "tools.json.tmp")

    def write(mapping: dict) -> None:
        path.write_text(json.dumps(mapping), encoding="utf-8")

    return write, path


def _body(tools, tool_choice=None):
    body = {"tools": [{"name": n} for n in tools]}
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _names(kept):
    return [t["name"] for t in kept]


def test_denied_tool_is_dropped(cfg):
    write, _ = cfg
    write({"Read": True, "Bash": False})

    result = tool_filter.filter_tools(_body(["Read", "Bash"]))
    assert result is not None
    new_body, dropped, discovered = result
    assert _names(new_body["tools"]) == ["Read"]
    assert dropped == ["Bash"]


def test_forced_tool_kept_despite_deny(cfg):
    """The core fix: tool_choice pins a denied tool -> it must survive."""
    write, _ = cfg
    write({"web_search": False, "Read": True})

    body = _body(
        ["web_search", "Read"],
        tool_choice={"type": "tool", "name": "web_search"},
    )
    result = tool_filter.filter_tools(body)

    # Nothing dropped or discovered -> filter is a no-op, original body forwards
    # unchanged WITH web_search still present. That is exactly the fail-open win:
    # the request stays valid.
    assert result is None


def test_forced_tool_kept_while_other_tools_dropped(cfg):
    write, _ = cfg
    write({"web_search": False, "Bash": False, "Read": True})

    body = _body(
        ["web_search", "Bash", "Read"],
        tool_choice={"type": "tool", "name": "web_search"},
    )
    result = tool_filter.filter_tools(body)
    assert result is not None
    new_body, dropped, _discovered = result
    # web_search forced -> kept; Bash denied & unforced -> dropped.
    assert "web_search" in _names(new_body["tools"])
    assert "Read" in _names(new_body["tools"])
    assert dropped == ["Bash"]


def test_auto_choice_does_not_protect_denied_tool(cfg):
    write, _ = cfg
    write({"web_search": False, "Read": True})

    body = _body(["web_search", "Read"], tool_choice={"type": "auto"})
    result = tool_filter.filter_tools(body)
    assert result is not None
    new_body, dropped, _ = result
    assert _names(new_body["tools"]) == ["Read"]
    assert dropped == ["web_search"]


def test_forced_tool_with_invalid_config_value_kept(cfg):
    """An unparseable entry is treated as denied, but forcing still wins."""
    write, _ = cfg
    write({"web_search": "false", "Read": True})  # typo: string, not bool

    body = _body(["web_search", "Read"], tool_choice={"type": "tool", "name": "web_search"})
    result = tool_filter.filter_tools(body)
    # web_search invalid->denied but forced->kept; nothing actually dropped.
    assert result is None


def test_new_tool_discovered_and_persisted(cfg):
    write, path = cfg
    write({"Read": True})

    result = tool_filter.filter_tools(_body(["Read", "BrandNew"]))
    assert result is not None
    _new_body, dropped, discovered = result
    assert dropped == []
    assert discovered == ["BrandNew"]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["BrandNew"] is True


def test_choice_any_fails_open_when_all_tools_denied(cfg):
    """tool_choice 'any' requires the model to call a tool; emptying the list
    would 400, so the filter must fail open and forward the request unchanged."""
    write, _ = cfg
    write({"Read": False, "Bash": False})

    body = _body(["Read", "Bash"], tool_choice={"type": "any"})
    result = tool_filter.filter_tools(body)
    assert result is None  # no-op -> original (full) tool list forwarded


def test_choice_any_still_filters_when_some_tool_survives(cfg):
    write, _ = cfg
    write({"Read": True, "Bash": False})

    body = _body(["Read", "Bash"], tool_choice={"type": "any"})
    result = tool_filter.filter_tools(body)
    assert result is not None
    new_body, dropped, _ = result
    assert _names(new_body["tools"]) == ["Read"]
    assert dropped == ["Bash"]


def test_explicit_tool_choice_naming_absent_tool_fails_open(cfg):
    """If tool_choice pins a tool that won't be in the forwarded set (here it
    was never sent), forward the original request rather than a filtered body
    that is still unsatisfiable — keep the proxy out of the failure."""
    write, _ = cfg
    write({"Bash": False, "Read": True})

    body = _body(["Bash", "Read"], tool_choice={"type": "tool", "name": "Missing"})
    result = tool_filter.filter_tools(body)
    assert result is None  # left untouched despite Bash being denied


def test_discovery_persists_even_when_failing_open(cfg):
    """A new tool seen on a request that then fails open (unsatisfiable
    tool_choice) must still be recorded — discovery is independent of forwarding."""
    write, path = cfg
    write({"Bash": False})

    body = _body(["Bash", "BrandNew"], tool_choice={"type": "tool", "name": "Missing"})
    result = tool_filter.filter_tools(body)
    assert result is None  # fail open: forced tool absent
    # ...but BrandNew was still discovered and persisted.
    assert json.loads(path.read_text(encoding="utf-8"))["BrandNew"] is True


def test_choice_auto_allows_emptying_the_tool_list(cfg):
    """'auto' does not oblige a tool call, so denying everything is fine."""
    write, _ = cfg
    write({"Read": False})

    body = _body(["Read"], tool_choice={"type": "auto"})
    result = tool_filter.filter_tools(body)
    assert result is not None
    new_body, dropped, _ = result
    assert new_body["tools"] == []
    assert dropped == ["Read"]


def test_forced_new_tool_is_kept_and_discovered(cfg):
    write, path = cfg
    write({"Read": True})

    body = _body(["BrandNew"], tool_choice={"type": "tool", "name": "BrandNew"})
    result = tool_filter.filter_tools(body)
    assert result is not None
    _new_body, _dropped, discovered = result
    assert discovered == ["BrandNew"]
    assert json.loads(path.read_text(encoding="utf-8"))["BrandNew"] is True
