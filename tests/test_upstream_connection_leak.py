# tests/test_upstream_connection_leak.py
"""Every upstream response must give its pool slot back, whatever the caller does.

On 2026-10-07 the proxy stopped serving for two hours without crashing: all 100
connections of the shared httpx pool sat in CLOSE-WAIT, each one a streamed
upstream response that was never closed. The leak was a client that hung up
while we waited on Anthropic -- ``stream_resp.prepare()`` raised
``ClientConnectionResetError`` *before* the ``try/finally`` that closes ``r``.
After three weeks of that, every request queued 600s for a slot, failed with an
empty ``PoolTimeout`` three times and answered 502, and the alert about it died
of the same ``PoolTimeout`` because Telegram shared the pool.

Pinned here:

* a client disconnect before ``prepare()`` releases the upstream connection
  (unit-level with fakes, and against a real one-slot httpx pool);
* a dropped keep-alive at send is a retryable transport failure, not a 500;
* an exhausted pool fails fast and loud instead of 3x600s of silence;
* token rotation and alerting do not share the data-path pool.
"""
from __future__ import annotations

import asyncio
import gc
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from aiohttp.test_utils import TestServer

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy import anthropic_proxy
from smart_proxy.anthropic_proxy import _build_notifier, _proxy_handler
from smart_proxy.claude_code_identity import ClaudeCodeVersion, DEFAULT_CLAUDE_CODE_VERSION
from smart_proxy.notifier import AlertThrottle
from tests.test_anthropic_proxy_oauth_messages import (
    _FakeDb,
    _FakeRequest,
    _FakeStreamResponse,
    _FakeStreamingUpstreamResponse,
)
from tests.test_proxy_upstream_overload import _MultiKeyPool, _SequencedClient, _key

_COMMITTED_SSE = [
    b'event: message_start\ndata: {"type":"message_start"}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n',
]
_OK_SSE = _COMMITTED_SSE + [b'event: message_stop\ndata: {"type":"message_stop"}\n\n']


class _DisconnectedStreamResponse(_FakeStreamResponse):
    """The caller hung up while we were waiting on upstream."""

    async def prepare(self, request):  # noqa: ANN001
        raise ClientConnectionResetError("Cannot write to closing transport")


class _BrokenMidHeadUpstream(_FakeStreamingUpstreamResponse):
    """Sends message_start, then the connection drops before the commit marker."""

    def aiter_bytes(self):
        async def _gen():
            yield _COMMITTED_SSE[0]
            raise httpx.ReadError("connection reset by peer")

        return _gen()


class _RecordingClientPool(_MultiKeyPool):
    def __init__(self, keys: list) -> None:
        super().__init__(keys)
        self.refresh_clients: list = []

    async def ensure_valid_token(self, key, client, **kwargs) -> str:  # noqa: ANN001, ANN003
        self.refresh_clients.append(client)
        return await super().ensure_valid_token(key, client, **kwargs)


class _ProxyCase(unittest.IsolatedAsyncioTestCase):
    def _app(self, client, pool) -> dict:  # noqa: ANN001
        notifier = MagicMock()
        self.sent: list[str] = []

        async def notify(text: str) -> bool:
            self.sent.append(text)
            return True

        notifier.notify = notify
        return {
            "anthropic_pool": pool,
            "http_client": client,
            "usage_tracker": None,
            "disable_1m_context": False,
            "strip_system_phrase": "",
            "claude_like": False,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "db": _FakeDb(),
            "key_limiter": None,
            "_notifier": notifier,
            "_alert_throttle": AlertThrottle(window_seconds=1800),
            "_alert_tasks": set(),
        }

    def _request(self, app: dict, body: bytes = b'{"model":"claude-opus-5","stream":true,"messages":[]}'):
        return _FakeRequest(
            app=app,
            headers={
                "Authorization": "Bearer sp-test-key-0123456789ab",
                "User-Agent": "anthropic/PHP 0.42.0",
            },
            body=body,
        )

    async def _drain(self, app: dict) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        if app["_alert_tasks"]:
            await asyncio.gather(*app["_alert_tasks"], return_exceptions=True)


class ClientDisconnectReleasesUpstreamTests(_ProxyCase):
    async def test_stream_is_closed_when_the_caller_hung_up_before_prepare(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(list(_COMMITTED_SSE))
        app = self._app(_SequencedClient([upstream]), _MultiKeyPool([_key("k1")]))

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _DisconnectedStreamResponse):
            with self.assertRaises(ConnectionResetError):
                await _proxy_handler(self._request(app))

        self.assertTrue(upstream.closed)

    async def test_non_stream_body_is_closed_when_the_caller_hung_up_before_prepare(self) -> None:
        upstream = _FakeStreamingUpstreamResponse(
            [b'{"type":"message"}'], content_type="application/json",
        )
        app = self._app(_SequencedClient([upstream]), _MultiKeyPool([_key("k1")]))

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _DisconnectedStreamResponse):
            with self.assertRaises(ConnectionResetError):
                await _proxy_handler(self._request(app, body=b'{"model":"claude-opus-5","messages":[]}'))

        self.assertTrue(upstream.closed)

    async def test_pool_slot_is_free_again_after_the_caller_hung_up(self) -> None:
        """The thing that actually broke: a real httpx pool, one slot.

        Non-streamed on purpose. A half-read SSE stream is an async generator,
        and asyncio closes it when it is garbage-collected, which hands the slot
        back eventually. A body nobody started reading has no such finalizer:
        in production these were mostly GET /v1/models from a poller that hung
        up, and garbage collection never returned them.
        """

        async def models(request: web.Request) -> web.Response:
            return web.json_response({"data": []})

        async def ping(request: web.Request) -> web.Response:
            return web.Response(text="pong")

        upstream_app = web.Application()
        upstream_app.router.add_get("/v1/models", models)
        upstream_app.router.add_get("/ping", ping)
        server = TestServer(upstream_app)
        await server.start_server()
        client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=1),
            timeout=httpx.Timeout(5.0, pool=0.5),
            trust_env=False,
        )
        try:
            app = self._app(client, _MultiKeyPool([_key("k1")]))
            request = _FakeRequest(
                app=app,
                headers={"Authorization": "Bearer sp-test-key-0123456789ab"},
                body=b"",
                path="/v1/models",
            )
            request.method = "GET"
            base = str(server.make_url("")).rstrip("/")
            with patch.object(anthropic_proxy, "UPSTREAM_BASE", base), \
                    patch("smart_proxy.anthropic_proxy.web.StreamResponse", _DisconnectedStreamResponse):
                with self.assertRaises(ConnectionResetError):
                    await _proxy_handler(request)
            gc.collect()

            # Before the fix this raised httpx.PoolTimeout: the slot was gone for good.
            pong = await client.get(str(server.make_url("/ping")))
            self.assertEqual(pong.text, "pong")
        finally:
            await client.aclose()
            await server.close()


class TransportFailureTests(_ProxyCase):
    async def test_dropped_keep_alive_at_send_is_retried(self) -> None:
        """'Server disconnected without sending a response' is a stale pooled
        connection: upstream never saw the request, so a retry is safe."""
        client = _SequencedClient([
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            _FakeStreamingUpstreamResponse(list(_OK_SSE)),
        ])
        app = self._app(client, _MultiKeyPool([_key("k1")]))

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
            resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 200)
        self.assertEqual(client.sends, 2)
        self.assertEqual(self.sent, [])

    async def test_persistent_protocol_errors_end_in_an_alerted_502(self) -> None:
        client = _SequencedClient([
            httpx.RemoteProtocolError("Server disconnected without sending a response.")
            for _ in range(3)
        ])
        app = self._app(client, _MultiKeyPool([_key("k1")]))

        resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 502)
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn("RemoteProtocolError", self.sent[0])

    async def test_connection_lost_before_commit_is_retried_not_a_500(self) -> None:
        broken = _BrokenMidHeadUpstream([])
        client = _SequencedClient([broken, _FakeStreamingUpstreamResponse(list(_OK_SSE))])
        app = self._app(client, _MultiKeyPool([_key("k1")]))

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
            resp = await _proxy_handler(self._request(app))

        self.assertEqual(resp.status, 200)
        self.assertEqual(client.sends, 2)
        self.assertTrue(broken.closed)

    async def test_exhausted_pool_fails_fast_and_alerts(self) -> None:
        """Retrying a PoolTimeout only queues again on the same full pool."""
        client = _SequencedClient([httpx.PoolTimeout("")])
        app = self._app(client, _MultiKeyPool([_key("k1"), _key("k2"), _key("k3")]))

        resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.headers.get("retry-after"), "5")
        self.assertEqual(client.sends, 1)
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn("pool exhausted", self.sent[0])


class ControlPlaneIsolationTests(_ProxyCase):
    async def test_token_refresh_does_not_use_the_data_path_client(self) -> None:
        oauth_client = object()
        pool = _RecordingClientPool([_key("k1")])
        client = _SequencedClient([_FakeStreamingUpstreamResponse(list(_OK_SSE))])
        app = self._app(client, pool)
        app["oauth_http_client"] = oauth_client

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
            await _proxy_handler(self._request(app))

        self.assertEqual(pool.refresh_clients, [oauth_client])

    def test_alerts_do_not_use_the_data_path_client(self) -> None:
        alert_client = object()
        notifier = _build_notifier({
            "telegram_bot_token": "token",
            "telegram_chat_id": "chat",
            "http_client": object(),
            "alert_http_client": alert_client,
        })

        self.assertIs(notifier._client, alert_client)


if __name__ == "__main__":
    unittest.main()
