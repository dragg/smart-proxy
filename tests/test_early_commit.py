# tests/test_early_commit.py
"""A slow upstream must not leave the client in silence long enough for a 524.

On 2026-10-19 Claude Code sent a ~509K-token Opus request after its prompt cache
had expired. Anthropic answers -- headers and ``message_start`` together -- only
once prefill is done, so the proxy sat in ``client.send()`` with nothing to
forward and Cloudflare cut the request at 120s with a 524.

Pinned here:

* a streamed ``/v1/messages`` whose upstream has not answered in time gets a 200
  SSE response with ``: keepalive`` comments, and the upstream stream follows;
* once that 200 is out, every way the request can still fail ends in one
  well-formed ``event: error`` -- never a second response, never raw JSON in the
  stream (Claude Code would re-send the request non-streaming);
* the error types are the ones Claude Code retries on (5xx -> overloaded_error);
* nothing changes for fast upstreams, non-streamed requests and the
  OpenAI-compat loopback, which needs real HTTP statuses.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from aiohttp.test_utils import TestServer

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy import anthropic_proxy
from smart_proxy.anthropic_proxy import _proxy_handler
from tests.test_anthropic_proxy_oauth_messages import (
    _FakeStreamResponse,
    _FakeStreamingUpstreamResponse,
)
from tests.test_proxy_upstream_overload import _MultiKeyPool, _SequencedClient, _key
from tests.test_upstream_connection_leak import _OK_SSE, _ProxyCase

_STREAM_BODY = b'{"model":"claude-opus-5","stream":true,"messages":[]}'
_KEEPALIVE = b": keepalive\n\n"


class _SlowClient(_SequencedClient):
    """Upstream that takes ``delay`` seconds to answer each send, like a long prefill."""

    def __init__(self, responses: list, delay: float) -> None:
        super().__init__(responses)
        self.delay = delay
        self.cancelled = False

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().send(req, stream=stream)


class _GoneAtPrepare(_FakeStreamResponse):
    async def prepare(self, request):  # noqa: ANN001
        raise ClientConnectionResetError("Cannot write to closing transport")


class _GoneAtFirstWrite(_FakeStreamResponse):
    """aiohttp 3.13 only buffers headers in prepare(); the first write finds the hang-up."""

    async def write(self, chunk: bytes) -> None:
        raise ClientConnectionResetError("Cannot write to closing transport")


def _sse_errors(chunks: list[bytes]) -> list[dict]:
    events = []
    for frame in b"".join(chunks).split(b"\n\n"):
        if frame.startswith(b"event: error\n"):
            events.append(json.loads(frame.split(b"data: ", 1)[1]))
    return events


class _EarlyCase(_ProxyCase):
    def _app(self, client, pool, *, early: float = 0.05) -> dict:  # noqa: ANN001
        app = super()._app(client, pool)
        app["early_commit_seconds"] = early
        return app

    async def _serve(self, app: dict, body: bytes = _STREAM_BODY, headers: dict | None = None):
        request = self._request(app, body=body)
        if headers:
            request.headers.update(headers)
        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse), \
                patch.object(anthropic_proxy, "_EARLY_COMMIT_KEEPALIVE_SECONDS", 0.02):
            resp = await _proxy_handler(request)
        await self._drain(app)
        return resp


class SlowUpstreamTests(_EarlyCase):
    async def test_keepalives_then_the_upstream_stream(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(list(_OK_SSE))
        app = self._app(_SlowClient([upstream], delay=0.2), _MultiKeyPool([_key("k1")]))

        resp = await self._serve(app)

        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertEqual(resp.chunks[0], _KEEPALIVE)
        self.assertGreaterEqual(resp.chunks.count(_KEEPALIVE), 2)
        self.assertEqual(b"".join(resp.chunks).replace(_KEEPALIVE, b""), b"".join(_OK_SSE))
        self.assertTrue(resp.eof_written)
        self.assertTrue(upstream.closed)

    async def test_late_overload_becomes_an_sse_overloaded_error(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(
            [b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'],
            status_code=529, content_type="application/json",
        )
        app = self._app(_SlowClient([upstream], delay=0.1), _MultiKeyPool([_key("k1")]))

        resp = await self._serve(app)

        self.assertEqual(resp.status, 200)
        [event] = _sse_errors(resp.chunks)
        self.assertEqual(event["error"]["type"], "overloaded_error")
        self.assertEqual(event["error"]["message"], "Overloaded")
        # The SDK prefers a top-level message over the JSON it would otherwise
        # show, and only the nested type tells Claude Code to retry.
        self.assertNotIn("message", event)
        self.assertTrue(resp.eof_written)

    async def test_unclassified_4xx_is_an_sse_error_not_raw_json(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(
            [b'{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long"}}'],
            status_code=400, content_type="application/json",
            headers={"request-id": "req_011abc"},
        )
        app = self._app(_SlowClient([upstream], delay=0.1), _MultiKeyPool([_key("k1")]))

        resp = await self._serve(app)

        [event] = _sse_errors(resp.chunks)
        self.assertEqual(event["error"], {
            "type": "invalid_request_error", "message": "prompt is too long",
        })
        # The request-id header the client can no longer see.
        self.assertEqual(event["request_id"], "req_011abc")
        self.assertNotIn(b'"prompt is too long"}}', b"".join(resp.chunks).replace(
            b"event: error\ndata: " + json.dumps(event).encode(), b""))
        self.assertTrue(upstream.closed)

    async def test_exhausted_attempts_are_retryable_for_claude_code(self) -> None:
        errors = [httpx.ConnectError("refused") for _ in range(3)]
        app = self._app(_SlowClient(errors, delay=0.1), _MultiKeyPool([_key("k1")]))

        resp = await self._serve(app)

        [event] = _sse_errors(resp.chunks)
        # A proxy-side 502 would be retried by the SDK as a status; an SSE
        # error only is when it reads as overloaded.
        self.assertEqual(event["error"]["type"], "overloaded_error")
        self.assertIn("attempts failed", event["error"]["message"])
        self.assertTrue(resp.eof_written)

    async def test_a_crash_after_commit_still_ends_the_stream_and_alerts(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(list(_OK_SSE))
        app = self._app(_SlowClient([upstream], delay=0.1), _MultiKeyPool([_key("k1")]))

        with patch.object(anthropic_proxy, "_is_upstream_overload", side_effect=RuntimeError("boom")):
            upstream.status_code = 503
            resp = await self._serve(app)

        [event] = _sse_errors(resp.chunks)
        self.assertEqual(event["error"]["type"], "overloaded_error")
        self.assertTrue(resp.eof_written)
        self.assertTrue(any("RuntimeError" in text for text in self.sent))

    async def test_client_gone_at_commit_cancels_the_upstream_wait(self) -> None:
        for gone in (_GoneAtPrepare, _GoneAtFirstWrite):
            with self.subTest(gone.__name__):
                upstream = _FakeStreamingUpstreamResponse(list(_OK_SSE))
                client = _SlowClient([upstream], delay=5.0)
                app = self._app(client, _MultiKeyPool([_key("k1")]))

                # Raised as the hang-up it is -- not turned into an SSE error
                # for a client that is no longer there, and not alerted on.
                with patch("smart_proxy.anthropic_proxy.web.StreamResponse", gone):
                    with self.assertRaises(ConnectionResetError):
                        await _proxy_handler(self._request(app))
                await self._drain(app)

                self.assertTrue(client.cancelled)
                self.assertEqual(self.sent, [])

    async def test_failure_after_the_stream_ended_writes_nothing_more(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(list(_OK_SSE))
        app = self._app(_SlowClient([upstream], delay=0.1), _MultiKeyPool([_key("k1")]))
        app["usage_tracker"] = object()  # usage accounting runs after write_eof

        with patch.object(anthropic_proxy, "extract_usage_from_sse", side_effect=RuntimeError("boom")):
            resp = await self._serve(app)

        self.assertEqual(b"".join(resp.chunks).replace(_KEEPALIVE, b""), b"".join(_OK_SSE))
        self.assertEqual(_sse_errors(resp.chunks), [])
        self.assertTrue(any("RuntimeError" in text for text in self.sent))


class UnchangedTests(_EarlyCase):
    async def test_fast_upstream_keeps_its_own_status(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(
            [b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'],
            status_code=529, content_type="application/json",
        )
        app = self._app(_SlowClient([upstream], delay=0.0), _MultiKeyPool([_key("k1")]), early=5.0)

        resp = await self._serve(app)

        self.assertEqual(resp.status, 529)

    async def test_non_streamed_request_is_never_committed_early(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(
            [b'{"type":"message"}'], content_type="application/json",
        )
        app = self._app(_SlowClient([upstream], delay=0.1), _MultiKeyPool([_key("k1")]))

        resp = await self._serve(app, body=b'{"model":"claude-opus-5","messages":[]}')

        self.assertNotIn(_KEEPALIVE, resp.chunks)
        self.assertEqual(resp.chunks, [b'{"type":"message"}'])

    async def test_openai_compat_loopback_is_never_committed_early(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(
            [b'{"type":"error","error":{"type":"invalid_request_error","message":"bad"}}'],
            status_code=400, content_type="application/json",
        )
        app = self._app(_SlowClient([upstream], delay=0.1), _MultiKeyPool([_key("k1")]))

        resp = await self._serve(app, headers={"x-smart-proxy-openai-compat": "1"})

        self.assertEqual(resp.status, 400)
        self.assertNotIn(_KEEPALIVE, resp.chunks)

    async def test_disabled_with_zero(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(list(_OK_SSE))
        app = self._app(_SlowClient([upstream], delay=0.1), _MultiKeyPool([_key("k1")]), early=0)

        resp = await self._serve(app)

        self.assertNotIn(_KEEPALIVE, resp.chunks)


class RealWireTests(_EarlyCase):
    """Fakes cannot show what aiohttp actually puts on the socket."""

    async def _over_the_wire(self, upstream_items: list, delay: float) -> tuple[int, str, bytes]:
        proxy = web.Application()
        for name, value in self._app(_SlowClient(upstream_items, delay=delay), _MultiKeyPool([_key("k1")])).items():
            proxy[name] = value
        proxy.router.add_post("/v1/messages", _proxy_handler)
        server = TestServer(proxy)
        await server.start_server()
        try:
            with patch.object(anthropic_proxy, "_EARLY_COMMIT_KEEPALIVE_SECONDS", 0.02):
                async with httpx.AsyncClient(trust_env=False) as client:
                    r = await client.post(
                        str(server.make_url("/v1/messages")),
                        content=_STREAM_BODY,
                        headers={"Authorization": "Bearer sp-test-key-0123456789ab"},
                    )
            return r.status_code, r.headers["content-type"], r.content
        finally:
            await server.close()

    async def test_success_is_one_clean_sse_stream(self) -> None:
        status, ctype, body = await self._over_the_wire(
            [_FakeStreamingUpstreamResponse(list(_OK_SSE))], delay=0.2,
        )

        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/event-stream"))
        self.assertTrue(body.startswith(_KEEPALIVE))
        self.assertEqual(body.replace(_KEEPALIVE, b""), b"".join(_OK_SSE))

    async def test_failure_is_one_sse_error_and_nothing_else(self) -> None:
        status, _, body = await self._over_the_wire(
            [_FakeStreamingUpstreamResponse(
                [b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'],
                status_code=529, content_type="application/json",
            )],
            delay=0.2,
        )

        self.assertEqual(status, 200)
        rest = body.replace(_KEEPALIVE, b"")
        self.assertEqual(rest, b'event: error\ndata: {"type": "error", "error": '
                               b'{"type": "overloaded_error", "message": "Overloaded"}}\n\n')


if __name__ == "__main__":
    unittest.main()
