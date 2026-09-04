# tests/test_anthropic_pool_best_effort_writes.py
"""Failure-path writes must not turn a database outage into a client 500.

The happy path never touches the database, so a dead database is survivable —
but the *failure* paths did touch it, unguarded. An upstream 429 recorded a
rate-limit row; an upstream 401 deactivated the key; a low-balance answer and a
standby promotion each wrote a row. With the database down every one of those
raised into the handler, which answered 500 — so the proxy fell over precisely
when it was supposed to fail over.

All five already mutate memory *before* the write, so guarding the write costs
durability only: the pool still bans, promotes and throttles correctly, the
request still fails over, and the lost row is reported rather than swallowed.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy import anthropic_proxy
from smart_proxy.claude_code_identity import ClaudeCodeVersion, DEFAULT_CLAUDE_CODE_VERSION
from smart_proxy.anthropic_proxy import _AnthropicKey, AnthropicKeyPool
from smart_proxy.db import DbUnavailable
from smart_proxy.notifier import AlertThrottle


class _DeadDb:
    """Every write dies the way a lost connection dies."""

    def is_available(self) -> bool:
        return False

    def __getattr__(self, name: str):
        async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise DbUnavailable("PostgreSQL is unavailable (breaker open)")
        return _boom


def _key(key_id: str = "key-1", *, role: str = "primary",
         key_type: str = "oauth") -> _AnthropicKey:
    return _AnthropicKey(
        key_id=key_id, key_type=key_type, status="active", api_key=None,
        access_token="a", refresh_token="r", client_id="c",
        expires_at=9999999999999, scopes="user:inference", role=role,
    )


class BestEffortWriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        notifier = MagicMock()
        self.sent: list[str] = []

        async def notify(text: str) -> bool:
            self.sent.append(text)
            return True

        notifier.notify = notify
        anthropic_proxy._ALERT_FALLBACK.clear()
        anthropic_proxy._ALERT_FALLBACK.update({
            "_notifier": notifier,
            "_alert_throttle": AlertThrottle(window_seconds=1800),
            "_alert_tasks": set(),
        })

    def tearDown(self) -> None:
        anthropic_proxy._ALERT_FALLBACK.clear()

    async def _drain(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        tasks = anthropic_proxy._ALERT_FALLBACK.get("_alert_tasks") or set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_deactivate_bans_in_memory_even_when_the_write_dies(self) -> None:
        key = _key()
        pool = AnthropicKeyPool(_DeadDb())
        pool._keys = [key]

        await pool.deactivate(key, audit_error_type="authentication_error")
        await self._drain()

        self.assertIn(key.key_id, pool._banned)
        self.assertEqual(key.status, "inactive")
        self.assertIsNone(pool.pick())          # really out of rotation
        self.assertTrue(self.sent)

    async def test_mark_low_balance_survives_a_dead_write(self) -> None:
        key = _key()
        pool = AnthropicKeyPool(_DeadDb())
        pool._keys = [key]

        await pool.mark_low_balance(key)
        await self._drain()

        self.assertEqual(key.status, "low_balance")
        self.assertTrue(self.sent)

    async def test_promote_to_primary_survives_a_dead_write(self) -> None:
        key = _key(role="standby")
        pool = AnthropicKeyPool(_DeadDb())
        pool._keys = [key]

        await pool.promote_to_primary(key)
        await self._drain()

        self.assertEqual(key.role, "primary")
        self.assertTrue(self.sent)

    async def test_note_fallback_serve_survives_a_dead_audit_write(self) -> None:
        key = _key(role="fallback", key_type="api_key")
        pool = AnthropicKeyPool(_DeadDb())
        pool._keys = [key]
        pool._notifier = None

        await pool.note_fallback_serve(key, "sp-consumer")
        await self._drain()

        # The throttle slot is taken, so a second serve stays quiet.
        self.assertIn((key.key_id, "sp-consumer"), pool._fallback_alert_at)


class DeadDbEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """The whole point: with the database gone, requests still get served."""

    def setUp(self) -> None:
        notifier = MagicMock()
        self.sent: list[str] = []

        async def notify(text: str) -> bool:
            self.sent.append(text)
            return True

        notifier.notify = notify
        self.notifier = notifier

    def _app(self, client, pool) -> dict:
        return {
            "anthropic_pool": pool,
            "http_client": client,
            "usage_tracker": None,
            "disable_1m_context": False,
            "strip_system_phrase": "",
            "claude_like": False,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "db": _DeadDb(),
            "key_limiter": None,
            "_notifier": self.notifier,
            "_alert_throttle": AlertThrottle(window_seconds=1800),
            "_alert_tasks": set(),
        }

    async def test_an_upstream_429_with_a_dead_db_fails_over_instead_of_500(self) -> None:
        from tests.test_anthropic_proxy_oauth_messages import (
            _FakeDb, _FakeRequest, _FakeStreamResponse,
            _FakeStreamingUpstreamResponse, _FakeUpstreamResponse,
        )
        from tests.test_proxy_upstream_overload import _MultiKeyPool, _SequencedClient
        from unittest.mock import patch

        client = _SequencedClient([
            _FakeUpstreamResponse(
                429, b'{"type":"error","error":{"type":"rate_limit_error"}}',
                headers={"retry-after": "1"},
            ),
            _FakeStreamingUpstreamResponse([
                b'event: message_start\ndata: {"type":"message_start"}\n\n',
                b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            ]),
        ])
        pool = _MultiKeyPool([_key("key-one"), _key("key-two")])
        app = self._app(client, pool)
        request = _FakeRequest(
            app=app,
            headers={"Authorization": "Bearer sp-test-key-0123456789ab",
                     "User-Agent": "anthropic/PHP 0.42.0"},
            body=b'{"model":"claude-opus-5","messages":[]}',
        )

        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
            resp = await anthropic_proxy._proxy_handler(request)
        for _ in range(3):
            await asyncio.sleep(0)

        # Failed over to the second key; the lost rate-limit row cost nothing.
        self.assertEqual(resp.status, 200)
        self.assertEqual(client.sends, 2)



if __name__ == "__main__":
    unittest.main()
