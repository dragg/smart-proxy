from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import (
    AnthropicKeyPool,
    _AnthropicKey,
    _build_notifier,
    _format_refresh_alert,
    create_app,
)
from smart_proxy.anthropic_oauth import OAuthRefreshError
from smart_proxy.notifier import TelegramNotifier
from tests.db_test_utils import connect_test_database


def _future_ms(minutes: float) -> int:
    return int(time.time() * 1000) + int(minutes * 60_000)


_BIG_BUFFER = 120 * 60 * 1000   # force a proactive refresh on a still-valid token
_DUMMY_CLIENT = object()        # unused: the /token call is patched out in these tests


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify(self, text: str) -> bool:
        self.messages.append(text)
        return True


class _RaisingNotifier:
    async def notify(self, text: str) -> bool:
        raise RuntimeError("telegram down")


def _invalid_grant() -> OAuthRefreshError:
    return OAuthRefreshError("invalid_grant", status_code=400, error_code="invalid_grant")


def _rate_limited() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.com/token")
    resp = httpx.Response(429, headers={"retry-after": "30"}, request=req)
    return httpx.HTTPStatusError("rate limited", request=req, response=resp)


async def _drain(pool: AnthropicKeyPool) -> None:
    await asyncio.gather(*list(pool._alert_tasks), return_exceptions=True)


async def _pool_with_key(
    db, *, role: str = "standby", expires_minutes: float = 60,
    refresh_token: str = "r1", key_id: str = "k",
) -> AnthropicKeyPool:
    await db.insert_anthropic_key(
        id=key_id, key_type="oauth", access_token="old-access",
        refresh_token=refresh_token, client_id="c",
        expires_at=_future_ms(expires_minutes),
        scopes=json.dumps(["user:inference"]), name="nm", role=role,
    )
    pool = AnthropicKeyPool(db)
    await pool.reload()
    return pool


def _key(role: str = "primary", name: str = "pro-sp-auth") -> _AnthropicKey:
    return _AnthropicKey(
        key_id="a1b2c3d4-ffff-0000-1111-222233334444",
        key_type="oauth", status="active", api_key=None,
        access_token="tok", refresh_token="r", client_id="c",
        expires_at=int(time.time() * 1000) + 47 * 60_000, name=name, role=role,
    )


class FormatRefreshAlertTests(unittest.TestCase):
    def test_brick_while_valid_mentions_code_role_and_latch(self) -> None:
        exc = OAuthRefreshError("invalid_grant", status_code=400, error_code="invalid_grant")
        msg = _format_refresh_alert(
            _key(), category="brick", exc=exc, deactivated=False, valid_ms_left=47 * 60_000
        )
        self.assertIn("🔴", msg)
        self.assertIn("SmartProxy", msg)        # service brand
        self.assertIn("invalid_grant", msg)
        self.assertIn("primary", msg)
        self.assertIn("pro-sp-auth", msg)
        self.assertIn("a1b2c3d4", msg)          # short key id
        self.assertNotIn("a1b2c3d4-ffff", msg)  # never the full id

    def test_brick_deactivated_marks_dead_key(self) -> None:
        exc = OAuthRefreshError("invalid_grant", status_code=400, error_code="invalid_grant")
        msg = _format_refresh_alert(
            _key(role="standby"), category="brick", exc=exc, deactivated=True, valid_ms_left=0
        )
        self.assertIn("🔴", msg)
        self.assertIn("standby", msg)
        self.assertIn("invalid_grant", msg)

    def test_transient_uses_yellow_and_reason(self) -> None:
        msg = _format_refresh_alert(
            _key(), category="transient", exc=RuntimeError("conn reset"), valid_ms_left=5 * 3600_000
        )
        self.assertIn("🟡", msg)
        self.assertIn("primary", msg)

    def test_recovered_uses_check(self) -> None:
        msg = _format_refresh_alert(_key(), category="recovered")
        self.assertIn("✅", msg)
        self.assertIn("SmartProxy", msg)
        self.assertIn("primary", msg)


class RefreshDeadLatchTests(unittest.TestCase):
    def test_invalid_grant_while_valid_latches_and_stops_refresh(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_invalid_grant()) as mock:
                        pool = await _pool_with_key(db, expires_minutes=60)
                        key = pool._keys[0]

                        tok1 = await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        self.assertEqual(tok1, "old-access")             # still serving
                        self.assertIn(key.key_id, pool._refresh_dead)    # latched
                        self.assertEqual(mock.call_count, 1)

                        # Latched: a second cycle must NOT hit /token again.
                        tok2 = await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        self.assertEqual(tok2, "old-access")
                        self.assertEqual(mock.call_count, 1)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_latched_then_expired_returns_none_without_refresh(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_invalid_grant()) as mock:
                        pool = await _pool_with_key(db, expires_minutes=60)
                        key = pool._keys[0]
                        await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        self.assertEqual(mock.call_count, 1)

                        # Token now genuinely expired → give up (None) without another /token.
                        key.expires_at = int(time.time() * 1000) - 1000
                        tok = await pool.ensure_valid_token(key, _DUMMY_CLIENT)
                        self.assertIsNone(tok)
                        self.assertEqual(mock.call_count, 1)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_reload_clears_latch_when_refresh_token_changes(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_invalid_grant()):
                        pool = await _pool_with_key(db, expires_minutes=60, key_id="k")
                        key = pool._keys[0]
                        await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        self.assertIn("k", pool._refresh_dead)

                    # Re-auth: a new refresh token lands in the DB row.
                    await db.update_anthropic_oauth_tokens("k", "fresh-access", _future_ms(480), "r2-new")
                    await pool.reload()
                    self.assertNotIn("k", pool._refresh_dead)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_reread_rescue_adopts_newer_token_and_does_not_latch(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    pool = await _pool_with_key(db, expires_minutes=60, key_id="k", refresh_token="r1")
                    key = pool._keys[0]
                    # Another writer already rotated + persisted a good token to the DB row,
                    # while our in-memory key still holds the stale one.
                    await db.update_anthropic_oauth_tokens("k", "adopted-access", _future_ms(480), "r2")

                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_invalid_grant()):
                        await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)

                    self.assertNotIn("k", pool._refresh_dead)     # rescued, not latched
                    self.assertEqual(key.refresh_token, "r2")     # adopted DB token
                finally:
                    await db.close()

        asyncio.run(run())


class _DummyDB:
    pass


class AlertThrottleUnitTests(unittest.TestCase):
    def test_brick_once_transient_windowed_recovered_always(self) -> None:
        async def run() -> None:
            pool = AnthropicKeyPool(_DummyDB())
            notifier = _FakeNotifier()
            pool._notifier = notifier
            key = _key()

            pool._fire_alert(category="brick", key=key, exc=_invalid_grant(), valid_ms_left=1000)
            pool._fire_alert(category="brick", key=key, exc=_invalid_grant())      # throttled
            pool._fire_alert(category="transient", key=key, exc=_invalid_grant())
            pool._fire_alert(category="transient", key=key, exc=_invalid_grant())  # throttled (30m)
            pool._fire_alert(category="recovered", key=key)
            pool._fire_alert(category="recovered", key=key)                        # always fires
            await _drain(pool)

            emojis = "".join(m[0] for m in notifier.messages)
            self.assertEqual(notifier.messages.__len__(), 4)   # 1 brick + 1 transient + 2 recovered
            self.assertEqual(emojis.count("🔴"), 1)
            self.assertEqual(emojis.count("🟡"), 1)
            self.assertEqual(emojis.count("✅"), 2)

        asyncio.run(run())

    def test_no_notifier_is_noop(self) -> None:
        async def run() -> None:
            pool = AnthropicKeyPool(_DummyDB())   # no notifier assigned
            pool._fire_alert(category="brick", key=_key(), exc=_invalid_grant())
            await _drain(pool)   # must not raise
        asyncio.run(run())


class AlertIntegrationTests(unittest.TestCase):
    def test_invalid_grant_fires_single_brick_alert(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_invalid_grant()):
                        pool = await _pool_with_key(db, expires_minutes=60)
                        pool._notifier = _FakeNotifier()
                        key = pool._keys[0]
                        await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        await _drain(pool)

                    msgs = pool._notifier.messages
                    self.assertEqual(len(msgs), 1)
                    self.assertIn("🔴", msgs[0])
                    self.assertIn("invalid_grant", msgs[0])
                finally:
                    await db.close()

        asyncio.run(run())

    def test_rate_limited_fires_transient_alert(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_rate_limited()):
                        pool = await _pool_with_key(db, expires_minutes=60)
                        pool._notifier = _FakeNotifier()
                        key = pool._keys[0]
                        await pool.ensure_valid_token(key, _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        await _drain(pool)

                    msgs = pool._notifier.messages
                    self.assertEqual(len(msgs), 1)
                    self.assertIn("🟡", msgs[0])
                    self.assertNotIn(key.key_id, pool._refresh_dead)   # transient never latches
                finally:
                    await db.close()

        asyncio.run(run())

    def test_recovery_alert_on_reauth_reload(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_invalid_grant()):
                        pool = await _pool_with_key(db, expires_minutes=60, key_id="k")
                        pool._notifier = _FakeNotifier()
                        await pool.ensure_valid_token(pool._keys[0], _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)

                    await db.update_anthropic_oauth_tokens("k", "fresh", _future_ms(480), "r2-new")
                    await pool.reload()
                    await _drain(pool)

                    self.assertTrue(any("✅" in m for m in pool._notifier.messages))
                finally:
                    await db.close()

        asyncio.run(run())

    def test_notifier_failure_does_not_break_refresh(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    with patch("smart_proxy.anthropic_proxy._refresh_oauth_token",
                               side_effect=_invalid_grant()):
                        pool = await _pool_with_key(db, expires_minutes=60)
                        pool._notifier = _RaisingNotifier()
                        tok = await pool.ensure_valid_token(pool._keys[0], _DUMMY_CLIENT, refresh_buffer_ms=_BIG_BUFFER)
                        await _drain(pool)
                    self.assertEqual(tok, "old-access")   # refresh path unharmed
                finally:
                    await db.close()

        asyncio.run(run())


class NotifierWiringTests(unittest.TestCase):
    def test_build_notifier_requires_token_and_chat(self) -> None:
        base = {"http_client": object()}
        self.assertIsNone(_build_notifier({**base, "telegram_bot_token": "", "telegram_chat_id": ""}))
        self.assertIsNone(_build_notifier({**base, "telegram_bot_token": "t", "telegram_chat_id": ""}))
        self.assertIsNone(_build_notifier({**base, "telegram_bot_token": "", "telegram_chat_id": "c"}))
        n = _build_notifier({**base, "telegram_bot_token": "t", "telegram_chat_id": "c"})
        self.assertIsInstance(n, TelegramNotifier)

    def test_create_app_stores_telegram_config(self) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="It is recommended to use web.AppKey instances for keys.")
            app = create_app("./smart-proxy.db", telegram_bot_token="BT", telegram_chat_id="42")
        self.assertEqual(app["telegram_bot_token"], "BT")
        self.assertEqual(app["telegram_chat_id"], "42")


if __name__ == "__main__":
    unittest.main()
