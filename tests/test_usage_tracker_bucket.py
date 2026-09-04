# tests/test_usage_tracker_bucket.py
"""`usage_bucket` writes, and the flush semantics they depend on.

The dashboard reads `usage_bucket` for hour-grain ranges and `usage_daily` for
day-grain ones, so the two must agree. They do because `record()` derives the
date and the hour label from a single `now`, and `flush()` writes both tables
in one transaction. Both halves are pinned here.

The flush rewrite is also covered: before it, all buffers were swapped out
under the lock and then written in sequence, so one failing write discarded
every buffer after it -- the 2026-08-21 incident, where a broken write starved
the spend limiter's re-seed source for hours.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import build_database_from_config
from smart_proxy.usage import UsageFlushError, UsageTracker


class _FrozenDatetime(datetime):
    _now = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._now if tz is None else cls._now.astimezone(tz)


class _FrozenDatetimeNextHour(_FrozenDatetime):
    _now = datetime(2026, 7, 30, 15, 5, tzinfo=timezone.utc)


class _FrozenDatetimeNextDay(_FrozenDatetime):
    _now = datetime(2026, 7, 31, 1, 5, tzinfo=timezone.utc)


class UsageBucketTrackerTests(unittest.IsolatedAsyncioTestCase):
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

    async def _all_buckets(self, db) -> list[dict]:
        cur = await db.db.execute(
            "SELECT * FROM usage_bucket ORDER BY hour_utc, proxy_key, model,"
            " via_openai_compat, request_kind"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def test_record_buffers_the_full_dimension_key(self) -> None:
        tracker = UsageTracker()
        with patch("smart_proxy.usage.datetime", _FrozenDatetime):
            tracker.record(
                "sp-a", "cred-1", "anthropic", "claude-opus-4-8", 100, 20,
                group_name="  acct-a  ", via_openai_compat=True,
                request_kind="subagent",
            )
        self.assertEqual(
            list(tracker._bucket_buf),
            [("2026-07-30T14", "sp-a", "acct-a", "cred-1", "anthropic",
              "claude-opus-4-8", 1, "subagent")],
        )

    async def test_flush_writes_bucket_rows(self) -> None:
        db = await self._db()
        tracker = UsageTracker()
        with patch("smart_proxy.usage.datetime", _FrozenDatetime):
            tracker.record("sp-a", "cred-1", "anthropic", "m", 100, 20,
                           cache_read_tokens=5, group_name="acct-a")
            tracker.record("sp-a", "cred-1", "anthropic", "m", 50, 10,
                           group_name="acct-a")
            tracker.record("sp-b", "cred-1", "anthropic", "m", 7, 3)
        await tracker.flush(db)

        rows = await self._all_buckets(db)
        self.assertEqual(len(rows), 2)
        a = next(r for r in rows if r["proxy_key"] == "sp-a")
        self.assertEqual(a["hour_utc"], "2026-07-30T14")
        self.assertEqual(a["group_name"], "acct-a")
        self.assertEqual(a["request_kind"], "unknown")
        self.assertEqual((a["input_tokens"], a["output_tokens"]), (150, 30))
        self.assertEqual(a["cache_read_tokens"], 5)
        self.assertEqual(a["requests"], 2)

        b = next(r for r in rows if r["proxy_key"] == "sp-b")
        self.assertIsNone(b["group_name"], "empty group_name is stored as NULL")

    async def test_bucket_is_a_projection_of_daily_and_kind(self) -> None:
        """The invariant the whole design rests on."""
        db = await self._db()
        tracker = UsageTracker()

        for clock in (_FrozenDatetime, _FrozenDatetimeNextHour, _FrozenDatetimeNextDay):
            with patch("smart_proxy.usage.datetime", clock):
                tracker.record("sp-a", "cred-1", "anthropic", "claude-opus-4-8",
                               100, 20, cache_read_tokens=7, group_name="acct-a",
                               request_kind="main")
                tracker.record("sp-a", "cred-1", "anthropic", "claude-opus-4-8",
                               5, 1, group_name="acct-a", request_kind="subagent")
                tracker.record("sp-a", "cred-2", "anthropic", "claude-haiku-4-5",
                               3, 2, group_name="acct-b", request_kind="main",
                               via_openai_compat=True)
                tracker.record("sp-b", "cred-1", "anthropic", "claude-opus-4-8",
                               9, 4, group_name="acct-a", request_kind="main")
        await tracker.flush(db)

        counters = (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cache_creation_5m_tokens",
            "cache_creation_1h_tokens", "web_search_requests", "requests",
        )
        sums = ", ".join(f"SUM({c})" for c in counters)

        cur = await db.db.execute(
            f"""SELECT substr(hour_utc, 1, 10) AS date, proxy_key, credential_id,
                       provider, model, via_openai_compat, {sums}
                FROM usage_bucket
                GROUP BY substr(hour_utc, 1, 10), proxy_key, credential_id,
                         provider, model, via_openai_compat
                ORDER BY 1, 2, 3, 4, 5, 6"""
        )
        projected = [tuple(r) for r in await cur.fetchall()]
        cur = await db.db.execute(
            f"""SELECT date, proxy_key, credential_id, provider, model,
                       via_openai_compat, {sums}
                FROM usage_daily
                GROUP BY date, proxy_key, credential_id, provider, model,
                         via_openai_compat
                ORDER BY 1, 2, 3, 4, 5, 6"""
        )
        daily = [tuple(r) for r in await cur.fetchall()]
        self.assertEqual(projected, daily)
        self.assertGreater(len(daily), 1, "the fixture must exercise several groups")

        cur = await db.db.execute(
            f"""SELECT substr(hour_utc, 1, 10) AS date, proxy_key, request_kind,
                       provider, model, {sums}
                FROM usage_bucket
                GROUP BY substr(hour_utc, 1, 10), proxy_key, request_kind, provider, model
                ORDER BY 1, 2, 3, 4, 5"""
        )
        projected_kind = [tuple(r) for r in await cur.fetchall()]
        cur = await db.db.execute(
            f"""SELECT date, proxy_key, request_kind, provider, model, {sums}
                FROM usage_kind_daily
                GROUP BY date, proxy_key, request_kind, provider, model
                ORDER BY 1, 2, 3, 4, 5"""
        )
        self.assertEqual(projected_kind, [tuple(r) for r in await cur.fetchall()])

    async def test_flush_clears_the_bucket_buffer(self) -> None:
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 1, 1)
        await tracker.flush(db)
        await tracker.flush(db)
        rows = await self._all_buckets(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requests"], 1)

    async def test_flush_return_contract_is_unchanged(self) -> None:
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 1, 1)
        rows = await tracker.flush(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 15, "usage_daily upsert arity")

    async def test_a_secondary_failure_does_not_starve_the_others(self) -> None:
        """One broken table must not discard four other buffers."""
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 10, 2, request_kind="main")

        boom = RuntimeError("session table is wedged")
        with patch.object(db, "upsert_usage_session_batch", AsyncMock(side_effect=boom)):
            with self.assertRaises(UsageFlushError) as ctx:
                await tracker.flush(db)

        self.assertEqual(set(ctx.exception.failures), {"usage_session"})
        self.assertEqual(len(ctx.exception.rows), 1, "daily rows landed")
        for table in ("usage_daily", "usage_bucket", "usage_key_hourly", "usage_kind_daily"):
            cur = await db.db.execute(f"SELECT COUNT(*) AS n FROM {table}")
            self.assertEqual(
                (await cur.fetchone())["n"], 1, f"{table} must still be written"
            )

    async def test_pair_failure_reports_no_landed_rows_but_writes_the_rest(self) -> None:
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 10, 2,
                       request_kind="main", session_id="sess-1")

        boom = RuntimeError("pair is wedged")
        with patch.object(
            db, "upsert_usage_daily_and_bucket_batch", AsyncMock(side_effect=boom)
        ):
            with self.assertRaises(UsageFlushError) as ctx:
                await tracker.flush(db)

        self.assertEqual(set(ctx.exception.failures), {"usage_daily+usage_bucket"})
        self.assertEqual(ctx.exception.rows, [], "nothing committed, nothing to attribute")
        for table in ("usage_key_hourly", "usage_kind_daily", "usage_session"):
            cur = await db.db.execute(f"SELECT COUNT(*) AS n FROM {table}")
            self.assertEqual((await cur.fetchone())["n"], 1, f"{table} must still be written")

    async def test_daily_and_bucket_are_atomic_on_sqlite(self) -> None:
        """A failing bucket write must roll usage_daily back, not half-commit."""
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 10, 2)

        with patch(
            "smart_proxy.db.build_usage_bucket_upsert_sql",
            return_value="INSERT INTO no_such_table VALUES (?)",
        ):
            with self.assertRaises(UsageFlushError):
                await tracker.flush(db)

        cur = await db.db.execute("SELECT COUNT(*) AS n FROM usage_daily")
        self.assertEqual(
            (await cur.fetchone())["n"], 0,
            "usage_daily must roll back with its bucket half",
        )

        # The connection is still usable and a later flush commits normally.
        tracker.record("sp-a", "cred-1", "anthropic", "m", 1, 1)
        await tracker.flush(db)
        cur = await db.db.execute("SELECT COUNT(*) AS n FROM usage_daily")
        self.assertEqual((await cur.fetchone())["n"], 1)


if __name__ == "__main__":
    unittest.main()
