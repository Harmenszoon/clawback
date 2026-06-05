"""Integration tests for the async proxy handler.

These drive the real `ProxyHandler.forward` path through an in-process aiohttp
test server, with the upstream Anthropic call replaced by a fake session. They
cover: short-circuit (no upstream call), normal forward + hop-by-hop header
handling, and SSE pass-through with parallel log assembly.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_proxy.log import RunLogger
from claude_proxy.server import ProxyHandler

# --- fakes -------------------------------------------------------------------


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_any(self):
        for c in self._chunks:
            yield c


class _FakeUpstream:
    def __init__(self, *, status=200, headers=None, body=b"", content_type="application/json", sse_chunks=None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self._body = body
        self.content = _FakeContent(sse_chunks or [])

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession that records calls and returns canned responses."""

    def __init__(self, upstream: _FakeUpstream) -> None:
        self._upstream = upstream
        self.calls: list[dict] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self._upstream

    async def close(self):
        pass


async def _client(tmp_path, upstream: _FakeUpstream) -> tuple[TestClient, ProxyHandler]:
    logger = RunLogger(tmp_path)
    handler = ProxyHandler(logger)
    handler.session = _FakeSession(upstream)  # bypass the real upstream
    app = web.Application(client_max_size=0)
    app.router.add_route("*", "/{path:.*}", handler.forward)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, handler


async def _drain_logs(handler: ProxyHandler) -> None:
    if handler._pending_logs:
        await asyncio.gather(*handler._pending_logs, return_exceptions=True)


# --- tests -------------------------------------------------------------------


async def test_shortcut_does_not_call_upstream(tmp_path):
    upstream = _FakeUpstream()
    client, handler = await _client(tmp_path, upstream)
    try:
        body = {
            "model": "claude-opus-4",
            "messages": [{"role": "user", "content": "The user stepped away and is coming back. Recap now"}],
        }
        resp = await client.post("/v1/messages", json=body)
        assert resp.status == 200
        assert resp.headers.get("X-Proxy-Synthesized") == "recap"
        data = await resp.json()
        assert data["content"][0]["text"] == "Continuing."
        # The upstream session was never touched.
        assert handler.session.calls == []
        await _drain_logs(handler)
        assert list(tmp_path.glob("*.json"))  # a record was written
    finally:
        await client.close()


async def test_normal_forward_returns_upstream_body_and_strips_hop_headers(tmp_path):
    upstream = _FakeUpstream(
        status=200,
        headers={"Content-Type": "application/json", "anthropic-organization-id": "org-x"},
        body=json.dumps({"type": "message", "content": [{"type": "text", "text": "hi"}]}).encode(),
    )
    client, handler = await _client(tmp_path, upstream)
    try:
        resp = await client.post("/v1/messages", json={"model": "claude", "messages": []})
        assert resp.status == 200
        assert (await resp.json())["content"][0]["text"] == "hi"

        # Exactly one upstream call, with Host rewritten and hop-by-hop headers dropped.
        assert len(handler.session.calls) == 1
        fwd_headers = {k.lower(): v for k, v in handler.session.calls[0]["headers"].items()}
        assert fwd_headers["host"] == "api.anthropic.com"
        assert "content-length" not in fwd_headers  # aiohttp recomputes it
        await _drain_logs(handler)
    finally:
        await client.close()


async def test_sse_passthrough_streams_and_assembles_for_log(tmp_path):
    events = (
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"id":"msg_1","model":"claude","type":"message","role":"assistant","usage":{}}}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"hello"}}\n\n'
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        b"data: [DONE]\n\n"
    )
    # Split across two chunks to exercise the incremental parser.
    upstream = _FakeUpstream(
        status=200,
        headers={"Content-Type": "text/event-stream"},
        content_type="text/event-stream",
        sse_chunks=[events[:120], events[120:]],
    )
    client, handler = await _client(tmp_path, upstream)
    try:
        resp = await client.post("/v1/messages", json={"model": "claude", "messages": [], "stream": True})
        assert resp.status == 200
        raw = await resp.read()
        # Client received the bytes verbatim.
        assert raw == events
        await _drain_logs(handler)
        # The log captured the assembled message.
        record = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
        assert record["response"]["body"]["content"] == [{"type": "text", "text": "hello"}]
    finally:
        await client.close()


async def test_upstream_error_returns_502_envelope(tmp_path):
    class _BoomSession:
        calls: list = []

        def request(self, **kwargs):
            raise ConnectionError("upstream down")

        async def close(self):
            pass

    logger = RunLogger(tmp_path)
    handler = ProxyHandler(logger)
    handler.session = _BoomSession()
    app = web.Application(client_max_size=0)
    app.router.add_route("*", "/{path:.*}", handler.forward)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/v1/messages", json={"model": "claude", "messages": []})
        assert resp.status == 502
        data = await resp.json()
        assert data["type"] == "error"
        assert data["error"]["type"] == "proxy_error"
        await _drain_logs(handler)
    finally:
        await client.close()
