"""Async HTTP proxy with a fail-open transform pipeline.

For each incoming request the handler:

  1. Reads the body and tries to parse it as JSON.
  2. Asks `transforms.maybe_shortcut` whether this request can be answered
     locally without an upstream call (currently: title-gen, recap). If so,
     emits a synthetic response and records it.
  3. Otherwise asks `transforms.apply_request_transforms` to mutate the
     body in place (reduce the main system prompt, strip system-reminders,
     filter the tool list). If anything was changed, the body is
     re-serialized before being forwarded.
  4. Forwards the request upstream and streams the response back. SSE
     responses pass through byte-for-byte while a parallel
     `SSEAssembler` builds a structured copy for the log.
  5. Records the exchange via `RunLogger`.

Every transform is structurally gated and fails open: if anything it looks
for is missing or has changed shape, the request flows through unchanged
and the un-stripped content surfaces in the per-request log so drift is
visible.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import sys
import traceback
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import aiohttp
import certifi
from aiohttp import web

from .config import (
    HOST,
    PORT,
    REQUEST_STRIP_HEADERS,
    RESPONSE_STRIP_HEADERS,
    TARGET_BASE,
)
from .log import RunLogger
from .sse import SSEAssembler
from .thinking_order import ThinkingOrderCache
from .transforms import apply_request_transforms, maybe_shortcut, to_sse_bytes

# ---------------------------------------------------------------------------
# SSL
# ---------------------------------------------------------------------------


def _make_ssl_context() -> ssl.SSLContext:
    """Return an SSL context backed by certifi's CA bundle."""
    return ssl.create_default_context(cafile=certifi.where())


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class ProxyHandler:
    """aiohttp request handler that mirrors traffic to the upstream API."""

    def __init__(self, logger: RunLogger) -> None:
        self.logger = logger
        self.session: aiohttp.ClientSession | None = None
        self.ssl_context = _make_ssl_context()
        # Canonical assistant-turn block orders, used to undo Claude Code's
        # interleaved-thinking reordering on resend. Stateful, per-process.
        self.thinking_cache = ThinkingOrderCache()
        # Outstanding log-write tasks. Each request fires a background log
        # write so the response is not blocked on disk I/O. The set holds a
        # reference until completion (asyncio garbage-collects orphan tasks
        # otherwise) and is awaited during shutdown.
        self._pending_logs: set[asyncio.Task] = set()

    async def startup(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def cleanup(self) -> None:
        # Drain any pending log writes so nothing is lost on shutdown.
        if self._pending_logs:
            await asyncio.gather(*self._pending_logs, return_exceptions=True)
        if self.session:
            await self.session.close()

    def _record(self, **kwargs: Any) -> None:
        """Spawn a background log write. Never raises, never blocks the request."""
        task = asyncio.create_task(_safe_log(self.logger, kwargs))
        self._pending_logs.add(task)
        task.add_done_callback(self._pending_logs.discard)

    async def _serve_shortcut(
        self,
        request: web.Request,
        started_at: datetime,
        path_with_query: str,
        req_body_parsed: Any,
        request_size: int,
        shortcut: tuple[dict, str],
    ) -> web.StreamResponse:
        """Emit a synthetic response — no upstream call — and log the suppression."""
        synth_body, reason = shortcut
        is_stream = bool(isinstance(req_body_parsed, dict) and req_body_parsed.get("stream"))

        if is_stream:
            sent_headers = {
                "X-Proxy-Synthesized": reason,
                "Content-Type": "text/event-stream; charset=utf-8",
            }
            response = web.StreamResponse(status=200, headers=sent_headers)
            response.enable_chunked_encoding()
            await response.prepare(request)
            with contextlib.suppress(ConnectionError, aiohttp.ClientConnectionError):
                await response.write(to_sse_bytes(synth_body))
        else:
            # web.json_response sets Content-Type itself. Read it back from the
            # constructed response so the log reflects exactly what was sent.
            response = web.json_response(
                synth_body,
                headers={"X-Proxy-Synthesized": reason},
            )
            sent_headers = dict(response.headers)

        elapsed = (datetime.now(UTC) - started_at).total_seconds()
        print(
            f"[{started_at.strftime('%H:%M:%S')}] {request.method} {path_with_query} "
            f"-> 200 (short-circuited: {reason}, {elapsed:.3f}s)",
            flush=True,
        )

        self._record(
            started_at=started_at,
            elapsed_s=elapsed,
            method=request.method,
            path=path_with_query,
            request_headers=dict(request.headers),
            request_body=req_body_parsed,
            response_status=200,
            response_headers=sent_headers,
            response_body=synth_body,
            short_circuited=reason,
            bytes_unsent=request_size,
        )
        return response

    async def forward(self, request: web.Request) -> web.StreamResponse:
        """Forward one client request upstream and stream the reply back."""
        started_at = datetime.now(UTC)
        assert self.session is not None

        req_body_bytes = await request.read()
        original_size = len(req_body_bytes)
        req_body_parsed = _try_parse_json(req_body_bytes)
        target_url = f"{TARGET_BASE}{request.rel_url}"
        forward_headers = _build_forward_headers(request)
        session_id = request.headers.get("X-Claude-Code-Session-Id", "")

        path_with_query = request.path + (f"?{request.query_string}" if request.query_string else "")

        # Transforms must never break the request path. A bug in detection or
        # mutation logic falls back to forwarding the original bytes unchanged.
        try:
            shortcut = maybe_shortcut(req_body_parsed)
        except Exception as exc:
            print(
                f"  WARNING: maybe_shortcut failed ({type(exc).__name__}: {exc}); forwarding upstream",
                flush=True,
            )
            shortcut = None

        if shortcut is not None:
            return await self._serve_shortcut(
                request,
                started_at,
                path_with_query,
                req_body_parsed,
                original_size,
                shortcut,
            )

        # Apply request mutations. If anything ran, we re-serialize so the
        # upstream call sees the transformed body. The original parsed body
        # is kept aside for logging — the log reflects what we sent.
        try:
            forwarded_body, transforms_applied = apply_request_transforms(req_body_parsed)
        except Exception as exc:
            print(
                f"  WARNING: apply_request_transforms failed ({type(exc).__name__}: {exc}); forwarding original body",
                flush=True,
            )
            forwarded_body, transforms_applied = req_body_parsed, []

        # Undo Claude Code's interleaved-thinking reordering using the canonical
        # block order remembered from the original response. Stateful and
        # fail-open: any error or cache miss leaves the body as-is.
        try:
            forwarded_body, repaired = self.thinking_cache.repair_request(
                session_id,
                forwarded_body,
            )
        except Exception as exc:
            print(
                f"  WARNING: thinking-order repair failed ({type(exc).__name__}: {exc}); forwarding as-is",
                flush=True,
            )
            repaired = 0
        if repaired:
            transforms_applied = [*transforms_applied, "restore-thinking-order"]

        # Approximate per-request savings: Claude Code sends compact JSON, so
        # the size delta of the re-serialized body is the content removed by
        # the transforms (a reorder-only repair nets out near zero).
        bytes_removed: int | None = None
        if transforms_applied:
            req_body_parsed = forwarded_body
            req_body_bytes = json.dumps(
                forwarded_body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            bytes_removed = original_size - len(req_body_bytes)

        try:
            async with self.session.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                data=req_body_bytes if req_body_bytes else None,
                ssl=self.ssl_context if TARGET_BASE.startswith("https://") else False,
            ) as upstream:
                response_headers = _filter_headers(upstream.headers, RESPONSE_STRIP_HEADERS)
                is_sse = (upstream.content_type or "").startswith("text/event-stream")

                stream_error: str | None = None
                if is_sse:
                    response, resp_body, total_bytes, stream_error = await _stream_sse(
                        request, upstream, response_headers
                    )
                else:
                    response, resp_body, total_bytes = await _buffer_response(request, upstream, response_headers)

                # Remember this turn's canonical block order so we can undo a
                # reordered resend on a later request. Never fatal to the proxy.
                try:
                    self.thinking_cache.record_response(session_id, resp_body, upstream.status)
                except Exception as exc:
                    print(
                        f"  WARNING: thinking-order record failed ({type(exc).__name__}: {exc})",
                        flush=True,
                    )

                elapsed = (datetime.now(UTC) - started_at).total_seconds()

                _log_console(started_at, request.method, path_with_query, upstream.status, total_bytes, elapsed)
                if stream_error:
                    print(f"  Note: {stream_error}", flush=True)

                self._record(
                    started_at=started_at,
                    elapsed_s=elapsed,
                    method=request.method,
                    path=path_with_query,
                    request_headers=dict(request.headers),
                    request_body=req_body_parsed if req_body_parsed is not None else _decode_or_repr(req_body_bytes),
                    response_status=upstream.status,
                    response_headers=dict(response_headers),
                    response_body=resp_body,
                    error=stream_error,
                    transforms_applied=transforms_applied,
                    bytes_removed=bytes_removed,
                )

                return response

        except Exception as exc:
            elapsed = (datetime.now(UTC) - started_at).total_seconds()
            err = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
            self._record(
                started_at=started_at,
                elapsed_s=elapsed,
                method=request.method,
                path=path_with_query,
                request_headers=dict(request.headers),
                request_body=req_body_parsed if req_body_parsed is not None else _decode_or_repr(req_body_bytes),
                response_status=502,
                response_headers={},
                response_body=None,
                error=err,
                transforms_applied=transforms_applied,
            )
            print(
                f"[{started_at.strftime('%H:%M:%S')}] ERROR {request.method} "
                f"{path_with_query} -> {err} ({elapsed:.1f}s)"
            )
            # Mirror Anthropic's error envelope so callers can parse the failure
            # without a special case for proxy-shaped responses.
            return web.json_response(
                {"type": "error", "error": {"type": "proxy_error", "message": err}},
                status=502,
            )


# ---------------------------------------------------------------------------
# Streaming / buffering helpers
# ---------------------------------------------------------------------------


async def _stream_sse(
    request: web.Request,
    upstream: aiohttp.ClientResponse,
    response_headers: dict[str, str],
) -> tuple[web.StreamResponse, Any, int, str | None]:
    """Forward an SSE response chunk-by-chunk; assemble a log copy in parallel.

    Returns (response, assembled_body, bytes_streamed, stream_error).

    `stream_error` is set when the stream ended abnormally, in either
    direction. Once the 200 status line has gone out, a failure can no longer
    be reported as an HTTP error — the only honest options are to finalize the
    truncated stream and record what actually happened. Two cases:

      * Upstream died mid-stream: the partial body is logged with the error
        attached (not mislabeled as a proxy 502 the client never saw).
      * The client went away (e.g. the user hit Esc): the upstream read stops
        too. Draining to completion would keep the model generating — and
        billing — a reply nobody will ever see; aborting matches what Claude
        Code gets without a proxy, where its own disconnect cancels generation.
    """
    response = web.StreamResponse(status=upstream.status, headers=response_headers)
    response.enable_chunked_encoding()
    await response.prepare(request)

    assembler = SSEAssembler()
    total_bytes = 0
    stream_error: str | None = None

    try:
        async for chunk in upstream.content.iter_any():
            if not chunk:
                continue
            total_bytes += len(chunk)
            assembler.feed(chunk)
            try:
                await response.write(chunk)
            except (ConnectionError, aiohttp.ClientConnectionError):
                stream_error = "client disconnected mid-stream; upstream aborted"
                break
    except (TimeoutError, OSError, aiohttp.ClientError) as exc:
        stream_error = f"upstream stream aborted mid-response ({type(exc).__name__}: {exc})"

    if stream_error:
        # Piggyback the assembler's error channel: the markdown render surfaces
        # it, and the thinking-order cache refuses to treat an errored body as
        # a canonical turn.
        assembler.errors.append({"type": "proxy_stream_interrupted", "message": stream_error})

    return response, assembler.assembled(), total_bytes, stream_error


async def _buffer_response(
    _request: web.Request,
    upstream: aiohttp.ClientResponse,
    response_headers: dict[str, str],
) -> tuple[web.Response, Any, int]:
    """Buffer a non-streaming response, forward it whole, return parsed body."""
    raw = await upstream.read()
    body_for_log: Any = _try_parse_json(raw)
    if body_for_log is None:
        body_for_log = _decode_or_repr(raw)

    response = web.Response(status=upstream.status, body=raw, headers=response_headers)
    return response, body_for_log, len(raw)


# ---------------------------------------------------------------------------
# Background logging
# ---------------------------------------------------------------------------


async def _safe_log(logger: RunLogger, fields: dict[str, Any]) -> None:
    """Write a log record and swallow any failure.

    Log writes happen on a worker thread but exceptions still propagate
    back into the coroutine; if disk is full or the path is unwritable we
    record the failure to stderr rather than letting it surface in the
    request handler — a request that has already succeeded must not be
    turned into a 502 by a log-write error.
    """
    try:
        await logger.record(**fields)
    except Exception as exc:
        print(
            f"  WARNING: log write failed ({type(exc).__name__}: {exc})",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Header / body utilities
# ---------------------------------------------------------------------------


def _build_forward_headers(request: web.Request) -> dict[str, str]:
    """Copy client headers, drop hop-by-hop entries, and set Host upstream."""
    headers = _filter_headers(request.headers, REQUEST_STRIP_HEADERS)
    headers["Host"] = urlparse(TARGET_BASE).netloc
    return headers


def _filter_headers(headers, strip: frozenset[str]) -> dict[str, str]:
    """Return a dict copy of `headers` with names in `strip` (case-insensitive) removed."""
    return {name: value for name, value in headers.items() if name.lower() not in strip}


def _try_parse_json(raw: bytes) -> Any:
    """Return the parsed JSON value if raw is valid JSON, else None."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _decode_or_repr(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace") if raw else ""


def _log_console(started_at: datetime, method: str, path: str, status: int, total_bytes: int, elapsed: float) -> None:
    print(
        f"[{started_at.strftime('%H:%M:%S')}] {method} {path} -> {status} ({total_bytes:,} bytes, {elapsed:.1f}s)",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def health_check(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def serve(logger: RunLogger) -> None:
    """Bind the proxy to HOST:PORT and run until cancelled."""
    handler = ProxyHandler(logger)
    await handler.startup()

    app = web.Application(client_max_size=0)
    app.router.add_get("/", health_check)
    app.router.add_route("*", "/{path:.*}", handler.forward)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()

    print(f"Proxy running on http://{HOST}:{PORT}")
    print(f"Forwarding to {TARGET_BASE}")
    print(f"Logs:        {logger.run_dir}")
    print()
    if sys.platform == "win32":
        print(f'  $env:ANTHROPIC_BASE_URL="http://{HOST}:{PORT}"')
    else:
        print(f"  export ANTHROPIC_BASE_URL=http://{HOST}:{PORT}")
    print("  claude")
    print("\nCtrl+C to stop.\n", flush=True)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await handler.cleanup()
