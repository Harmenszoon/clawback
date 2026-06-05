---
name: Bug report
about: Something the proxy did wrong (a leak through, a corrupted request, a crash)
title: ""
labels: bug
---

**What happened**
A clear description of the bug.

**Expected**
What you expected instead.

**Repro**
Steps, and the relevant request shape if you can share it.

> ⚠️ Logs in `logs/` contain your prompts, file contents, and tool output.
> Sanitize before pasting — redaction only covers credentials and the
> `metadata.user_id` blob, not conversation content.

**Environment**
- OS:
- Python version:
- `clawback` version (`python -c "import clawback; print(clawback.__version__)"`):
- Claude Code CLI version:

**Relevant log / console output**
```
(paste here, sanitized)
```
