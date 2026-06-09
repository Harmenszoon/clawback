# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[semantic versioning](https://semver.org/).

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
