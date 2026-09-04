"""End-to-end wiring of the Claude Code version through the proxy handler.

The unit behaviour of the version holder lives in test_claude_code_identity.py.
What is checked here is that the handler actually renders it into the billing
block Anthropic gates on, and that learning is committed only when upstream
accepted the request that taught it.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

from unittest import mock

from aiohttp import web

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_anthropic_proxy_oauth_messages import (  # noqa: E402
    _FakeDb,
    _FakeHttpClient,
    _FakePool,
    _FakeRequest,
    _FakeStreamingUpstreamResponse,
    AnthropicProxyOAuthMessagesTests as _Harness,
)

from smart_proxy.anthropic_proxy import _proxy_handler  # noqa: E402
from smart_proxy.claude_code_identity import (  # noqa: E402
    ClaudeCodeVersion,
    render_billing_header,
)

NEWER = "2.1.288"
CLI_UA = f"claude-cli/{NEWER} (external, sdk-cli)"
SDK_UA = "Anthropic/Python 0.80.0"


def _billing_block(version: str) -> dict:
    return {"type": "text", "text": f"x-anthropic-billing-header: cc_version={version}; cc_entrypoint=sdk-cli;"}


def _body(*, system: list | None = None) -> bytes:
    payload: dict = {"model": "claude-fable-5-1", "messages": [{"role": "user", "content": "ping"}]}
    if system is not None:
        payload["system"] = system
    return json.dumps(payload).encode()


async def _noop(*_a, **_kw):
    """Swallow response writes: the fake request has no transport behind it."""
    return None


class ClaudeCodeVersionWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _Harness()

    def _run(self, *, status: int, headers: dict, body: bytes, version: ClaudeCodeVersion):
        """Drive one request through the handler; return the body sent upstream."""

        async def run():
            client = _FakeHttpClient(
                _FakeStreamingUpstreamResponse(
                    [b"{}"], status_code=status, content_type="application/json"
                )
            )
            pool = _FakePool(self.harness._oauth_key())
            app = self.harness._app(client, pool, db=_FakeDb())
            app["claude_code_version"] = version
            req = _FakeRequest(
                app=app,
                headers={"Authorization": "Bearer sp-test", **headers},
                body=body,
            )
            # A non-error status reaches the passthrough path, which streams
            # the answer back through aiohttp. What this test is about happens
            # strictly before that, so the write side is stubbed out rather than
            # given a fake transport to satisfy.
            with mock.patch.object(web.StreamResponse, "prepare", _noop), \
                 mock.patch.object(web.StreamResponse, "write", _noop), \
                 mock.patch.object(web.StreamResponse, "write_eof", _noop):
                await _proxy_handler(req)
            assert client.last_build is not None
            return json.loads(client.last_build["content"])

        return asyncio.run(run())

    def test_sdk_client_gets_the_configured_version_in_the_billing_block(self) -> None:
        # The bug: this block is what Anthropic reads its model version gate
        # from, and a client without one of its own depends entirely on it.
        version = ClaudeCodeVersion("2.1.260")
        sent = self._run(status=200, headers={"User-Agent": SDK_UA}, body=_body(), version=version)
        self.assertEqual(sent["system"][0]["text"], render_billing_header("2.1.260.a35"))

    def test_a_clients_own_billing_block_is_left_alone(self) -> None:
        version = ClaudeCodeVersion("2.1.260")
        own = _billing_block("9.9.9.1")
        sent = self._run(
            status=200, headers={"User-Agent": CLI_UA}, body=_body(system=[own]), version=version
        )
        self.assertEqual(sent["system"][0]["text"], own["text"])
        self.assertEqual(len(sent["system"]), 1)

    def test_newer_cli_version_is_adopted_after_upstream_accepts(self) -> None:
        version = ClaudeCodeVersion("2.1.260")
        self._run(
            status=200,
            headers={"User-Agent": CLI_UA},
            body=_body(system=[_billing_block(f"{NEWER}.777")]),
            version=version,
        )
        self.assertEqual(version.token, f"{NEWER}.777")

    def test_version_is_not_adopted_when_upstream_rejects(self) -> None:
        # A 400 is not filtered as a failure upstream — it reaches the success
        # path on its way to the client — so this is the case that would have
        # taught the proxy a version Anthropic refuses.
        version = ClaudeCodeVersion("2.1.260")
        self._run(
            status=400,
            headers={"User-Agent": CLI_UA},
            body=_body(system=[_billing_block(f"{NEWER}.777")]),
            version=version,
        )
        self.assertEqual(version.token, "2.1.260.a35")

    def test_adopted_version_is_served_to_later_sdk_clients(self) -> None:
        # The whole point: a real CLI session teaches the proxy, and the SDK
        # client that cannot speak for itself gets the benefit.
        version = ClaudeCodeVersion("2.1.260")
        self._run(
            status=200,
            headers={"User-Agent": CLI_UA},
            body=_body(system=[_billing_block(f"{NEWER}.777")]),
            version=version,
        )
        sent = self._run(status=200, headers={"User-Agent": SDK_UA}, body=_body(), version=version)
        self.assertEqual(sent["system"][0]["text"], render_billing_header(f"{NEWER}.777"))

    def test_sdk_client_cannot_teach_the_proxy(self) -> None:
        version = ClaudeCodeVersion("2.1.260")
        self._run(
            status=200,
            headers={"User-Agent": SDK_UA},
            body=_body(system=[_billing_block("9.9.9.1")]),
            version=version,
        )
        self.assertEqual(version.token, "2.1.260.a35")

    def test_autolearn_disabled_keeps_the_configured_floor(self) -> None:
        version = ClaudeCodeVersion("2.1.260", autolearn=False)
        self._run(
            status=200,
            headers={"User-Agent": CLI_UA},
            body=_body(system=[_billing_block(f"{NEWER}.777")]),
            version=version,
        )
        self.assertEqual(version.token, "2.1.260.a35")


if __name__ == "__main__":
    unittest.main()
