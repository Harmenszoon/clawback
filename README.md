<div align="center">

# Clawback

### Less haystack. More needle.

**A tiny local proxy that sits between Claude Code and Anthropic — clawing back the tokens the CLI wastes, and sharpening the model by keeping its context window on _your_ task instead of the harness's boilerplate.**

[![CI](https://github.com/Harmenszoon/clawback/actions/workflows/ci.yml/badge.svg)](https://github.com/Harmenszoon/clawback/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](LICENSE)

</div>

---

## The problem

Every time you press Enter in Claude Code, the CLI quietly ships a **phone book** of boilerplate to the API — on *every single turn*:

- a **~27 KB** behavioral system prompt (rules the model already follows by default),
- **~70 KB** of tool manuals — for **43** tools, most of which you'll never let it touch,
- a fresh batch of `<system-reminder>` nudges re-stapled to your messages,
- and separate **Opus** side-calls just to invent a chat title and to "recap" when you step away.

You pay for all of it. Worse: the model has to read *past* all of it to find your actual question. **The bigger the haystack, the harder the needle is to find.**

## What Clawback does about it

It intercepts that traffic and strips it down to what matters — without ever touching what the model actually needs:

| On every turn, Claude Code sends… | Clawback forwards… |
| --- | --- |
| 🧾 A **~27 KB** behavioral system prompt | The 3 lines that actually matter (working dir, platform, OS) + 1 directive → **~280 chars** |
| 🧰 **43** tool definitions (**~70 KB**) | Only the tools *you've* allowed |
| 🔔 `<system-reminder>` blocks, re-injected every turn | Gone — and kept out of history too, so the prompt cache stays warm |
| 💸 A separate **Opus** call to title the chat + another to "recap" | Answered locally, instantly, for **0 tokens** |

Everything else flows through **untouched**.

## Two things happen when you stop shipping junk to the model

### 💰 You stop paying for boilerplate
Title generation and recap are full **Opus** inference calls — Clawback answers them locally for nothing. Reminders that change every turn stop busting your prompt cache. Tool manuals you'll never use stop riding along.

> **Straight talk:** the giant system prompt is mostly *cached*, and cache reads are cheap — so the headline savings come from the killed Opus side-calls, the cache-busting churn we eliminate, and the un-cached deltas. Real money, no hand-waving.

### 🧠 The model gets sharper
This is the part caching **can't** fix. A cached token is still a token sitting in the context window, still competing for the model's attention. Caching makes the clutter *cheap*; it doesn't make it *invisible*. Clawback is the only lever that shrinks the haystack — so the needle (your task) is easier to find. *Less haystack, more needle.*

## 📊 How much does this actually save?

Rough but honest estimates, derived from real captured traffic with the math shown. Mileage varies with conversation length and which tools you keep.

### Tokens

Clawback removes a fixed slab of overhead from **every** turn:

| Trimmed each turn | tokens: before → after |
| --- | --- |
| Behavioral system prompt | ~6,700 → ~70 |
| Tool definitions (43 tools → your lean set) | ~17,000 → ~4,000 |
| Re-injected `<system-reminder>` blocks | ~300 → 0 |
| **Fixed overhead removed** | **≈ 20,000 tokens / turn** |

That's **~50–75% of a short request**, easing to **~10–15% deep into a long session** (where your own history is most of the payload). On top of that, the title and recap **Opus** side-calls are eliminated outright.

> 💵 **On cost, honestly:** most of that overhead is *cached* (cache reads bill at ~10%), so the dollar savings are smaller than the raw token count suggests — the real money is the killed Opus calls and the cache-busting we prevent. On a Pro/Max plan it mostly buys you **more headroom before you hit your rate limit.**

### Focus — the haystack, quantified

Caching makes the clutter cheap, but the model still has to read past it. Clawback removes **~20K distractor tokens per turn** — exactly the kind of reduction the research says matters:

- 📉 **Lost in the Middle** (Liu et al., TACL 2024): accuracy follows a U-shape — a fact buried mid-context is recalled far less reliably than one near the edges, swinging results by **double-digit points**.
- 📏 **RULER** (NVIDIA, 2024): a model's *effective* context is often a fraction of its advertised window — **only about half** of tested models held up at 32K tokens. Extra tokens aren't free.
- 🧪 **Context Rot** (Chroma, 2025 — tested on Claude 4 among others): accuracy degrades as input grows **even on simple tasks**, and distractors make it measurably worse.

We deliberately **don't** slap a "+X% smarter" sticker on it — anyone who does is guessing. The precise, defensible version: the needle didn't change; we just shrank the haystack around it.

<details>
<summary><b>How we got these numbers</b></summary>

- ~4 characters per token; block sizes taken from real captured traffic in `logs/`.
- System-prompt and tool figures are Clawback's own observed before/after (27 KB → ~280 chars; 43 tools → a lean allowlist).
- "% of request" assumes a short turn ≈ 25–35K tokens and a long turn ≈ 150K+ (dominated by conversation history) — consistent with the cache-read sizes measured in live sessions.
- The focus/accuracy figures are **reported by the cited papers**, describing the failure mode Clawback targets — they are *not* a measured Clawback result.

</details>

**References:** [Lost in the Middle](https://arxiv.org/abs/2307.03172) · [RULER](https://arxiv.org/abs/2404.06654) · [Context Rot](https://research.trychroma.com/context-rot)

## 🛟 It can't break your session

A proxy that mangles your traffic is worse than no proxy. So every transform is **fail-open**:

- If it doesn't recognize something with **certainty**, it forwards your request **untouched**. The worst case is paying full price for one request — never a corrupted one.
- Detection keys off **structure** (JSON schema shape, tool names, Markdown landmarks) — not wording — so an Anthropic copy-edit can't trip it.
- When detection *does* drift, the un-stripped content simply shows up in the log. That's your signal, not a silent failure.

Backed by **53 tests** running on Linux, macOS, and Windows across Python 3.11–3.13.

## ⚡ Quickstart

```bash
pip install -r requirements.txt
python -m clawback
```

Then point Claude Code at it and go:

```powershell
# PowerShell
$env:ANTHROPIC_BASE_URL = "http://localhost:3456"
claude
```
```bash
# bash / zsh
export ANTHROPIC_BASE_URL=http://localhost:3456
claude
```

That's it. `Ctrl+C` to stop. Requires Python 3.11+; the only runtime deps are `aiohttp` and `certifi`.

---

## 🎁 Bonus: it fixes a bug that wedges sessions

With extended thinking + tool use, the API returns `thinking` blocks interleaved among `tool_use` blocks and requires them back **in the exact same order** (it cryptographically signs each one). Claude Code sometimes regroups them on resend — and the API rejects the whole turn with a 400, **permanently wedging the session**.

Clawback remembers the original order and quietly restores it before forwarding. The repair is reorder-only, exact-match-or-nothing, and fails open on any doubt — so it can never make a request worse than the client already made it. You just stop hitting the wall.

## 🔍 Everything is logged, beautifully

Clawback writes a complete, human-readable record of every request and response — so you can finally answer *"what is Claude Code actually sending?"* by opening one file.

- `NNN_*.json` — the full structured record (the source of truth).
- `NNN_*.md` — a paired, readable rendering: system blocks sized and cache-flagged, a tool index, every message block annotated, usage breakdown.
- `index.jsonl` — one line per request to scan or grep.

Each record is flagged with whether Clawback intervened and how. Credentials and the `metadata.user_id` telemetry blob are redacted in the logs (but still forwarded upstream).

## 🎛️ You're the policy — `tools.json`

Which tools reach the model is **entirely yours to decide**, in a plain JSON file you edit at your leisure:

```json
{ "Read": true, "Edit": true, "WebSearch": true, "Bash": false, "TaskCreate": false }
```

- Edits take effect on the **next request** — no restart.
- A tool Clawback has never seen defaults to **allowed** and is added to the file, so nothing ever breaks silently — it just shows up for you to decide.
- Writes are atomic; a broken file fails *safe* (allow-all + a warning, never a silent wipe of your config).

Start from the checked-in [`tools.json.example`](tools.json.example).

---

## How it works

```
                       ┌──────────────┐
   claude  ──HTTP──▶   │   Clawback   │   ──HTTP──▶  api.anthropic.com
                       │  (localhost) │
                       └──────┬───────┘
                              │
   ┌──────────────┬───────────┴──────────┬──────────────────┐
   ▼              ▼                       ▼                  ▼
 short-circuit  mutate request        repair               log
 (title, recap) (shrink prompt,    (restore thinking     (JSON + Markdown
  → answered    filter tools,        block order)         + index)
   locally)     strip reminders)
```

For each request Clawback either (1) **answers it locally** if it's a known auxiliary call (title, recap), or (2) **slims it** (reduce the system prompt, filter tools, strip reminders), repairs any reordered thinking blocks, forwards it, and streams the reply back **byte-for-byte** while assembling a copy for the log.

### The transforms

| Transform | What it does |
| --- | --- |
| `title-gen` *(short-circuit)* | Detects the title-request schema and returns a synthetic `CONVERSATION_<hex>` — no upstream call. |
| `recap` *(short-circuit)* | Detects the "user stepped away" prompt and returns `"Continuing."` instead of paying Opus to summarize. |
| `reduce-main-system` | Replaces the ~27 KB behavioral prompt with the operational env lines + one tool-selection directive. |
| `filter-tools` | Drops tools disabled in `tools.json`; auto-discovers new ones; never drops a tool the request *forces* via `tool_choice`. |
| `strip-system-reminders` | Removes injected reminders in all three forms (standalone blocks, inline in tool results, and `role:"system"` messages), consistently across history so the cache prefix holds. |
| `restore-thinking-order` *(repair)* | Undoes Claude Code's interleaved-thinking reordering using the original order Clawback recorded. |

## Configuration

All settings are environment variables (optionally from a `.env` file):

| Variable | Default | Description |
| --- | --- | --- |
| `PROXY_HOST` | `localhost` | Bind address |
| `PROXY_PORT` | `3456` | Listen port |
| `PROXY_TARGET_URL` | `https://api.anthropic.com` | Upstream API endpoint |

## FAQ

**Will it break Claude Code?**
No transform mutates a request it isn't certain about — it fails open and forwards the original. The worst case is a missed optimization, never a broken request.

**Does it phone home or read my data?**
It runs entirely on your machine. Nothing leaves except the (slimmer) request to Anthropic you were already making. Your credentials pass straight through; logs stay local and are gitignored.

**Will it slow me down?**
It's an async passthrough. Transforms are microseconds; the upstream call is seconds. Streaming responses are forwarded chunk-by-chunk, unchanged.

**Does it work with my MCP servers / OS / tool setup?**
Yes. `tools.json` is yours; unknown tools default to allowed and appear in the file for you to decide. Swap `PowerShell` for `Bash` off Windows.

**Is it safe to run?**
It's a localhost, single-user tool with no auth, and its logs contain your prompts and tool output. Keep it bound to `localhost` and don't publish logs raw. See [SECURITY.md](SECURITY.md).

---

## Design principles

- **Detect structurally, never on prose.** Schema shapes and tool names are contracts; the sentences around them are not.
- **Fail open, never silently break.** A missed optimization is cheap; a corrupted request is not.
- **Copy-on-write.** Transforms never mutate the object they receive.
- **The user is the policy.** `tools.json` decides; Clawback never pre-judges.
- **Exhaustive logs by default.** If you can't see it, you can't trim it.

## Project layout

```
clawback/
  __main__.py        entry point (python -m clawback); tees output to console.log
  server.py          aiohttp app + the request handler that drives the pipeline
  transforms.py      short-circuits + mutations (title, recap, reduce, strip, filter)
  thinking_order.py  stateful repair for the interleaved-thinking reorder bug
  tool_filter.py     dynamic tools.json allowlist with atomic writes
  sse.py             SSE stream assembler (observation-only, for the log)
  log.py / render.py per-run logging + JSON→Markdown rendering
  config.py          env loading, paths, header filters
tests/               pytest suite (transforms, SSE, repair, redaction, async handler)
.github/             CI + issue/PR templates
```

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest          # 53 tests, no network needed
ruff check . && ruff format --check .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the design rules and how to add a transform. The same gates run in CI across three OSes and Python 3.11–3.13.

## License

[The Unlicense](LICENSE) — public domain. Do whatever you want with it; no attribution required.

---

<div align="center">
<sub>Clawback is an independent project and is not affiliated with or endorsed by Anthropic.</sub>
</div>
