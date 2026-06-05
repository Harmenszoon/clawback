# claude_proxy

A small, sharp HTTP proxy that sits between Claude Code and the Anthropic
API. It forwards every request upstream — and along the way applies a
fail-open transform pipeline that removes known sources of wasted tokens,
cache-invalidating noise, and unused tool definitions. It also repairs one
client-side bug that would otherwise wedge a session (interleaved `thinking`
blocks being reordered on resend — see **Repair** below).

It also writes an exhaustive, human-readable log of every request and
response so you can see exactly what Claude Code is doing and decide
what else is worth trimming.

---

## Why this exists

Claude Code sends a very large amount of metadata, instructions, tool
definitions, and per-turn reminders on every API call. Most of it is
genuinely useful and load-bearing. Some of it is not — and the parts
that are not still cost tokens, invalidate the prompt cache, or
clutter the model's context with information that does not help it do
the user's task.

A few concrete examples we observed in real traffic and chose to address:

* **Session-title generation.** On every new session Claude Code makes a
  separate call to Opus with `effort: xhigh` to produce a 3–7 word title.
  The title is shown in the `/resume` picker. An opaque-but-unique ID
  serves the same purpose and costs zero tokens.

* **Recap-on-return.** When a user steps away from the CLI and comes
  back, Claude Code asks the model for a ~40-word summary of the
  conversation. The user can scroll up.

* **The behavioral system prompt.** ~27 KB of accumulated guidance shipped
  on every main-conversation call. Most of it is tuning for cases the
  model already handles correctly by default; a small slice (working
  directory, platform) is the actual operational context the model needs.

* **`<system-reminder>` injections.** Claude Code appends reminder blocks
  to user messages — both as stand-alone content blocks and inline
  inside `tool_result.content` strings. Their text changes (e.g. current
  date), which means they invalidate the user-message prompt cache every
  turn even when their substantive content has nothing to do with the
  conversation. The `mid-conversation-system` beta added a third envelope
  for the same material — stand-alone `role:"system"` messages in the
  `messages` array (MCP-server instructions, the skills catalog, "you
  haven't used the task tools" nudges) — which the proxy now trims too.

* **The interleaved-thinking reorder bug.** Not token waste — a correctness
  bug in Claude Code that the proxy is well-placed to paper over. With
  extended thinking + tool use, the API can return an assistant turn whose
  `thinking` blocks are interleaved among `tool_use` blocks; it signs each
  to its position and requires it back unchanged. Claude Code regroups them
  to the front when it resends the turn with tool results, and the API
  rejects the whole request with a 400, permanently wedging the session.
  See **Repair**.

* **43 tool definitions on every call.** ~70 KB of tool descriptions
  shipped on every request, including tools the user does not have any
  intention of letting the model use (Cron, NotebookEdit, Worktree, MCP
  authentication flows, etc.).

The proxy is the right place to address these because it sees the full
request as it leaves the client, can act surgically, and never has to
modify Claude Code itself.

---

## What it does

There are two kinds of intervention:

**1. Short-circuit — answer locally, never call upstream.**

| Transform | Trigger | Synthetic response |
|-----------|---------|--------------------|
| `title-gen` | A request whose `output_config.format.json_schema` requires a `title` field | `{"title": "CONVERSATION_<8 hex chars>"}` |
| `recap`     | The last user message is a string starting with `"The user stepped away and is coming back. Recap"` | `"Continuing."` |

Detection in both cases uses structural signals (schema shape, content
shape and exact prefix) — not the surrounding prose — so neither breaks
if Anthropic edits the wording of the instruction.

**2. Mutation — modify the request, then forward it.**

| Transform | What it does |
|-----------|--------------|
| `reduce-main-system` | Finds the longest cached text block in the request's `system` array, verifies it has both an env-section landmark (`# Environment` or the older sentence preamble) **and** at least two of the three operational env-field labels (`Primary working directory:`, `Platform:`, `OS Version:`), then replaces the block's text with just those three lines plus one short behavioral directive about tool selection. |
| `strip-system-reminders` | Removes Claude Code's injected reminders in all three forms. A user-message text block that is wholly a `<system-reminder>` is dropped; an embedded reminder inside `tool_result.content` (string or list of sub-blocks) is excised in place; and a stand-alone `role:"system"` message (the `mid-conversation-system` envelope) is trimmed *by relevance* — recognized transient nudges are dropped, an MCP-instructions or skills-catalog block is kept only while a tool it documents is still enabled (so re-enabling a tool restores its guidance), and anything unrecognized passes through untouched. |
| `filter-tools` | Reads `tools.json` and strips any tool whose entry is `false`. Tools the file has not seen before are auto-added with `true` and persisted, so new tools from upstream are never silently dropped. A tool pinned by `tool_choice` is always kept even if denied — dropping a forced tool would make the API reject the request, so request validity wins over the deny (and the override is logged). |

If any transform's detection misses, the request flows through unchanged.
The mutation is **never** applied speculatively. The un-stripped content
showing up in the next log is the signal that something upstream has
shifted and our detection needs an update.

**3. Repair — restore order the client corrupted.**

| Repair | What it does |
|--------|--------------|
| `restore-thinking-order` | Works around the interleaved-thinking reorder bug above (Claude Code's `ensureToolResultPairing` path). The proxy remembers each response's canonical content-block order and, when a later request resends that turn with the `thinking` blocks regrouped, reorders the request's *own* blocks back into the original positions before forwarding — so the API sees the order it signed. |

Unlike the transforms, this repair is **stateful**: it keeps a small,
session-scoped, LRU-bounded memory of recent responses (`thinking_order.py`,
owned by the handler for the process lifetime). It matches a turn only by
exact block identity (thinking signature, redacted-data hash, tool-use
id + name + canonical-input hash, text hash), reorders only — never
substitutes — and fails open on any cache miss, ambiguity, or mismatch. It
therefore can never make a request worse than the client already made it; a
proxy restart just means an already-generated turn isn't repairable until a
fresh turn is observed.

---

## Architecture at a glance

```
                       ┌────────────────┐
   claude CLI ──HTTP──▶│  ProxyHandler  │──HTTP──▶ api.anthropic.com
                       │   (server.py)  │
                       └───────┬────────┘
                               │
        ┌──────────────┬────────┴───────┬─────────────────┐
        ▼              ▼                ▼                 ▼
 maybe_shortcut  apply_request_  ThinkingOrderCache   SSEAssembler
 (transforms.py)  transforms()   (thinking_order.py)  (sse.py)
                 (transforms.py)  repair / record           │
                                                            ▼
                                                       RunLogger
                                                       (log.py)
                                                             │
                                                             ▼
                                            logs/<UTC ts>/NNN_*.{json,md}
```

Each request handler call:

1. Reads the body and tries to parse it as JSON.
2. Asks `maybe_shortcut` whether the request can be answered locally.
   If yes, emits a synthetic streaming or non-streaming response and
   records it. Returns. No upstream call is made.
3. Otherwise calls `apply_request_transforms`, then asks the
   `ThinkingOrderCache` to `restore-thinking-order` on any assistant turn
   the client has reordered (using the canonical order remembered from a
   prior response). If anything ran, the modified body is re-serialized and
   replaces the original bytes.
4. Forwards the request upstream. The response is streamed to the
   client byte-for-byte. An `SSEAssembler` runs in parallel against the
   same stream so the log can capture the response as a single
   assembled message dict rather than a thousand SSE events — and that
   assembled message's block order is recorded for the repair in step 3.
5. The `RunLogger` writes both a structured JSON record and a paired
   human-readable markdown rendering, then appends a one-line summary to
   `index.jsonl`.

Everything is async. Log writes are dispatched to a worker thread (and
spawned as fire-and-forget tasks so responses are never blocked on disk
I/O). `tools.json` reads happen inline on the event loop — sub-millisecond
against the OS file cache, and the simplicity is worth it for a
single-user proxy. See "Trade-offs we deliberately accepted" below.

---

## Design principles

These principles inform every transform and every choice about what to
strip versus what to leave alone.

**Detect structurally, never on prose.** Tool names, JSON schema shape,
Markdown heading landmarks, and tag boundaries are stable contracts. The
sentence around them can be rewritten at any time. We always pick a
signal that survives a rewording.

**Fail open, never silently break.** A transform that cannot find what it
expects must return the request unchanged. The cost of a missed
optimization (paying full price for one request) is much lower than the
cost of a silent corruption (model loses real context and behaves
oddly). The un-stripped content reappearing in the log is the
self-healing signal that detection needs updating.

**Cache prefix consistency.** Some transforms (`strip-system-reminders`
in particular) must be applied to *every* user message in *every* turn,
including the conversation history. If we stripped the latest turn but
left history's reminders in place, the historical messages would
re-appear in the next request with their original (unstripped) content,
and the prompt cache would miss on the boundary every turn. We strip
from history too so the cached prefix is deterministic.

**Copy-on-write.** Mutating transforms never modify the dict they receive.
They build a new dict for any container they need to change and leave the
rest aliased. The caller's record of the original request body — used
when writing the log entry — is preserved unchanged.

**The user is the policy.** The choice of *which* tools to allow is
controlled entirely by `tools.json`, which the user owns and edits at
their leisure. The proxy does not pre-judge: any tool name it has not
seen before defaults to `true` and is added to the file for the user
to inspect.

**Exhaustive logs, by default.** Every request gets a complete record
(headers, full body, response body assembled from SSE). Per-request
markdown files make the records reviewable without needing tooling. The
goal is for the operator to be able to answer "what is Claude Code
actually sending?" by reading a single file.

---

## What gets logged

Each proxy startup creates a fresh `logs/<UTC-timestamp>/` directory. For
every request the proxy handles, four artifacts may be written:

| File | Purpose |
|------|---------|
| `NNN_<path-slug>.json` | Full structured record: timestamp, elapsed time, method, path, sanitized request headers, request body **as forwarded** (post-transform), response status, sanitized response headers, response body (parsed JSON for non-streaming, assembled message dict for SSE). For SSE, out-of-band events outside the normal message lifecycle are captured too — `error` events and any unrecognized event shape are recorded on the assembled message, so an error-only stream is never logged as an empty success. Lossless source of truth, modulo the log-only redactions noted under **Redaction**. |
| `NNN_<path-slug>.md` | Paired human-readable rendering of the same record: outlined sections, system blocks sized and cache-flagged, tool index table plus per-tool details, messages with each content block annotated, response with usage breakdown. This is the file to open first when reviewing. |
| `index.jsonl` (single, append-only) | One JSON line per request: seq, ts, elapsed, method, path, status, model, stream flag, stop_reason, usage, plus `short_circuited` / `transforms_applied` markers when relevant. |
| `console.log` (single, per run) | Captured stdout/stderr for the entire run. |

The header at the top of every markdown record explicitly flags whether
the request was short-circuited locally or which transforms were applied
before forwarding, so a glance answers "did the proxy intervene here?"

### Inspecting

```bash
ls logs/                                # runs, sorted chronologically
cat logs/<run>/index.jsonl              # one-line-per-request overview
cat logs/<run>/002_messages.md          # detailed reading of one request
```

If you change `render.py` and want to rebuild the markdown views for an
existing run:

```bash
python -m claude_proxy.render logs/<run>/        # whole directory
python -m claude_proxy.render logs/<run>/002_messages.json   # one file
```

The `.json` files are the source of truth (the parsed request/response with
log-only redactions applied — see **Redaction**); the `.md` files can always
be regenerated from them.

### Redaction

The following are replaced with `<redacted>` in the log files only — the
real values are always forwarded upstream unchanged:

* **Headers:** `Authorization`, `x-api-key`, `Cookie`, `Set-Cookie`,
  `Proxy-Authorization`, and `X-Claude-Code-Session-Id`.
* **Request body:** `metadata.user_id` — the telemetry blob carrying
  `device_id`, `account_uuid`, and `session_id`. Upstream still receives the
  real value (billing and rate-limit reconciliation depend on it); only the
  on-disk copy is scrubbed, since that is the one likely to be pasted, zipped,
  or shared. The whole value is replaced rather than parsed — the device and
  account ids are more durable identifiers than the session id, so a
  half-redaction would be little better than none.

Redaction is the only place the logged request *content* diverges from what
was forwarded (the body is otherwise the full parsed request, re-serialized).
The logs still contain your prompts, file contents, and command output, so
redaction makes them *tidier*, not *safe to publish* — review before sharing.

---

## Tool filtering — `tools.json`

A JSON object mapping tool name to a boolean. `true` allows the tool;
`false` strips it from the forwarded request.

```json
{
  "Bash": false,
  "PowerShell": true,
  "Read": true,
  "CronCreate": false,
  ...
}
```

* The file is at the project root, named `tools.json`.
* It is read on **every request** — file I/O is microseconds; the
  upstream call is seconds — so edits take effect on the very next
  request. No proxy restart required.
* On the first request to ever reach a fresh proxy install, the file
  does not exist yet. The proxy creates it, populating it with every
  tool name from that request set to `true`. The user then edits the
  file to disable whatever they want.
* When a request contains a tool name not in the file, the proxy
  defaults it to `true` and appends it to the file. **New tools never
  break silently.** They appear in `tools.json` so the user can decide.
* Writes are atomic (write to `tools.json.tmp` then `os.replace`) so a
  concurrent read can never observe a half-written file.
* If the file is missing, malformed, or unreadable, the proxy falls
  back to allow-all for that request and prints a one-line warning. A
  malformed file will **not** be silently overwritten — the proxy
  refuses to persist new discoveries until the file is valid again, so a
  typo cannot wipe the user's careful allow/deny config.
* The value schema is forward-compatible. The loader accepts both bare
  `true`/`false` and `{"allow": true}` object form, so future per-tool
  metadata (description overrides, parameter restrictions, etc.) can be
  added without breaking existing config files.
* A tool that the request **forces** via `tool_choice`
  (`{"type":"tool","name":...}`) is kept even when `tools.json` denies it.
  Stripping a tool while leaving the request pinning it by name makes the
  API return a 400 — a proxy-manufactured failure, which the fail-open
  contract forbids. Your deny is honored on a best-effort basis; a valid
  request takes precedence, and the override is logged to the console.

Your real `tools.json` is gitignored (it ends up holding your own MCP servers
and OS-specific choices). A `tools.json.example` is checked in as a lean
starting point: the file/search core (`Edit`, `Glob`, `Grep`, `Read`,
`Write`), `PowerShell`, and the web tools (`WebFetch`, `WebSearch`, the
`web_search` server tool) enabled — with the agent/cron/task/worktree/skill/
notebook surface disabled. Treat it as a template, not a prescription:
`PowerShell` reflects a Windows host (swap in `Bash` elsewhere), and it lists
no `mcp__*` entries because those are specific to your own MCP setup — they're
auto-added with `true` the first time the proxy sees them, so you can decide.
Copy it to `tools.json` (or just let the proxy create one on the first
request), then edit; changes take effect on the very next request.

---

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env                 # optional — the defaults usually work
cp tools.json.example tools.json     # optional — the proxy creates one if absent
```

The only runtime dependencies are `aiohttp` for the HTTP server/client
and `certifi` for the TLS CA bundle. The package is also pip-installable
(`pip install .`), which exposes a `claude-proxy` console command equivalent
to `python -m claude_proxy`.

---

## Run

```bash
python -m claude_proxy
```

You should see:

```
Proxy running on http://localhost:3456
Forwarding to https://api.anthropic.com
Logs:        /path/to/logs/2026-05-28T01-24-56
```

Then point Claude Code at it:

PowerShell:
```powershell
$env:ANTHROPIC_BASE_URL = "http://localhost:3456"
claude
```

bash/zsh:
```bash
export ANTHROPIC_BASE_URL=http://localhost:3456
claude
```

`Ctrl+C` stops the proxy.

---

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite covers the pure, highest-risk logic: tool filtering (allow/deny,
discovery, and the `tool_choice` reconciliation), the `SSEAssembler` (error /
unknown event capture and UTF-8 chunk-boundary handling), and
strip-system-reminders. No network or running proxy is required.

---

## Configuration

All settings are environment variables, optionally loaded from a `.env`
file in the project root.

| Variable           | Default                       | Description |
|--------------------|-------------------------------|-------------|
| `PROXY_HOST`       | `localhost`                   | Bind address |
| `PROXY_PORT`       | `3456`                        | Listen port |
| `PROXY_TARGET_URL` | `https://api.anthropic.com`   | Upstream API endpoint |

---

## Project layout

```
claude_proxy/
  __init__.py        package metadata + version
  __main__.py        entry point; tees stdout/stderr into the run's console.log
  config.py          env loading, host/port/target, header strip lists
  server.py          aiohttp app + the request handler that drives the pipeline
  transforms.py      short-circuit + mutation transforms (title-gen, recap,
                     reduce-main-system, strip-system-reminders, filter-tools)
  thinking_order.py  stateful repair: remembers canonical block order, undoes
                     the client's interleaved-thinking reordering on resend
  tool_filter.py     dynamic `tools.json` allowlist with atomic writes
  sse.py             SSE stream parser — observation-only, used for the log
  log.py             RunLogger: per-run dir, file-per-request, index.jsonl
  render.py          JSON record → human-readable markdown view
tests/               pytest unit tests for the pure transform/parse logic
pyproject.toml       packaging metadata, console entry point, pytest config
requirements.txt     runtime dependencies
requirements-dev.txt runtime + test dependencies
CHANGELOG.md         notable changes per version
tools.json.example   template allowlist (checked in)
tools.json           per-user allow/deny config (gitignored, auto-created)
.env.example         template config (checked in)
.env                 local secrets (gitignored)
logs/                runtime artifacts (gitignored)
```

---

## Trade-offs we deliberately accepted

These are choices we considered, evaluated, and chose to live with. Future
contributors should know about them so they understand why the code looks
the way it does.

* **Single-user scope.** The proxy serves one developer's Claude Code
  CLI. We do not coordinate across concurrent processes, do not lock
  log writes, and do not strictly serialize the read-modify-write of
  `tools.json`. In a real concurrent setting two simultaneous "new
  tool" discoveries could race and lose one update. For the realistic
  workload (one CLI, requests issued sequentially) the simpler shape is
  the right one.

* **Partial test coverage.** `tests/` covers the highest-risk pure logic —
  tool filtering (including the `tool_choice` reconciliation), the
  `SSEAssembler` (out-of-band events and UTF-8 chunk boundaries), and
  strip-system-reminders — but the async server handler and the stateful
  thinking-order repair are still validated manually against real traffic.
  Extending coverage to those is the next step.

* **`metadata.user_id` is forwarded but log-redacted.** Anthropic uses
  this for billing reconciliation and rate-limit attribution, so the proxy
  passes the real value upstream unchanged — but redacts it in the on-disk
  log (see **Redaction**). The forwarded request and the logged request
  therefore differ in exactly this one field (plus the redacted headers).

* **Disk reads on the event loop for `tools.json`.** We read the file
  from inside the request handler coroutine on every request. Reads
  are sub-millisecond against the OS file cache; the upstream API call
  takes seconds. Caching the config in memory was considered and
  rejected because it would introduce a "is the file or the memory the
  truth?" question and would prevent edit-and-it-takes-effect-now
  behavior.

* **The behavioral instruction in the reduced system prompt names
  shells but not file tools.** When the original 27 KB behavioral
  prompt is removed, the model occasionally reaches for shell idioms
  (`cat`, `Get-Content`) when a dedicated file tool is available. We
  paste in one sentence to discourage that, but we deliberately do
  *not* name specific tools (Read, Glob, etc.) in the rule because the
  user may have disabled some of them via `tools.json`. The rule reads
  as a conditional: shells are forbidden *when* another available tool
  handles the operation.

* **Compaction (`/compact`) is not short-circuited.** Compaction
  produces a substantive summary that Claude Code uses when context
  fills up. It is one of the few aux calls whose output is genuinely
  load-bearing.

---

## What we observed but did not (yet) handle

These showed up in real traffic and were noted as candidates for future
work:

* **`count_tokens` calls.** Claude Code occasionally hits
  `/v1/messages/count_tokens` to measure the size of a file before
  acting on it. The endpoint is cheap (no inference, just a token
  count) and our transforms correctly do not fire on it. Could be
  short-circuited locally with `tiktoken` or similar; the savings are
  small.

* **Stripping `metadata.user_id` from the forwarded request.** We log-redact
  it (see **Redaction**) but still forward the real value, because billing
  reconciliation depends on it. Dropping it from the upstream request entirely
  is possible but would forfeit per-device attribution; we have not done it.

---

## Extending it

**Adding a new short-circuit.** Write a `_is_<something>(body)`
predicate and a `_synthesize_<something>_response(body)` builder in
`transforms.py`, then add a third clause to `maybe_shortcut`. The
detection should pick the most stable structural signal in the request
shape, never prose. Test against a captured log entry before relying
on it.

**Adding a new mutation.** Write a function that takes the body and
returns either the modified body or `None` (no-op). Chain it into
`apply_request_transforms`. Use copy-on-write — do not mutate the
input. Use the same fail-open pattern: if anything you expect to find
is missing, return `None`.

**Adding per-tool metadata.** `tool_filter._coerce_allow` already
accepts both `bool` and `{"allow": bool, ...}` values, so the schema
can grow without breaking existing config files. Add the new field,
read it in `filter_tools` where the tool's description or schema is
about to be passed through.

---

## Acknowledgements

This project replaces an earlier prototype that hard-coded its tool
list and prompt rewrites inline. The current shape — fail-open
detection, copy-on-write transforms, dynamic `tools.json`,
per-request file-and-markdown logs — is the result of iterating
against real Claude Code traffic and observing what actually broke
when we got it wrong.

---

## License

Released into the public domain under [The Unlicense](LICENSE). Do whatever
you want with it — copy, modify, sell, redistribute — no attribution required.
