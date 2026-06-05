"""Dynamic tool allowlist backed by `tools.json` at the project root.

The proxy reads `tools.json` on every request. The file is a JSON object
mapping tool name to a boolean — `true` allows the tool to pass through,
`false` strips it. User edits are picked up by the next request, no restart
required.

The cost of re-reading is microseconds; the upstream API call is seconds.
Caching the config in memory would buy nothing and introduce a "is the
file or the memory the truth?" question. Per-request reads keep the file
as the single source of truth.

# Discovery

When the proxy sees a tool name not in the config, it adds the tool with
`true` (allow) and persists the file atomically. The user can flip it to
`false` later. Default-allow means a new tool we've never seen never
breaks silently — it just appears in the file for review.

# Atomic writes

Writes go to `tools.json.tmp` then `os.replace()`, which is atomic on
POSIX and Windows. A concurrent read can only observe the old or new
file, never a half-written one.

# Fail-soft

If the file is missing, malformed, or unreadable, the filter falls back
to allow-all for that request and prints a one-line warning. The next
successful read restores normal behavior. The proxy never refuses a
request because the config is broken.

# Forward-compatible schema

Today's values are bare bools. Future per-tool metadata can replace the
bool with `{"allow": bool, ...}` without breaking existing config files —
the loader accepts both shapes.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .config import PROJECT_ROOT

_CONFIG_PATH = PROJECT_ROOT / "tools.json"
_TMP_PATH = PROJECT_ROOT / "tools.json.tmp"


def filter_tools(body: dict) -> tuple[dict, list[str], list[str]] | None:
    """Filter `body["tools"]` against the on-disk allowlist.

    Returns `(modified_body, dropped, discovered)` if anything was dropped
    or any tool was newly discovered (and persisted), else `None` (forward the
    original body unchanged). `None` is also returned on the fail-open path
    below — when filtering would leave a `tool_choice` unsatisfiable — even if a
    tool was discovered: the discovery is still persisted, but the request is
    forwarded untouched.

    A "discovered" tool is one whose name was absent from the config and
    has just been added with `allow=true`. The caller treats either kind
    of change as "transform fired."

    A tool pinned by `tool_choice: {"type":"tool","name":...}` is always kept,
    even when the allowlist denies it: dropping a tool while leaving the request
    forcing it produces an upstream 400 (a proxy-manufactured failure, which the
    fail-open contract forbids). The user's deny is honored on a best-effort
    basis — a valid request wins over it — and the override is logged.
    """
    if not isinstance(body, dict):
        return None
    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        return None

    config, invalid_names, ok_to_persist = _load()
    forced = _forced_tool_name(body)

    kept: list = []
    dropped: list[str] = []
    discovered: list[str] = []
    forced_overrides: list[str] = []

    for tool in tools:
        if not isinstance(tool, dict):
            kept.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            kept.append(tool)
            continue

        # Resolve the allow/deny decision first.
        if name in invalid_names:
            # An entry exists in the file with an unparseable value (e.g. a typo
            # like {"allow": "false"}). Conservative choice: treat as denied,
            # do not auto-discover, do not overwrite the user's typo. The user
            # sees the warning printed during _load and fixes the value;
            # meanwhile the tool is effectively denied — closer to the user's
            # likely intent than silently allowing it.
            allow = False
        else:
            if name not in config:
                discovered.append(name)
                config[name] = True
            allow = config[name]

        # A tool the request forces via `tool_choice` must keep its definition
        # regardless of the allowlist, otherwise the forwarded request names a
        # tool the proxy just removed and the API rejects it with a 400. Honor
        # the deny on a best-effort basis; request validity takes precedence.
        if not allow and name == forced:
            allow = True
            forced_overrides.append(name)

        if allow:
            kept.append(tool)
        else:
            dropped.append(name)

    # Persist discoveries first. Recording a newly-seen tool is independent of
    # whether this request ends up filtered or fails open below, so it must not
    # be skipped on the fail-open paths. Only persist when the on-disk file is
    # fully clean: if anything was unreadable, malformed, or had invalid
    # entries, writing back would either lose the user's allow/deny config or
    # silently rewrite their typo as a definitive `true`, so persistence is
    # paused until the file is valid again.
    if discovered and ok_to_persist:
        _save(config)
        for name in discovered:
            print(f"  Discovered new tool: {name} (defaulted to allow in tools.json)", flush=True)

    # If what we'd forward can't satisfy a `tool_choice` that requires a tool
    # call, don't forward it — return the request untouched. This covers two
    # shapes: "any" with every tool denied (an empty list the model must yet
    # choose from), and an explicit "tool" whose named tool won't be present in
    # the forwarded set. A forced tool that *was* in the list is already kept
    # above, so the only way it's absent is that the client itself named a tool
    # it never sent; forwarding the original (rather than our filtered body) at
    # least keeps the proxy out of the failure.
    forced_absent = forced is not None and not any(isinstance(t, dict) and t.get("name") == forced for t in kept)
    if _requires_tool_use(body) and (not kept or forced_absent):
        requirement = f"tool {forced!r}" if forced else "a tool"
        print(
            f"  Note: filtering would leave tool_choice ({requirement}) "
            "unsatisfiable; forwarding the original request to avoid a 400",
            flush=True,
        )
        return None

    for name in forced_overrides:
        print(
            f"  Note: tool {name!r} is disabled in tools.json but pinned by "
            "tool_choice; keeping its definition to avoid an upstream 400",
            flush=True,
        )

    if not dropped and not discovered:
        return None

    return {**body, "tools": kept}, dropped, discovered


def _forced_tool_name(body: dict) -> str | None:
    """Return the tool name pinned by `tool_choice`, or None.

    Only an explicit `{"type":"tool","name":...}` choice forces a specific tool.
    `auto`, `any`, and `none` name no tool, so a denied tool can be dropped
    freely under those — only the explicit form risks the dropped-but-forced 400.
    """
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "tool":
        name = choice.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _requires_tool_use(body: dict) -> bool:
    """True iff `tool_choice` obliges the model to call a tool.

    `any` ("use one of the provided tools") and `tool` ("use this tool") both
    become unsatisfiable — an upstream 400 — if filtering leaves no tools.
    `auto`, `none`, and an absent `tool_choice` tolerate an empty tool list.
    """
    choice = body.get("tool_choice")
    return isinstance(choice, dict) and choice.get("type") in ("any", "tool")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _load() -> tuple[dict[str, bool], set[str], bool]:
    """Read tools.json.

    Returns `(config, invalid_names, ok_to_persist)`:

      * `config`: `{name: allow_bool}` for entries that parsed cleanly.
      * `invalid_names`: names that are present in the file but with a value
        we cannot interpret (a typo like `{"allow": "false"}`, a stray
        number, etc.). These names are deliberately left out of `config`
        and the caller treats them as denied — closer to the user's likely
        intent than silently allowing a tool they tried to deny.
      * `ok_to_persist`: True only when the file was either absent or
        successfully parsed end-to-end with no invalid entries. When False,
        the caller must not write the file back, because doing so would
        either destroy the user's allow/deny config (unreadable/malformed
        file) or silently rewrite their typo as a definitive `true`
        (invalid entry).
    """
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, set(), True
    except OSError as exc:
        print(
            f"  WARN: tools.json read failed ({exc}); allowing all tools this turn "
            "and refusing to persist (file may be locked or transiently unreadable)",
            flush=True,
        )
        return {}, set(), False

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(
            f"  WARN: tools.json malformed ({exc}); allowing all tools this turn "
            "and refusing to persist (fix the file to avoid losing config)",
            flush=True,
        )
        return {}, set(), False

    if not isinstance(data, dict):
        print(
            f"  WARN: tools.json must be a JSON object, got {type(data).__name__}; "
            "allowing all tools this turn and refusing to persist",
            flush=True,
        )
        return {}, set(), False

    result: dict[str, bool] = {}
    invalid_names: set[str] = set()
    for name, value in data.items():
        if not isinstance(name, str):
            continue
        allow = _coerce_allow(value)
        if allow is None:
            # Preserve the name as "invalid" so the caller drops the tool
            # from the request (effective deny) and avoids the discovery
            # path that would otherwise overwrite the typo as `true`. The
            # entry stays in the file untouched until the user fixes it.
            invalid_names.add(name)
            print(
                f"  WARN: tools.json: entry {name!r} has invalid value {value!r} "
                '(expected true, false, or {"allow": true|false}); treating '
                "tool as denied and refusing to persist until fixed",
                flush=True,
            )
            continue
        result[name] = allow
    return result, invalid_names, not invalid_names


def _coerce_allow(value: Any) -> bool | None:
    """Strict coercion: `True`/`False` or `{"allow": <bool>}`. None on anything else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        inner = value.get("allow")
        if isinstance(inner, bool):
            return inner
    return None


def _save(config: dict[str, bool]) -> None:
    """Atomically replace tools.json with the new config."""
    payload = json.dumps(config, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        _TMP_PATH.write_text(payload, encoding="utf-8")
        os.replace(_TMP_PATH, _CONFIG_PATH)
    except OSError as exc:
        print(f"  WARN: tools.json write failed ({exc})", flush=True)
