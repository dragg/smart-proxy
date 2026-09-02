from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy import anthropic_debug_proxy


class _FakeStreamResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self.prepared = False
        self.eof_written = False

    async def prepare(self, request) -> "_FakeStreamResponse":  # noqa: ANN001
        self.prepared = True
        return self

    async def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    async def write_eof(self) -> None:
        self.eof_written = True


class _FakeStreamingUpstreamResponse:
    def __init__(self, owner: "_FakeAsyncClient") -> None:
        self._owner = owner
        self.status_code = 200
        self.headers = {
            "content-type": "text/event-stream; charset=utf-8",
            "x-test-header": "ok",
        }
        self.closed = False

    def aiter_bytes(self):
        async def _gen():
            if self._owner.closed:
                raise httpx.ReadError(
                    "client closed before stream consumption",
                    request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
                )
            yield b"event: message_start\n"
            yield b"data: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":10}}}\n\n"

        return _gen()

    async def aclose(self) -> None:
        self.closed = True


class _FakeAsyncClient:
    last_instance: "_FakeAsyncClient | None" = None

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.closed = False
        self.sent_requests: list[SimpleNamespace] = []
        self.response = _FakeStreamingUpstreamResponse(self)
        _FakeAsyncClient.last_instance = self

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        self.closed = True
        return False

    def build_request(self, method: str, url: str, headers: dict, content: bytes | None):
        return SimpleNamespace(method=method, url=url, headers=dict(headers), content=content)

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        self.sent_requests.append(req)
        return self.response


class _FakeRequest:
    def __init__(self, body: bytes) -> None:
        self.method = "POST"
        self.path = "/v1/messages"
        self.query_string = "beta=true"
        self.headers = {
            "Authorization": "Bearer test-token",
            "Content-Type": "application/json",
            "User-Agent": "claude-cli/2.1.92 (external, cli)",
        }
        self.app = {"capture_seq_get": self._capture_seq}
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def _capture_seq(self) -> int:
        return 1


class AnthropicDebugProxyTests(unittest.TestCase):
    def test_streaming_keeps_upstream_client_alive_until_chunks_are_forwarded(self) -> None:
        async def run() -> None:
            req = _FakeRequest(b'{"model":"claude-haiku-4-5-20251001","stream":true}')

            with (
                patch("smart_proxy.anthropic_debug_proxy.RAW_CAPTURE_DIR", ""),
                patch("smart_proxy.anthropic_debug_proxy.httpx.AsyncClient", _FakeAsyncClient),
                patch("smart_proxy.anthropic_debug_proxy.web.StreamResponse", _FakeStreamResponse),
            ):
                resp = await anthropic_debug_proxy._proxy_handler(req)

            self.assertTrue(resp.prepared)
            self.assertTrue(resp.eof_written)
            self.assertEqual(
                b"".join(resp.chunks),
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n',
            )
            self.assertIsNotNone(_FakeAsyncClient.last_instance)
            assert _FakeAsyncClient.last_instance is not None
            self.assertTrue(_FakeAsyncClient.last_instance.closed)
            self.assertTrue(_FakeAsyncClient.last_instance.response.closed)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
