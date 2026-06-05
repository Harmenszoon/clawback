# Contributing

Thanks for your interest. This is a small, public-domain utility; contributions
are welcome but the bar is "does it keep the proxy boring and safe."

## Development setup

```bash
pip install -r requirements-dev.txt
```

## Before you push

The CI runs exactly these three gates — run them locally first:

```bash
ruff check .          # lint
ruff format --check . # formatting (run `ruff format .` to fix)
python -m pytest      # tests
```

## Design principles (please preserve them)

These are the rules the codebase lives by. A change that violates one is
unlikely to be merged:

- **Detect structurally, never on prose.** Key off schema shape, tool names,
  Markdown landmarks, and tag boundaries — never the exact wording, which
  Anthropic can change at any time.
- **Fail open, never silently corrupt.** If a transform can't find what it
  expects, forward the request unchanged. A missed optimization is cheap; a
  corrupted request is not. The un-stripped content showing up in the log is
  the intended signal that detection needs updating.
- **Copy-on-write.** Mutating transforms must not modify the dict they
  receive; build new containers for what changes and alias the rest.
- **The user is the policy.** `tools.json` decides what's allowed; the proxy
  never pre-judges and defaults unknown tools to allowed.

See `README.md` ("Design principles" and "Extending it") for the full
rationale and how to add a transform or short-circuit.

## Tests

Add tests for new behavior. The pure transform/parse logic is
straightforward to unit-test; the async handler has integration tests in
`tests/test_server.py` that drive the real pipeline against a fake upstream —
follow that pattern.

## Please don't commit

`logs/`, `tools.json`, `.env`, or anything containing real credentials or
conversation content. They are gitignored; keep them that way.
