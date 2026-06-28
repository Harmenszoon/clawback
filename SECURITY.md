# Security & Privacy

`clawback` sits in the path of your Claude Code traffic and writes an
exhaustive log of every request and response. Please read this before running
it anywhere other than your own machine.

## Threat model / intended deployment

- **Single-user, localhost only.** The proxy binds to `localhost:3456` by
  default and has **no authentication**. The proxy itself holds and adds no
  credentials — it forwards whatever `Authorization` / `x-api-key` headers
  each client sends — so an attacker reaching the socket cannot spend *your*
  quota. What they can do is relay their own traffic through your machine
  (hiding their origin) and grow your logs without bound. Do not bind it to
  a public interface (`PROXY_HOST`) or expose the port.
- **It forwards your real API credentials upstream.** The `Authorization` /
  `x-api-key` headers your client sends are passed through to
  `api.anthropic.com` unchanged on every request (and redacted in the logs).
- **Request bodies are unbounded** (`client_max_size=0`) by design, which is
  fine for a local single client but unsafe on an exposed interface.

## The logs contain sensitive data

`logs/` holds your **prompts, file contents, command output, and tool
results** in full. Treat the directory as sensitive:

- It is gitignored and must never be committed.
- Secrets-bearing headers (`Authorization`, `x-api-key`, `Cookie`,
  `Set-Cookie`, `Proxy-Authorization`, `X-Claude-Code-Session-Id`) and the
  request-body `metadata.user_id` telemetry blob are redacted in the log
  copy — but the conversation content is **not**. Redaction makes logs
  *tidier*, not *safe to publish*.
- Review and sanitize before sharing a log for a bug report.

## Reporting a vulnerability

This is a small, public-domain, single-author project. If you find a security
issue, please open a [GitHub Security Advisory][advisory] (preferred) or a
regular issue if it is not sensitive. There is no formal SLA, but reports are
welcome and will be addressed on a best-effort basis.

[advisory]: https://github.com/Harmenszoon/clawback/security/advisories/new
