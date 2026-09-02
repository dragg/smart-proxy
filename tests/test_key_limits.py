# tests/test_key_limits.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
from datetime import datetime, timezone
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import build_database_from_config
from smart_proxy.key_limits import LIMIT_KINDS, KeyLimiter


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class WindowMathTests(unittest.IsolatedAsyncioTestCase):
    async def _limiter(self, tz="Europe/Paris"):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        self._db = db
        return KeyLimiter(db, tz=tz)

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_paris_summer_window_starts_at_22utc(self):
        lim = await self._limiter()
        # 2026-07-30 12:00 UTC is 14:00 Paris (UTC+2, CEST).
        now = _utc(2026, 7, 30, 12)
        self.assertEqual(lim.window_start(now), _utc(2026, 7, 29, 22))
        self.assertEqual(lim.window_end(now), _utc(2026, 7, 30, 22))

    async def test_paris_winter_window_starts_at_23utc(self):
        lim = await self._limiter()
        # 2026-01-15 12:00 UTC is 13:00 Paris (UTC+1, CET).
        now = _utc(2026, 1, 15, 12)
        self.assertEqual(lim.window_start(now), _utc(2026, 1, 14, 23))
        self.assertEqual(lim.window_end(now), _utc(2026, 1, 15, 23))

    async def test_utc_tz_setting_gives_utc_midnight(self):
        lim = await self._limiter(tz="UTC")
        now = _utc(2026, 7, 30, 12)
        self.assertEqual(lim.window_start(now), _utc(2026, 7, 30, 0))
        self.assertEqual(lim.window_end(now), _utc(2026, 7, 31, 0))

    async def test_retry_after_is_seconds_until_window_end(self):
        lim = await self._limiter(tz="UTC")
        self.assertEqual(lim.retry_after(_utc(2026, 7, 30, 23, 0)), 3600)
        self.assertGreaterEqual(lim.retry_after(_utc(2026, 7, 30, 23, 59)), 1)


class LimitEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def _limiter(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        self._db = db
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        return lim

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_key_without_a_limit_is_never_blocked(self):
        lim = await self._limiter()
        lim.add("sp-a", "claude-opus-4-5", (10_000_000, 10_000_000, 0, 0, 0, 0, 0))
        self.assertIsNone(lim.check("sp-a"))

    async def test_block_once_spend_reaches_the_limit(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        self.assertIsNone(lim.check("sp-a"))
        # 200k input tokens of claude-opus-4-5 at $5/Mtok = $1.00
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))
        block = lim.check("sp-a")
        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "daily_usd")
        self.assertEqual(block.label, "24h")
        self.assertGreater(block.retry_after, 0)

    async def test_raising_the_limit_unblocks_immediately(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))
        self.assertIsNotNone(lim.check("sp-a"))
        await lim.set_limit("sp-a", "daily_usd", 5.0)
        self.assertIsNone(lim.check("sp-a"))

    async def test_clearing_the_limit_makes_the_key_unlimited(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))
        self.assertIsNotNone(lim.check("sp-a"))
        await lim.set_limit("sp-a", "daily_usd", None)
        self.assertIsNone(lim.check("sp-a"))
        self.assertEqual(await self._db.list_proxy_key_limits(), [])

    async def test_unknown_model_costs_nothing_and_never_blocks(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 0.01)
        lim.add("sp-a", "totally-unknown-model", (1_000_000, 1_000_000, 0, 0, 0, 0, 0))
        self.assertIsNone(lim.check("sp-a"))

    async def test_window_rollover_clears_spend(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        day1 = _utc(2026, 7, 30, 12)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0), now=day1)
        self.assertIsNotNone(lim.check("sp-a", now=day1))
        day2 = _utc(2026, 7, 31, 12)
        self.assertIsNone(lim.check("sp-a", now=day2))

    async def test_set_limit_rejects_unknown_kind(self):
        lim = await self._limiter()
        with self.assertRaises(ValueError):
            await lim.set_limit("sp-a", "hourly_bananas", 1.0)


class SeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_seed_reads_spend_from_hourly_buckets_in_window(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        await db.upsert_usage_hourly_batch([
            # in window (UTC day 2026-07-30): 200k input opus = $1.00
            ("2026-07-30T09", "sp-a", "claude-opus-4-5", 200_000, 0, 0, 0, 0, 0, 0, 1),
            # out of window — previous day
            ("2026-07-29T09", "sp-a", "claude-opus-4-5", 200_000, 0, 0, 0, 0, 0, 0, 1),
        ])
        await db.set_proxy_key_limit("sp-a", "daily_usd", 1.0)
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        await lim.seed(now=_utc(2026, 7, 30, 12))
        block = lim.check("sp-a", now=_utc(2026, 7, 30, 12))
        self.assertIsNotNone(block)
        self.assertAlmostEqual(block.spent_usd, 1.0, places=6)


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_snapshot_shape_for_limited_and_unlimited_keys(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        await lim.set_limit("sp-a", "daily_usd", 4.0)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))

        limited = lim.snapshot("sp-a")["daily_usd"]
        self.assertAlmostEqual(limited["limit_usd"], 4.0)
        self.assertAlmostEqual(limited["spent_usd"], 1.0, places=6)
        self.assertAlmostEqual(limited["remaining_usd"], 3.0, places=6)
        self.assertAlmostEqual(limited["percent"], 25.0, places=1)
        self.assertFalse(limited["exceeded"])
        self.assertTrue(limited["resets_at"].startswith("20"))

        unlimited = lim.snapshot("sp-b")["daily_usd"]
        self.assertIsNone(unlimited["limit_usd"])
        self.assertIsNone(unlimited["remaining_usd"])
        self.assertIsNone(unlimited["percent"])
        self.assertEqual(unlimited["spent_usd"], 0.0)
        self.assertFalse(unlimited["exceeded"])

    async def test_limits_for_returns_configured_amounts(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        self.assertEqual(lim.limits_for("sp-a"), {})
        await lim.set_limit("sp-a", "daily_usd", 12.5)
        self.assertEqual(lim.limits_for("sp-a"), {"daily_usd": 12.5})


class KindRegistryTests(unittest.TestCase):
    def test_daily_usd_is_registered_with_a_24h_label(self):
        self.assertIn("daily_usd", LIMIT_KINDS)
        self.assertEqual(LIMIT_KINDS["daily_usd"].label, "24h")
        self.assertEqual(LIMIT_KINDS["daily_usd"].window_hours, 24)


if __name__ == "__main__":
    unittest.main()
