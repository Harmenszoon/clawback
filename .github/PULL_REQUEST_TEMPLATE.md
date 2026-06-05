## What & why

Briefly: what does this change, and what traffic/behavior motivated it?

## Checklist

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] `python -m pytest` passes; new behavior has tests
- [ ] Transforms stay **fail-open** and **copy-on-write** (see CONTRIBUTING.md)
- [ ] Detection is **structural**, not prose-based
- [ ] No `logs/`, `tools.json`, `.env`, or credentials committed
- [ ] `CHANGELOG.md` updated if user-facing
