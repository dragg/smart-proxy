from __future__ import annotations

import asyncio
import json
import random
import tempfile
import unittest
import warnings
from datetime import datetime, time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import (
    AnthropicKeyPool,
    _PARIS_TZ,
    _build_daily_oauth_smoke_schedule,
    _ensure_daily_oauth_smoke_schedule,
    _parse_smoke_window,
    _run_oauth_smoke_pass,
    _start_background_tasks,
    create_app,
)
from tests.db_test_utils import connect_test_database


class _FakeTask:
    """Enough of asyncio.Task for _start_background_tasks to wire a watcher."""

    def __init__(self) -> None:
        self.callbacks: list = []

    def add_done_callback(self, cb) -> None:  # noqa: ANN001
        self.callbacks.append(cb)


def _fake_create_task(coro):  # noqa: ANN001
    coro.close()
    return _FakeTask()


class _FakeSmokeResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": "application/json", "retry-after": "11"}

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        return None


class _FakeSmokeClient:
    def __init__(self, response: _FakeSmokeResponse) -> None:
        self._response = response

    def build_request(self, method: str, url: str, headers: dict, content: bytes | None):
        return type("Req", (), {"method": method, "url": url, "headers": headers, "content": content})()

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        return self._response


class AnthropicProxyScheduleTests(unittest.TestCase):
    def test_parse_smoke_window_reads_start_and_end_times(self) -> None:
        window_start, window_end = _parse_smoke_window("08:00-09:00")

        self.assertEqual(window_start, time(8, 0))
        self.assertEqual(window_end, time(9, 0))

    def test_parse_smoke_window_rejects_invalid_ranges(self) -> None:
        with self.assertRaises(ValueError):
            _parse_smoke_window("09:00-08:00")

    def test_pick_skips_low_balance_keys(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-low-balance",
                        key_type="oauth",
                        access_token="low-balance-token",
                        refresh_token="low-balance-refresh",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="low-balance-key",
                    )
                    await db.insert_anthropic_key(
                        id="key-active",
                        key_type="oauth",
                        access_token="active-token",
                        refresh_token="active-refresh",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="active-key",
                    )
                    await db.set_anthropic_key_status("key-low-balance", "low_balance")
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    key = pool.pick()
                    self.assertIsNotNone(key)
                    assert key is not None
                    self.assertEqual(key.key_id, "key-active")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_cooldown_skips_current_key_until_another_is_available(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-a",
                        key_type="oauth",
                        access_token="token-a",
                        refresh_token="refresh-a",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="key-a",
                    )
                    await db.insert_anthropic_key(
                        id="key-b",
                        key_type="oauth",
                        access_token="token-b",
                        refresh_token="refresh-b",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="key-b",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    first = pool.pick()
                    self.assertIsNotNone(first)
                    assert first is not None
                    pool.cooldown(first, 99)

                    second = pool.pick()
                    self.assertIsNotNone(second)
                    assert second is not None
                    self.assertEqual(second.key_id, "key-b")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_pick_prefers_available_oauth_over_sticky_api_key(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-api",
                        key_type="api_key",
                        api_key="sk-ant-api-key",
                        name="key-api",
                    )
                    await db.insert_anthropic_key(
                        id="key-oauth",
                        key_type="oauth",
                        access_token="token-oauth",
                        refresh_token="refresh-oauth",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="key-oauth",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    first = pool.pick()
                    self.assertIsNotNone(first)
                    assert first is not None
                    self.assertEqual(first.key_id, "key-oauth")

                    second = pool.pick()
                    self.assertIsNotNone(second)
                    assert second is not None
                    self.assertEqual(second.key_id, "key-oauth")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_pick_falls_back_to_api_key_when_preferred_oauth_is_in_cooldown(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-api",
                        key_type="api_key",
                        api_key="sk-ant-api-key",
                        name="key-api",
                    )
                    await db.insert_anthropic_key(
                        id="key-oauth",
                        key_type="oauth",
                        access_token="token-oauth",
                        refresh_token="refresh-oauth",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="key-oauth",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    oauth_key = next(k for k in pool._keys if k.key_id == "key-oauth")
                    pool.cooldown(oauth_key, 99)

                    picked = pool.pick()
                    self.assertIsNotNone(picked)
                    assert picked is not None
                    self.assertEqual(picked.key_id, "key-api")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_pick_switches_back_to_oauth_once_cooldown_expires(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-api",
                        key_type="api_key",
                        api_key="sk-ant-api-key",
                        name="key-api",
                    )
                    await db.insert_anthropic_key(
                        id="key-oauth",
                        key_type="oauth",
                        access_token="token-oauth",
                        refresh_token="refresh-oauth",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="key-oauth",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    oauth_key = next(k for k in pool._keys if k.key_id == "key-oauth")
                    pool.cooldown(oauth_key, 99)

                    first = pool.pick()
                    self.assertIsNotNone(first)
                    assert first is not None
                    self.assertEqual(first.key_id, "key-api")

                    pool._cooldowns.pop("key-oauth", None)

                    second = pool.pick()
                    self.assertIsNotNone(second)
                    assert second is not None
                    self.assertEqual(second.key_id, "key-oauth")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_build_daily_schedule_creates_independent_slots_for_each_window(self) -> None:
        rng = random.Random(123)

        schedule = _build_daily_oauth_smoke_schedule(
            now=datetime(2026, 4, 4, 6, 0, tzinfo=_PARIS_TZ),
            rng=rng,
            morning_window=_parse_smoke_window("08:00-09:00"),
            midday_window=_parse_smoke_window("14:00-15:00"),
        )

        self.assertEqual(str(schedule.current_day), "2026-04-04")
        self.assertGreaterEqual(schedule.morning_slot.time(), time(8, 0))
        self.assertLess(schedule.morning_slot.time(), time(9, 0))
        self.assertGreaterEqual(schedule.midday_slot.time(), time(14, 0))
        self.assertLess(schedule.midday_slot.time(), time(15, 0))
        self.assertNotEqual(schedule.morning_slot, schedule.midday_slot)
        self.assertFalse(schedule.morning_done)
        self.assertFalse(schedule.midday_done)

    def test_new_paris_day_redraws_both_slots(self) -> None:
        rng = random.Random(123)
        first = _build_daily_oauth_smoke_schedule(
            now=datetime(2026, 4, 4, 6, 0, tzinfo=_PARIS_TZ),
            rng=rng,
            morning_window=_parse_smoke_window("08:00-09:00"),
            midday_window=_parse_smoke_window("14:00-15:00"),
        )

        second = _ensure_daily_oauth_smoke_schedule(
            first,
            now=datetime(2026, 4, 5, 6, 0, tzinfo=_PARIS_TZ),
            rng=rng,
            morning_window=_parse_smoke_window("08:00-09:00"),
            midday_window=_parse_smoke_window("14:00-15:00"),
        )

        self.assertNotEqual(first.current_day, second.current_day)
        self.assertNotEqual(first.morning_slot, second.morning_slot)
        self.assertNotEqual(first.midday_slot, second.midday_slot)

    def test_start_background_tasks_skips_smoke_task_when_disabled(self) -> None:
        app = {"oauth_smoke_enabled": False, "standby_keepwarm_enabled": False}

        with patch("smart_proxy.anthropic_proxy.asyncio.create_task",
                   side_effect=_fake_create_task) as create_task:
            _start_background_tasks(app)

        self.assertEqual(create_task.call_count, 2)
        self.assertIn("_flush_task", app)
        self.assertIn("_recheck_task", app)
        self.assertNotIn("_oauth_smoke_task", app)
        self.assertNotIn("_standby_keepwarm_task", app)
        # Every started loop must be watched: a task that dies can no longer
        # report anything from inside its own except blocks.
        self.assertTrue(app["_flush_task"].callbacks)
        self.assertTrue(app["_recheck_task"].callbacks)

    def test_start_background_tasks_creates_keepwarm_task_when_enabled(self) -> None:
        app = {"oauth_smoke_enabled": False, "standby_keepwarm_enabled": True}

        with patch("smart_proxy.anthropic_proxy.asyncio.create_task",
                   side_effect=_fake_create_task) as create_task:
            _start_background_tasks(app)

        self.assertEqual(create_task.call_count, 3)
        self.assertIn("_standby_keepwarm_task", app)
        self.assertTrue(app["_standby_keepwarm_task"].callbacks)

    def test_create_app_rejects_invalid_window_when_smoke_enabled(self) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="It is recommended to use web.AppKey instances for keys.",
            )
            with self.assertRaises(ValueError):
                create_app(
                    "./smart-proxy.db",
                    oauth_smoke_enabled=True,
                    oauth_smoke_morning_window="09:00-08:00",
                )

    def test_create_app_allows_invalid_windows_when_smoke_disabled(self) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="It is recommended to use web.AppKey instances for keys.",
            )
            app = create_app(
                "./smart-proxy.db",
                oauth_smoke_enabled=False,
                oauth_smoke_morning_window="09:00-08:00",
                oauth_smoke_midday_window="15:00-14:00",
            )

        self.assertFalse(app["oauth_smoke_enabled"])
        self.assertNotIn("oauth_smoke_morning_window", app)
        self.assertNotIn("oauth_smoke_midday_window", app)

    def test_create_app_wires_standby_keepwarm_config(self) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="It is recommended to use web.AppKey instances for keys.",
            )
            app = create_app("./smart-proxy.db", standby_keepwarm_buffer_minutes=90)

        self.assertTrue(app["standby_keepwarm_enabled"])          # default on
        self.assertEqual(app["standby_keepwarm_buffer_ms"], 90 * 60 * 1000)

    def test_create_app_defaults_standby_keepwarm_buffer(self) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="It is recommended to use web.AppKey instances for keys.",
            )
            app = create_app("./smart-proxy.db")

        self.assertEqual(app["standby_keepwarm_buffer_ms"], 120 * 60 * 1000)

    def test_smoke_pass_does_not_clear_existing_cooldowns(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-cooldown",
                        key_type="oauth",
                        access_token="access-token",
                        refresh_token="refresh-token",
                        client_id="client-id",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="cooldown-key",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()
                    key = pool.pick()
                    assert key is not None
                    pool.cooldown(key, 99)

                    app = {
                        "db": db,
                        "anthropic_pool": pool,
                        "http_client": _FakeSmokeClient(
                            _FakeSmokeResponse(
                                429,
                                b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}',
                            )
                        ),
                        "disable_1m_context": False,
                    }

                    await _run_oauth_smoke_pass(app, "morning")

                    self.assertIn("key-cooldown", pool._cooldowns)
                finally:
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
