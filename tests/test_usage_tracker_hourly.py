# tests/test_usage_tracker_hourly.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from datetime import datetime, timezone

from smart_proxy.db import build_database_from_config
from smart_proxy.usage import UsageTracker


class _FrozenDatetime(datetime):
    _now = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._now if tz is None else cls._now.astimezone(tz)


class HourlyBufferTests(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        return db

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_record_buckets_by_utc_hour_and_flush_writes_them(self):
        db = await self._db()
        tracker = UsageTracker()
        with patch("smart_proxy.usage.datetime", _FrozenDatetime):
            tracker.record("sp-a", "cred-1", "anthropic", "claude-opus-4-5", 100, 20,
                           cache_read_tokens=5, web_search_requests=1)
            tracker.record("sp-a", "cred-1", "anthropic", "claude-opus-4-5", 50, 10)
            tracker.record("sp-b", "cred-1", "anthropic", "claude-sonnet-5", 7, 3)
        await tracker.flush(db)

        rows = await db.query_usage_key_hourly("2026-07-30T14", "2026-07-30T15")
        got = {(r["proxy_key"], r["model"]): (int(r["input_tokens"]),
                                              int(r["output_tokens"]),
                                              int(r["requests"])) for r in rows}
        self.assertEqual(got, {
            ("sp-a", "claude-opus-4-5"): (150, 30, 2),
            ("sp-b", "claude-sonnet-5"): (7, 3, 1),
        })

    async def test_flush_still_returns_usage_daily_rows_only(self):
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 1, 1)
        rows = await tracker.flush(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 15)   # usage_daily upsert arity

    async def test_flush_clears_the_hour_buffer(self):
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 1, 1)
        await tracker.flush(db)
        await tracker.flush(db)
        rows = await db.query_usage_key_hourly("2000-01-01T00", "2100-01-01T00")
        self.assertEqual(int(rows[0]["requests"]), 1)


if __name__ == "__main__":
    unittest.main()
