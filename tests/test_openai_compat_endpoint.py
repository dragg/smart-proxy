from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiohttp
import httpx
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from smart_proxy.anthropic_proxy import _forward_headers
from smart_proxy.openai_compat import (
    AnthropicToOpenAIStream,
    OpenAICompatStats,
    setup_openai_compat,
)

ANTHROPIC_JSON = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "Hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 4},
}

ANTHROPIC_SSE = "".join(
    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
    for e in [
        {
            "type": "message_start",
            "message": {"id": "msg_02", "model": "claude-sonnet-5", "usage": {"input_tokens": 7}},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
).encode()

# Partial stream: message_start + one text delta, deliberately missing
# content_block_stop / message_delta / message_stop so the connection gets
# force-closed mid-stream (see _FakeInner "sse_abort" kind below).
PARTIAL_ANTHROPIC_SSE = "".join(
    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
    for e in [
        {
            "type": "message_start",
            "message": {"id": "msg_03", "model": "claude-sonnet-5", "usage": {"input_tokens": 7}},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Partial"}},
    ]
).encode()


class _FakeInner:
    """Stands in for the proxy's own /v1/messages endpoint."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.response: tuple = ("json", 200, ANTHROPIC_JSON, {})

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {"body": json.loads(await request.read()), "headers": dict(request.headers)}
        )
        kind, status, payload, headers = self.response
        if kind == "json":
            return web.Response(
                status=status,
                body=json.dumps(payload).encode(),
                content_type="application/json",
                headers=headers,
            )
        resp = web.StreamResponse(status=status)
        resp.headers["content-type"] = "text/event-stream; charset=utf-8"
        await resp.prepare(request)
        await resp.write(payload)
        if kind == "sse_abort":
            # Force-close the transport mid-stream (no terminating chunk /
            # write_eof) so the outer proxy's httpx read fails or EOFs
            # before it ever sees message_stop.
            request.transport.close()
            return resp
        await resp.write_eof()
        return resp


class OpenAICompatEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.inner = _FakeInner()
        inner_app = web.Application()
        inner_app.router.add_post("/v1/messages", self.inner.handle)
        self.inner_server = TestServer(inner_app)
        await self.inner_server.start_server()

        outer_app = web.Application()
        outer_app["anthropic_pool"] = SimpleNamespace(
            check_auth=lambda token: token == "sp-test"
        )
        outer_app["openai_compat_loopback_base"] = str(
            self.inner_server.make_url("")
        ).rstrip("/")
        setup_openai_compat(outer_app, default_max_tokens=8192, auto_cache=True)
        self.outer_app = outer_app
        self.client = TestClient(TestServer(outer_app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.inner_server.close()

    def _chat(self, **overrides):
        payload = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload.update(overrides)
        return payload

    async def test_non_streaming_roundtrip(self):
        # setup_openai_compat's own on_startup hook must have created a
        # dedicated loopback client (not reused from the outer proxy's
        # shared http_client pool) by the time the server is up.
        self.assertIsInstance(
            self.outer_app["openai_compat_http_client"], httpx.AsyncClient
        )
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer sp-test"},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("x-smart-proxy-openai-compat"), "1")
        data = await resp.json()
        self.assertEqual(data["object"], "chat.completion")
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        # loopback carried the client token and anthropic body
        sent = self.inner.requests[0]
        self.assertEqual(sent["headers"].get("Authorization"), "Bearer sp-test")
        self.assertEqual(sent["body"]["model"], "claude-sonnet-5")
        self.assertIn("max_tokens", sent["body"])
        last_block = sent["body"]["messages"][-1]["content"][-1]
        self.assertEqual(
            last_block["cache_control"], {"type": "ephemeral", "ttl": "1h"}
        )
        # loopback tags itself so the native handler attributes usage to the
        # compat layer (x-smart-proxy-openai-compat: 1)
        self.assertEqual(sent["headers"].get("x-smart-proxy-openai-compat"), "1")

    async def test_streaming_roundtrip(self):
        self.inner.response = ("sse", 200, ANTHROPIC_SSE, {})
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(stream=True),
            headers={"Authorization": "Bearer sp-test"},
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
        self.assertEqual(resp.headers.get("x-smart-proxy-openai-compat"), "1")
        body = await resp.read()
        text = body.decode()
        self.assertIn('"chat.completion.chunk"', text)
        self.assertIn('"content":"Hi"', text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))
        # inner saw stream:true
        self.assertIs(self.inner.requests[0]["body"].get("stream"), True)

    async def test_streaming_mid_stream_abort_still_terminates_with_done(self):
        # Upstream writes a partial SSE stream (message_start + one text
        # delta) then force-closes the connection before message_stop. The
        # outer proxy's httpx read will fail or EOF mid-stream; the client
        # facing side must still receive a finish chunk + [DONE] rather than
        # hanging on an unterminated stream.
        self.inner.response = ("sse_abort", 200, PARTIAL_ANTHROPIC_SSE, {})
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(stream=True),
            headers={"Authorization": "Bearer sp-test"},
        )
        self.assertEqual(resp.status, 200)
        body = await resp.read()
        text = body.decode()
        self.assertIn('"content":"Partial"', text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))

    async def test_auth_rejected(self):
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer wrong"},
        )
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "invalid_api_key")

    async def test_invalid_body_rejected(self):
        resp = await self.client.post(
            "/v1/chat/completions",
            data=b"{not json",
            headers={
                "Authorization": "Bearer sp-test",
                "content-type": "application/json",
            },
        )
        self.assertEqual(resp.status, 400)

    async def test_upstream_429_passthrough(self):
        self.inner.response = (
            "json",
            429,
            {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            {"retry-after": "17", "x-should-retry": "false"},
        )
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer sp-test"},
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers.get("retry-after"), "17")
        self.assertEqual(resp.headers.get("x-should-retry"), "false")
        data = await resp.json()
        self.assertEqual(data["error"]["type"], "rate_limit_error")

    async def test_stats_counting_and_endpoint(self):
        await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer sp-test"},
        )
        resp = await self.client.get(
            "/_openai_compat_stats", headers={"Authorization": "Bearer sp-test"}
        )
        self.assertEqual(resp.status, 200)
        snap = await resp.json()
        self.assertEqual(snap["requests"], 1)
        self.assertEqual(snap["by_model"], {"claude-sonnet-5": 1})
        self.assertEqual(snap["prompt_tokens"], 10)
        self.assertEqual(snap["completion_tokens"], 4)

    async def test_stats_endpoint_requires_auth(self):
        resp = await self.client.get("/_openai_compat_stats")
        self.assertEqual(resp.status, 401)


class ModelsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.passthrough_calls: list[str] = []

        async def fake_passthrough(request: web.Request) -> web.Response:
            self.passthrough_calls.append(request.path)
            return web.Response(
                body=b'{"native": true}', content_type="application/json"
            )

        app = web.Application()
        app["anthropic_pool"] = SimpleNamespace(check_auth=lambda t: t == "sp-test")
        setup_openai_compat(app, models_passthrough=fake_passthrough)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_openai_client_gets_static_list(self):
        resp = await self.client.get(
            "/v1/models", headers={"Authorization": "Bearer sp-test"}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("x-smart-proxy-openai-compat"), "1")
        data = await resp.json()
        self.assertEqual(data["object"], "list")
        ids = [m["id"] for m in data["data"]]
        self.assertIn("claude-sonnet-5", ids)
        self.assertTrue(all(i.startswith("claude-") for i in ids))
        self.assertEqual(data["data"][0]["object"], "model")
        self.assertEqual(data["data"][0]["owned_by"], "anthropic")
        self.assertEqual(self.passthrough_calls, [])

    async def test_native_anthropic_client_is_delegated(self):
        resp = await self.client.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer sp-test",
                "anthropic-version": "2023-06-01",
            },
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"native": True})
        self.assertEqual(self.passthrough_calls, ["/v1/models"])

    async def test_models_requires_auth(self):
        resp = await self.client.get("/v1/models")
        self.assertEqual(resp.status, 401)


def _sse(payload: dict) -> bytes:
    return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n".encode()


class StreamEventsAfterDoneTests(unittest.TestCase):
    """Once an `error` event has terminated the stream with [DONE], any
    further upstream events (e.g. a stray text_delta after a disconnect
    race) must be swallowed rather than reopening the SSE stream."""

    def test_events_fed_after_done_produce_no_output(self):
        conv = AnthropicToOpenAIStream(created=1)
        conv.feed(
            _sse(
                {
                    "type": "message_start",
                    "message": {"id": "m", "model": "m", "usage": {}},
                }
            )
        )
        terminated = conv.feed(
            _sse(
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "boom"},
                }
            )
        )
        self.assertTrue(terminated.rstrip().endswith(b"data: [DONE]"))

        trailing = conv.feed(
            _sse(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "late"},
                }
            )
        )
        self.assertEqual(trailing, b"")
        self.assertEqual(conv.finish(), b"")


from smart_proxy.anthropic_proxy import create_app  # noqa: E402
from smart_proxy.config import Settings  # noqa: E402


class CreateAppWiringTests(unittest.TestCase):
    def _canonicals(self, app) -> set[str]:
        return {r.canonical for r in app.router.resources()}

    def test_compat_routes_registered_by_default(self):
        app = create_app("./ignored.db", oauth_smoke_enabled=False)
        canonicals = self._canonicals(app)
        self.assertIn("/v1/chat/completions", canonicals)
        self.assertIn("/v1/models", canonicals)
        self.assertIn("/_openai_compat_stats", canonicals)
        self.assertIsNotNone(app.get("openai_compat_loopback_base"))
        self.assertEqual(app["openai_compat_default_max_tokens"], 8192)

    def test_compat_routes_absent_when_disabled(self):
        app = create_app(
            "./ignored.db", oauth_smoke_enabled=False, openai_compat_enabled=False
        )
        canonicals = self._canonicals(app)
        self.assertNotIn("/v1/chat/completions", canonicals)
        self.assertNotIn("/_openai_compat_stats", canonicals)

    def test_compat_routes_precede_catch_all(self):
        app = create_app("./ignored.db", oauth_smoke_enabled=False)
        canonicals = [r.canonical for r in app.router.resources()]
        self.assertLess(
            canonicals.index("/v1/chat/completions"),
            canonicals.index("/{path}"),
        )

    def test_settings_defaults(self):
        s = Settings()
        self.assertTrue(s.anthropic_proxy_openai_compat_enabled)
        self.assertEqual(s.anthropic_proxy_openai_compat_default_max_tokens, 8192)
        self.assertTrue(s.anthropic_proxy_openai_compat_auto_cache)
        self.assertEqual(s.anthropic_proxy_openai_compat_cache_ttl, "1h")


class LoopbackMarkerHeaderTests(unittest.TestCase):
    def test_forward_headers_drops_compat_marker(self):
        # The native handler reads x-smart-proxy-openai-compat for usage attribution
        # but must never forward it to the Anthropic upstream.
        req = make_mocked_request(
            "POST",
            "/v1/messages",
            headers={
                "x-smart-proxy-openai-compat": "1",
                "user-agent": "smart-proxy-openai-compat/1.0",
                "anthropic-version": "2023-06-01",
            },
        )
        fwd = _forward_headers(req)
        self.assertNotIn("x-smart-proxy-openai-compat", {k.lower() for k in fwd})
        # unrelated headers still pass through
        self.assertIn("anthropic-version", {k.lower() for k in fwd})


if __name__ == "__main__":
    unittest.main()
