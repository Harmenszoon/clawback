"""Runtime configuration: env-loaded settings, log paths, header filters."""

from __future__ import annotations

import os
from pathlib import Path


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

def _load_env_file() -> None:
    """Populate os.environ from a project-root .env file, if present.

    Lines are KEY=VALUE; `export ` prefix and `# comment` lines are tolerated.
    Quoted values may contain trailing inline comments; unquoted values are
    truncated at the first ` #`. Existing env vars are not overwritten.
    """
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and value[0] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            if end != -1:
                value = value[1:end]
        elif " #" in value:
            value = value[: value.index(" #")].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

HOST: str = os.environ.get("PROXY_HOST", "localhost")
PORT: int = int(os.environ.get("PROXY_PORT", "3456"))
TARGET_BASE: str = os.environ.get("PROXY_TARGET_URL", "https://api.anthropic.com").rstrip("/")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).parent.parent
LOGS_ROOT: Path = PROJECT_ROOT / "logs"


# ---------------------------------------------------------------------------
# Header filters
# ---------------------------------------------------------------------------
# Headers we drop when forwarding because the proxy or aiohttp must set them
# itself, or because they describe a hop-by-hop concern that does not survive
# proxying.

REQUEST_STRIP_HEADERS: frozenset[str] = frozenset({
    "host",
    "content-length",      # aiohttp recomputes
    "transfer-encoding",   # aiohttp manages framing
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "upgrade",
})

RESPONSE_STRIP_HEADERS: frozenset[str] = frozenset({
    "content-length",      # chunked framing in use
    "content-encoding",    # aiohttp auto-decompresses upstream
    "transfer-encoding",
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "upgrade",
})

# Header values redacted before logging. The proxy still forwards the real
# value upstream; only the log entry is sanitized.
SENSITIVE_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-claude-code-session-id",
})
