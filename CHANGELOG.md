# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[semantic versioning](https://semver.org/).

## [0.6.0] — 2026-06-09

Hardening pass driven by a full code/architecture review (including an
independent second-opinion review): every fail-open gap found was closed,
and the proxy now measures its own savings.

### Fixed
- **Reminder stripping can no longer produce empty content blocks.** A
  `tool_result` text sub-block that is *wholly* a reminder is now dropped
  from the list (the API rejects empty text blocks); if excision would empty
  the whole list — or reduce a string content to nothing — the block is left
  untouched instead. Closes the one path where stripping could have
  manufactured an upstream 400.
- **`title-gen` detection tightened.** The schema must now be *exactly*
  `{"title"}`. A future Claude Code call asking for `{"title", "description"}`
  (a PR title, a plan title…) is a different feature and no longer gets
  hijacked with a synthetic `CONVERSATION_<hex>`.
- **Mid-stream upstream failures are no longer mislogged as proxy 502s.**
  Once the 200/SSE status line has gone out, a failure can't become an HTTP
  error; the truncated stream is finalized as-is and the record now shows the
  truth — status 200 plus an explicit `proxy_stream_interrupted` error — and
  the partial turn is never recorded as canonical by the thinking-order cache.
- **A cache breakpoint riding a dropped tool is preserved.** When
  `filter-tools` removes the last tool and it carried `cache_control`, the
  marker moves to the new last kept tool instead of silently turning every
  subsequent request into a full prompt-cache miss.
- **Deferred tools (ToolSearch) no longer cause over-trimming.** With
  `ToolSearch` present, the request's tools array is not the full reachable
  set, so MCP-instructions sections and the skills catalog are kept rather
  than dropped for servers whose tools are merely deferred.
- **Skills-catalog-before-MCP layout fails open.** If the catalog marker
  appears before the `# MCP Server Instructions` heading (a layout change),
  the message is kept verbatim instead of gambling on the split.

### Changed
- **The no-narration steer moved to a recency tail.** It rode the reduced
  system block before; now a new `inject-narration-tail` transform appends it
  as a tiny `role:"system"` message at the end of `messages`, re-stamped every
  turn just before generation. Recency is the lever — cached ~100K tokens
  behind the conversation it faded on long sessions; at the tail it holds
  (~3% narration vs ~12% cached vs ~28% unguided on a multi-turn coding task,
  Opus 4.8). Cache-safe (sits after Claude Code's last breakpoint, so it never
  enters the cached prefix and never accumulates) and gated on tool-bearing
  turns. The no-shell directive stays in the cached block — unlike narration it
  is not recency-sensitive.
- **Client disconnect now aborts the upstream stream.** Draining to
  completion kept the model generating — and billing — a reply nobody would
  see; aborting matches what Claude Code gets without a proxy. The partial
  body is still logged, marked with the disconnect.
- **`Accept-Encoding` is no longer forwarded.** The proxy transparently
  decompresses upstream responses, so the negotiated codec must match
  aiohttp's capability, not the client's advertisement (a client advertising
  `br` on a machine without the codec would have broken every response).
- **Synthetic SSE no longer emits `data: [DONE]`.** Real Anthropic streams
  terminate on `message_stop`; the OpenAI-style sentinel was the one
  non-mimicking byte in the short-circuit synthesis.
- **Data paths respect `CLAWBACK_HOME`.** New env var; running from a
  checkout keeps the historical layout (tools.json / logs next to
  pyproject.toml), while a pip-installed `clawback` now uses `~/.clawback`
  instead of writing into `site-packages`.

### Added
- **The proxy measures its own savings.** Every mutated request records
  `bytes_removed` (and short-circuits record `bytes_unsent`) in the JSON
  record, the index, and the markdown header — the README's receipt, now
  reproducible from your own traffic.
- **`python -m clawback.stats [run-dir]`** aggregates a run's `index.jsonl`:
  per-transform and short-circuit hit counts (a count falling to zero is the
  drift signal that detection needs updating), bytes/tokens removed and
  unsent, and summed billed usage.
- **SSE parser hardening (log/repair path only).** CRLF record separators,
  `data:` without the optional space, multi-`data`-line events, and verbatim
  payload samples of unknown event types (capped) — the evidence trail for
  upstream format drift.
- **Form-3 test suite.** The selective trimming of inline `role:"system"`
  messages (nudges, MCP instructions, skills catalog) — previously the most
  intricate untested logic in the repo — plus tests for every fix above
  (83 tests total, up from 59).
- Friendly startup error for a non-integer `PROXY_PORT`; markdown log views
  now label which side of the proxy each header section shows.

## [0.5.0] — 2026-06-09

Compatibility pass for the Fable model family (`claude-fable-5`) and
Claude Code 2.1.x, verified by dogfooding the proxy against itself.

### Added
- **`strip-1m-model-suffix` transform.** Claude Code 2.1.x sends its `[1m]`
  (1M-context) model alias verbatim in some auxiliary calls — observed live:
  the `max_tokens: 1` "quota" probe with `claude-fable-5[1m]` — and the API
  rejects the literal name with a 404. The 1M window is actually selected by
  the `context-1m-2025-08-07` beta header the CLI already sends, so the
  suffix is stripped. A plain model name is never touched.
- **Renderer: new usage detail.** Fable-era responses carry
  `output_tokens_details` (e.g. `thinking_tokens`); the markdown usage
  section now renders the breakdown generically.

### Fixed
- **Inline reminder stripping no longer corrupts tool_results that quote the
  tags.** Found live while reviewing this very project through the proxy: a
  `Read` of `clawback/transforms.py` (which contains literal
  `<system-reminder>` text in its own regexes and docs) let the unanchored
  DOTALL matcher span from a tag quoted in a docstring to one quoted in a
  regex literal, silently excising ~460 lines of source from the forwarded
  tool_result. The matcher is now end-anchored (Claude Code *appends*
  reminders after real tool output) and tempered (a match can never cross
  another tag boundary). Reminders anywhere but the tail now fail open and
  surface in the log.

### Verified (no change needed)
- `reduce-main-system`, `filter-tools`, `strip-system-reminders`, and the
  `title-gen` short-circuit all fire correctly on Claude Code 2.1.170 /
  `claude-fable-5` traffic, including the new request fields
  (`thinking: {"type": "adaptive"}`, `output_config: {"effort": ...}`,
  `context_management`) and the `x-anthropic-billing-header` system block,
  which pass through untouched.

## [0.4.0] — 2026-06-05

### Changed
- **Renamed the project to `clawback`** (was `claude_proxy`). The import path,
  distribution name, and console command are all `clawback` now
  (`python -m clawback` / `clawback`). No behavior changed.
- Rewrote the README as a landing page, including a quantified
  savings/focus section with transparent methodology and cited research.

## [0.3.0] — 2026-06-05

### Added
- **Out-of-band SSE capture.** The assembler now records `error` events and any
  unrecognized event shape on the assembled message, so an error-only stream
  (HTTP 200 then a mid-stream failure) is no longer logged as an empty success.
  These are surfaced in the markdown view.
- **Log-only redaction** of `metadata.user_id` and the `X-Claude-Code-Session-Id`
  header. The real values are still forwarded upstream (billing / rate-limit
  reconciliation depend on them); only the on-disk copy is scrubbed.
- **Test suite** (`tests/`, pytest) covering tool filtering, SSE assembly,
  strip-system-reminders, the thinking-order repair, and log redaction.
- **Packaging metadata** in `pyproject.toml` and a `clawback` console entry
  point; `tools.json.example` template; `requirements-dev.txt`.
- **License:** released into the public domain under The Unlicense.
- **Project infrastructure for public release:** GitHub Actions CI (ruff lint +
  format check, pytest on Linux/macOS/Windows × Python 3.11–3.13), ruff config,
  `CONTRIBUTING.md`, `SECURITY.md`, issue/PR templates, and `.gitattributes`
  line-ending normalization.
- **Expanded test suite** (now covering reduce-main-system, the title-gen/recap
  short-circuits, the renderer, and an end-to-end async handler test against a
  fake upstream).

### Fixed
- **`tool_choice` reconciliation.** `filter-tools` no longer drops a tool pinned
  by `tool_choice`, and fails open when filtering would leave an `any`/`tool`
  choice unsatisfiable — preventing a proxy-caused upstream 400.
- **UTF-8 chunk boundaries.** `SSEAssembler` uses an incremental decoder, so a
  multi-byte character split across stream chunks no longer corrupts the logged
  copy or the thinking-order repair's identity hashes.
- **Reminder false positives.** `strip-system-reminders` no longer rewrites a
  `<system-reminder>` quoted inside a user's own message; only `tool_result`
  content is excised in place.

## [0.2.0] and earlier

Prototypes distributed as zip snapshots, predating version control. Tracked
history begins at 0.3.0.
