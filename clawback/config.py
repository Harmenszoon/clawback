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

_raw_port = os.environ.get("PROXY_PORT", "3456")
try:
    PORT: int = int(_raw_port)
except ValueError:
    raise SystemExit(f"PROXY_PORT must be an integer, got {_raw_port!r}") from None

TARGET_BASE: str = os.environ.get("PROXY_TARGET_URL", "https://api.anthropic.com").rstrip("/")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _resolve_data_home() -> Path:
    """Directory that holds the mutable per-user state: tools.json and logs/.

    Resolution order:
      1. `CLAWBACK_HOME` (env var or .env) — explicit wins, created if missing.
      2. The repo root, when running from a checkout (detected by the
         pyproject.toml sitting next to the package) — the historical layout.
      3. `~/.clawback` — the pip-installed case, where the package parent is
         `site-packages` and writing config/logs there would be unwritable at
         worst and invisible to the user at best.
    """
    env = os.environ.get("CLAWBACK_HOME")
    if env:
        home = Path(env).expanduser()
        home.mkdir(parents=True, exist_ok=True)
        return home
    pkg_parent = Path(__file__).parent.parent
    if (pkg_parent / "pyproject.toml").exists():
        return pkg_parent
    home = Path.home() / ".clawback"
    home.mkdir(parents=True, exist_ok=True)
    return home


DATA_HOME: Path = _resolve_data_home()
LOGS_ROOT: Path = DATA_HOME / "logs"


# ---------------------------------------------------------------------------
# Header filters
# ---------------------------------------------------------------------------
# Headers we drop when forwarding because the proxy or aiohttp must set them
# itself, or because they describe a hop-by-hop concern that does not survive
# proxying.

REQUEST_STRIP_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "content-length",  # aiohttp recomputes
        "transfer-encoding",  # aiohttp manages framing
        # The client's Accept-Encoding advertises what *it* can decode, but the
        # proxy transparently decompresses upstream responses (Content-Encoding
        # is stripped below), so the relevant capability is aiohttp's, not the
        # client's. Forwarding e.g. `br`/`zstd` from a client on a machine where
        # the codec isn't installed would make every compressed response fail.
        # Dropping the header lets aiohttp negotiate exactly what it can decode.
        "accept-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)

RESPONSE_STRIP_HEADERS: frozenset[str] = frozenset(
    {
        "content-length",  # chunked framing in use
        "content-encoding",  # aiohttp auto-decompresses upstream
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)

# Header values redacted before logging. The proxy still forwards the real
# value upstream; only the log entry is sanitized.
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-claude-code-session-id",
    }
)
