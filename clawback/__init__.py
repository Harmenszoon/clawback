"""HTTP proxy between Claude Code and the Anthropic API.

Forwards every request upstream after applying a small, fail-open transform
pipeline that trims known sources of token waste, and writes a complete
per-request log for inspection. See README.md for the architecture and
the rationale behind each transform.
"""

__version__ = "0.5.0"
