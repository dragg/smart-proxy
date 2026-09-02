from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database, WINDOW_USAGE_COUNTERS
from smart_proxy.anthropic_proxy import _flush_usage, _window_usage_deltas
from smart_proxy.usage import UsageTracker


KEY_ID = "oauth-key-1"


def _minute_iso(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).isoformat()


def _delta(key_id: str = KEY_ID, model: str = "claude-sonnet-5", **over: int) -> dict:
    base = {
        "key_id": key_id, "model": model,
        "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 10,
        "cache_creation_tokens": 5, "cache_creation_5m_tokens": 5,
        "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 2,
    }
    base.update(over)
    return base


async def _insert_key(db: Database, key_id: str = KEY_ID) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.db.execute(
        """INSERT INTO anthropic_keys
           (id, key_type, status, access_token, refresh_token,
            expires_at, name, created_at, updated_at)
           VALUES (?, 'oauth', 'active', 'tok', 'ref',
                   9999999999999, 't', ?, ?)""",
        (key_id, now, now),
    )
    await db.db.commit()


async def _observe(
    db: Database, key_id: str, kind: str, resets_at_dt: datetime,
    utilization: float = 50.0,
) -> str:
    resets_at = _minute_iso(resets_at_dt)
    await db.record_oauth_window_observations(key_id, [{
        "window_kind": kind,
        "resets_at": resets_at,
        "resets_at_raw": resets_at_dt.isoformat(),
        "utilization": utilization,
    }])
    return resets_at


class WindowUsageSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_tables_exist_with_expected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                expected = {
                    "oauth_window_usage":
                        {"window_id", "model", *WINDOW_USAGE_COUNTERS},
                    "oauth_window_usage_pending":
                        {"key_id", "window_kind", "model", "updated_at",
                         *WINDOW_USAGE_COUNTERS},
                }
                for table, columns in expected.items():
                    cur = await db.db.execute(f"PRAGMA table_info({table})")
                    names = {row["name"] for row in await cur.fetchall()}
                    self.assertEqual(names, columns, table)
            finally:
                await db.close()


class AttributeWindowUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._td.name) / "t.db"))
        await self.db.connect()
        await _insert_key(self.db)
        self.now = datetime.now(timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._td.cleanup()

    async def test_attributes_to_live_window_and_accumulates(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        await self.db.attribute_oauth_window_usage([_delta()])
        await self.db.attribute_oauth_window_usage([_delta()])
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-sonnet-5")
        self.assertEqual(rows[0]["input_tokens"], 200)
        self.assertEqual(rows[0]["requests"], 4)
        self.assertEqual(
            await self.db.list_oauth_window_usage_pending(KEY_ID), [])

    async def test_expired_window_goes_to_pending(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        late = (self.now + timedelta(hours=2)).isoformat()
        await self.db.attribute_oauth_window_usage([_delta()], now=late)
        self.assertEqual(await self.db.list_oauth_window_usage(KEY_ID), [])
        pending = await self.db.list_oauth_window_usage_pending(KEY_ID)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["window_kind"], "seven_day")
        self.assertEqual(pending[0]["input_tokens"], 100)
        self.assertEqual(pending[0]["updated_at"], late)

    async def test_unobserved_kind_is_skipped(self) -> None:
        await self.db.attribute_oauth_window_usage([_delta()])
        self.assertEqual(await self.db.list_oauth_window_usage(KEY_ID), [])
        self.assertEqual(
            await self.db.list_oauth_window_usage_pending(KEY_ID), [])

    async def test_all_kinds_receive_same_deltas(self) -> None:
        await _observe(self.db, KEY_ID, "five_hour",
                       self.now + timedelta(hours=1))
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(days=3))
        await self.db.attribute_oauth_window_usage([_delta()])
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["input_tokens"] for r in rows}, {100})

    async def test_all_zero_delta_is_ignored(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        zero = _delta(**{c: 0 for c in WINDOW_USAGE_COUNTERS})
        await self.db.attribute_oauth_window_usage([zero])
        self.assertEqual(await self.db.list_oauth_window_usage(KEY_ID), [])

    async def test_limit_kinds_are_not_tracked(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(days=3))
        await _observe(self.db, KEY_ID, "limit:weekly_scoped:Fable",
                       self.now + timedelta(days=3))
        await self.db.attribute_oauth_window_usage([_delta()])
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 1)  # seven_day only
        # expired limit:* windows must not produce pending either
        late = (self.now + timedelta(days=4)).isoformat()
        await self.db.attribute_oauth_window_usage([_delta()], now=late)
        pending = await self.db.list_oauth_window_usage_pending(KEY_ID)
        self.assertEqual({p["window_kind"] for p in pending}, {"seven_day"})


class PendingDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._td.name) / "t.db"))
        await self.db.connect()
        await _insert_key(self.db)
        self.now = datetime.now(timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._td.cleanup()

    async def test_pending_drains_into_new_window(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        late = (self.now + timedelta(hours=2)).isoformat()
        await self.db.attribute_oauth_window_usage([_delta()], now=late)
        self.assertEqual(
            len(await self.db.list_oauth_window_usage_pending(KEY_ID)), 1)

        new_resets = await _observe(self.db, KEY_ID, "seven_day",
                                    self.now + timedelta(days=7))
        self.assertEqual(
            await self.db.list_oauth_window_usage_pending(KEY_ID), [])
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 100)
        logs = await self.db.list_oauth_window_log(KEY_ID)
        new_row = next(r for r in logs if r["resets_at"] == new_resets)
        self.assertEqual(rows[0]["window_id"], new_row["id"])

    async def test_drain_only_moves_matching_kind(self) -> None:
        await _observe(self.db, KEY_ID, "five_hour",
                       self.now + timedelta(hours=1))
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        late = (self.now + timedelta(hours=2)).isoformat()
        await self.db.attribute_oauth_window_usage([_delta()], now=late)
        self.assertEqual(
            len(await self.db.list_oauth_window_usage_pending(KEY_ID)), 2)

        await _observe(self.db, KEY_ID, "five_hour",
                       self.now + timedelta(hours=7))
        pending = await self.db.list_oauth_window_usage_pending(KEY_ID)
        self.assertEqual([p["window_kind"] for p in pending], ["seven_day"])

    async def test_reobservation_of_same_window_does_not_duplicate(self) -> None:
        resets_dt = self.now + timedelta(hours=1)
        await _observe(self.db, KEY_ID, "seven_day", resets_dt)
        await self.db.attribute_oauth_window_usage([_delta()])
        await _observe(self.db, KEY_ID, "seven_day", resets_dt,
                       utilization=60.0)  # jittered duplicate, merged
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 100)


class FlushAttributionTests(unittest.IsolatedAsyncioTestCase):
    def test_window_usage_deltas_aggregates_anthropic_rows(self) -> None:
        rows = [
            ("2026-07-12", "sp-a", None, "k1", "anthropic", "claude-sonnet-5",
             0, 10, 5, 0, 0, 0, 0, 0, 1),
            ("2026-07-12", "sp-b", None, "k1", "anthropic", "claude-sonnet-5",
             1, 30, 15, 2, 0, 0, 0, 0, 2),
            ("2026-07-12", "sp-a", None, "k1", "openai", "gpt-x",
             0, 99, 99, 0, 0, 0, 0, 0, 9),
        ]
        deltas = _window_usage_deltas(rows)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["key_id"], "k1")
        self.assertEqual(deltas[0]["model"], "claude-sonnet-5")
        self.assertEqual(deltas[0]["input_tokens"], 40)
        self.assertEqual(deltas[0]["output_tokens"], 20)
        self.assertEqual(deltas[0]["cache_read_tokens"], 2)
        self.assertEqual(deltas[0]["requests"], 3)

    async def test_flush_usage_lands_on_live_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                await _insert_key(db)
                await _observe(db, KEY_ID, "seven_day",
                               datetime.now(timezone.utc) + timedelta(days=3))
                tracker = UsageTracker()
                tracker.record("sp-a", KEY_ID, "anthropic",
                               "claude-sonnet-5", 100, 50)
                n = await _flush_usage(tracker, db)
                self.assertEqual(n, 1)
                rows = await db.list_oauth_window_usage(KEY_ID)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["input_tokens"], 100)
                self.assertEqual(rows[0]["requests"], 1)
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
