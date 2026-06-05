# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[semantic versioning](https://semver.org/).

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
- **Packaging metadata** in `pyproject.toml` and a `claude-proxy` console entry
  point; `tools.json.example` template; `requirements-dev.txt`.
- **License:** released into the public domain under The Unlicense.

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
