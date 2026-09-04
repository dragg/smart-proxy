# tests/test_proxy_upstream_overload.py
"""Upstream overload must reach the client, and only our own faults must alert.

On 2026-08-21 Anthropic returned 89 `529 overloaded_error` responses in five
hours. The proxy already knew the right answer -- hand the overload to the
caller as a clean 529 so its own retry engine (backoff, jitter, retry-after)
takes over -- but that branch only existed on the streaming path, where the
overload arrives as an SSE event inside a 200. An overload delivered as an HTTP
529 *status* fell into the generic `status_code >= 500` retry, which burned all
three attempts against the same sticky key inside one second and then answered
502. All 29 client-visible failures landed on one consumer, whose PHP SDK never
saw a 529 and so never retried.

Two behaviours are pinned here:

* an upstream 5xx is retried only while a *different, untried* key exists;
  once they are exhausted the upstream status is surfaced verbatim;
* the "all attempts failed" alert fires only when at least one attempt failed
  for a reason of ours -- upstream capacity problems are not our outage and
  were explicitly out of alerting scope.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy import anthropic_proxy
from smart_proxy.claude_code_identity import ClaudeCodeVersion, DEFAULT_CLAUDE_CODE_VERSION
from smart_proxy.anthropic_proxy import (
    _AttemptFailure,
    _caller_label,
    _proxy_handler,
    _summarise_attempt_failures,
)
from tests.test_anthropic_proxy_oauth_messages import (
    _FakeDb,
    _FakeRequest,
    _FakeStreamResponse,
    _FakeStreamingUpstreamResponse,
    _FakeUpstreamResponse,
)

_OVERLOADED = (
    b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
)


class _MultiKeyPool:
    """Hands out each key once, the way pick() rotates over a real pool."""

    _REFRESH_BLOCKED = object()

    def __init__(self, keys: list, token: str = "oauth-access-token") -> None:
        self._keys = list(keys)
        self._cooled: set[str] = set()
        self._token = token
        self._names = {"sp-test-key-0123456789ab": "Acme (webapp)"}

    def check_auth(self, token: str) -> bool:
        return bool(token)

    def pick(self, model: str | None = None, *, fallback_for: str | None = None,
             exclude: set[str] | None = None):
        """Sticky, exactly like the real pool.

        The real pick() keeps returning the SAME key until it becomes
        unavailable (banned / low_balance / cooldown) -- serving a request does
        not rotate it. An earlier version of this fake rotated on use, which hid
        the fact that the production 5xx path could never reach a second key.
        """
        del model, fallback_for
        for key in self._keys:
            if exclude and key.key_id in exclude:
                continue
            if key.key_id not in self._cooled:
                return key
        return None

    def has_scoped_fallback(self, proxy_key: str) -> bool:
        return False

    def next_available_in(self, model=None, *, fallback_for=None) -> int:  # noqa: ANN001
        return 0

    def proxy_key_name(self, proxy_key: str) -> str:
        return self._names.get(proxy_key, "")

    async def ensure_valid_token(self, key, client, **kwargs) -> str:  # noqa: ANN001, ANN003
        del kwargs
        return self._token

    def cooldown(self, key, seconds=None, *, model=None) -> None:  # noqa: ANN001
        self._cooled.add(key.key_id)

    async def deactivate(self, key, **kwargs) -> None:  # noqa: ANN001, ANN003
        return None


class _SequencedClient:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.sends = 0

    def build_request(self, method: str, url: str, headers: dict, content):  # noqa: ANN001
        from types import SimpleNamespace

        return SimpleNamespace(method=method, url=url, headers=headers, content=content)

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        self.sends += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _key(key_id: str):
    from smart_proxy.anthropic_proxy import _AnthropicKey

    return _AnthropicKey(
        key_id=key_id, key_type="oauth", status="active", api_key=None,
        access_token="t", refresh_token="r", client_id="c",
        expires_at=9999999999999, scopes="user:inference",
    )


class AttemptFailureSummaryTests(unittest.TestCase):
    def test_repeats_are_collapsed_with_counts(self) -> None:
        failures = [
            _AttemptFailure("upstream 529", ours=False),
            _AttemptFailure("upstream 529", ours=False),
            _AttemptFailure("upstream 529", ours=False),
        ]
        self.assertEqual(_summarise_attempt_failures(failures), "upstream 529 ×3")

    def test_mixed_reasons_are_all_listed(self) -> None:
        failures = [
            _AttemptFailure("upstream 529", ours=False),
            _AttemptFailure("transport: ConnectError", ours=True),
        ]
        summary = _summarise_attempt_failures(failures)
        self.assertIn("upstream 529", summary)
        self.assertIn("transport: ConnectError", summary)

    def test_caller_label_keeps_the_name_and_the_key_tail(self) -> None:
        label = _caller_label("Acme (webapp)", "sp-0123456789abcdef0123456789ffee11")
        self.assertIn("Acme (webapp)", label)
        self.assertIn("ffee11", label)          # last 6 chars, readable
        self.assertNotIn("sp-0123456789ab", label)  # never the whole key


class UpstreamOverloadTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, client, pool) -> dict:
        notifier = MagicMock()
        self.sent: list[str] = []

        async def notify(text: str) -> bool:
            self.sent.append(text)
            return True

        notifier.notify = notify
        from smart_proxy.notifier import AlertThrottle

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

    async def _drain(self, app: dict) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        if app["_alert_tasks"]:
            await asyncio.gather(*app["_alert_tasks"], return_exceptions=True)

    def _request(self, app: dict):
        return _FakeRequest(
            app=app,
            headers={
                "Authorization": "Bearer sp-test-key-0123456789ab",
                "User-Agent": "anthropic/PHP 0.42.0",
            },
            body=b'{"model":"claude-opus-5","messages":[]}',
        )

    async def test_overload_is_surfaced_as_529_when_no_other_key_exists(self) -> None:
        """Production shape: one active key, so there is nothing to retry on."""
        client = _SequencedClient([
            _FakeUpstreamResponse(529, _OVERLOADED, headers={"retry-after": "7"}),
        ])
        pool = _MultiKeyPool([_key("key-one")])
        app = self._app(client, pool)

        resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        # The client sees the real overload, not a 502 it cannot interpret.
        self.assertEqual(resp.status, 529)
        self.assertIn(b"overloaded_error", resp.body)
        self.assertEqual(resp.headers.get("retry-after"), "7")
        # And we stopped after one attempt instead of hammering the same key.
        self.assertEqual(client.sends, 1)
        # Anthropic being out of capacity is not our outage.
        self.assertEqual(self.sent, [])

    async def test_overload_retries_on_a_second_key_before_surfacing(self) -> None:
        client = _SequencedClient([
            _FakeUpstreamResponse(529, _OVERLOADED),
            _FakeStreamingUpstreamResponse([
                b'event: message_start\ndata: {"type":"message_start"}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]),
        ])
        pool = _MultiKeyPool([_key("key-one"), _key("key-two")])
        app = self._app(client, pool)

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
            resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 200)
        self.assertEqual(client.sends, 2)
        self.assertEqual(self.sent, [])

    async def test_a_plain_500_still_retries_instead_of_being_handed_over(self) -> None:
        """Narrowness matters: only an overload is handed straight to the client.

        A one-off 500 is often cleared by the next attempt on the same key, and
        many callers have no retry logic at all -- surfacing those would trade
        one incident for a quieter, wider one.
        """
        client = _SequencedClient([
            _FakeUpstreamResponse(500, b'{"type":"error","error":{"type":"api_error"}}'),
            _FakeStreamingUpstreamResponse([
                b'event: message_start\ndata: {"type":"message_start"}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]),
        ])
        pool = _MultiKeyPool([_key("key-one")])
        app = self._app(client, pool)

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
            resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 200)
        self.assertEqual(client.sends, 2)   # retried the same key, and it worked

    async def test_overload_body_without_a_529_status_is_also_surfaced(self) -> None:
        client = _SequencedClient([_FakeUpstreamResponse(500, _OVERLOADED)])
        pool = _MultiKeyPool([_key("key-one")])
        app = self._app(client, pool)

        resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 500)
        self.assertIn(b"overloaded_error", resp.body)
        self.assertEqual(client.sends, 1)

    async def test_an_html_error_page_does_not_crash_the_surface_path(self) -> None:
        """Anthropic sits behind Cloudflare, whose 5xx pages carry a charset.

        aiohttp rejects a charset in the content_type kwarg, so forwarding one
        verbatim raised ValueError -- a 500 plus an alert, strictly worse than
        the 502 this change set out to remove.
        """
        client = _SequencedClient([
            _FakeUpstreamResponse(
                529, b"<html>Overloaded</html>",
                content_type="text/html; charset=UTF-8",
            ),
        ])
        pool = _MultiKeyPool([_key("key-one")])
        app = self._app(client, pool)

        resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 529)
        self.assertEqual(resp.content_type, "text/html")
        self.assertEqual(self.sent, [])

    async def test_our_own_failures_still_alert_and_name_the_caller(self) -> None:
        import httpx

        client = _SequencedClient([
            httpx.ConnectError("connection refused"),
            httpx.ConnectError("connection refused"),
            httpx.ConnectError("connection refused"),
        ])
        pool = _MultiKeyPool([_key("key-one"), _key("key-two"), _key("key-three")])
        app = self._app(client, pool)

        resp = await _proxy_handler(self._request(app))
        await self._drain(app)

        self.assertEqual(resp.status, 502)
        self.assertEqual(len(self.sent), 1, self.sent)
        # Readable at a glance: who was hit, and why every attempt died.
        self.assertIn("Acme (webapp)", self.sent[0])
        self.assertIn("ConnectError", self.sent[0])


if __name__ == "__main__":
    unittest.main()
