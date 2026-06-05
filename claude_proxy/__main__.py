"""Entry point: `python -m claude_proxy`.

Boots the server and tees stdout/stderr into the active run's `console.log`
so crashes and prints are captured alongside the per-request JSON files.
"""

from __future__ import annotations

import asyncio
import sys
import traceback

from .log import RunLogger
from .server import serve


class _Tee:
    """Duplicate writes between an original stream and a file."""

    def __init__(self, stream, file) -> None:
        self._stream = stream
        self._file = file

    def write(self, data):
        try:
            self._file.write(data)
            self._file.flush()
        except Exception:
            pass
        return self._stream.write(data)

    def flush(self) -> None:
        try:
            self._file.flush()
        except Exception:
            pass
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _run() -> None:
    logger = RunLogger.create()
    console_path = logger.run_dir / "console.log"
    log_file = open(console_path, "w", encoding="utf-8", buffering=1)

    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)

    exit_code = 0
    try:
        asyncio.run(serve(logger))
    except KeyboardInterrupt:
        print("\nShutting down.")
    except Exception as exc:
        # A startup or runtime failure must produce a non-zero exit code so
        # supervisors and CI can distinguish a clean shutdown from a crash.
        # `KeyboardInterrupt` is handled above as a clean shutdown (exit 0).
        # `SystemExit` is not caught here so explicit `sys.exit(N)` calls
        # retain their code.
        print(f"\nFatal: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        exit_code = 1
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            log_file.close()
        except Exception:
            pass

    sys.exit(exit_code)


if __name__ == "__main__":
    _run()
