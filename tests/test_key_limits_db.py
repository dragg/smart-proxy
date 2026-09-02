# tests/test_key_limits_db.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import Database, build_database_from_config


class HourlyBucketTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_hourly_upsert_accumulates_per_hour_key_model(self):
        db = await self._db()
        row = ("2026-07-30T14", "sp-a", "claude-opus-4-5", 10, 5, 0, 0, 0, 0, 0, 1)
        await db.upsert_usage_hourly_batch([row])
        await db.upsert_usage_hourly_batch([row])
        rows = await db.query_usage_key_hourly("2026-07-30T00", "2026-07-31T00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["input_tokens"]), 20)
        self.assertEqual(int(rows[0]["output_tokens"]), 10)
        self.assertEqual(int(rows[0]["requests"]), 2)
        self.assertEqual(rows[0]["proxy_key"], "sp-a")
        self.assertEqual(rows[0]["model"], "claude-opus-4-5")

    async def test_query_range_is_start_inclusive_end_exclusive(self):
        db = await self._db()
        await db.upsert_usage_hourly_batch([
            ("2026-07-29T23", "sp-a", "m", 1, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T00", "sp-a", "m", 2, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T23", "sp-a", "m", 4, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-31T00", "sp-a", "m", 8, 0, 0, 0, 0, 0, 0, 1),
        ])
        rows = await db.query_usage_key_hourly("2026-07-30T00", "2026-07-31T00")
        self.assertEqual(int(rows[0]["input_tokens"]), 6)   # 2 + 4 only

    async def test_query_groups_by_key_and_model(self):
        db = await self._db()
        await db.upsert_usage_hourly_batch([
            ("2026-07-30T01", "sp-a", "m1", 1, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T02", "sp-a", "m1", 2, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T02", "sp-a", "m2", 4, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T02", "sp-b", "m1", 8, 0, 0, 0, 0, 0, 0, 1),
        ])
        rows = await db.query_usage_key_hourly("2026-07-30T00", "2026-07-31T00")
        got = {(r["proxy_key"], r["model"]): int(r["input_tokens"]) for r in rows}
        self.assertEqual(got, {("sp-a", "m1"): 3, ("sp-a", "m2"): 4, ("sp-b", "m1"): 8})

    async def test_prune_deletes_only_older_hours(self):
        db = await self._db()
        await db.upsert_usage_hourly_batch([
            ("2026-06-01T00", "sp-a", "m", 1, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T00", "sp-a", "m", 2, 0, 0, 0, 0, 0, 0, 1),
        ])
        await db.prune_usage_key_hourly("2026-07-01T00")
        rows = await db.query_usage_key_hourly("2026-01-01T00", "2027-01-01T00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["input_tokens"]), 2)


class ProxyKeyLimitTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_set_is_upsert_and_list_returns_rows(self):
        db = await self._db()
        await db.set_proxy_key_limit("sp-a", "daily_usd", 25.0)
        await db.set_proxy_key_limit("sp-a", "daily_usd", 40.0)
        rows = await db.list_proxy_key_limits()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proxy_key"], "sp-a")
        self.assertEqual(rows[0]["kind"], "daily_usd")
        self.assertAlmostEqual(float(rows[0]["amount"]), 40.0)

    async def test_delete_removes_the_row(self):
        db = await self._db()
        await db.set_proxy_key_limit("sp-a", "daily_usd", 25.0)
        await db.delete_proxy_key_limit("sp-a", "daily_usd")
        self.assertEqual(await db.list_proxy_key_limits(), [])

    async def test_delete_is_idempotent(self):
        db = await self._db()
        await db.delete_proxy_key_limit("sp-missing", "daily_usd")
        self.assertEqual(await db.list_proxy_key_limits(), [])

    async def test_get_proxy_key_by_created_at(self):
        db = await self._db()
        await db.add_proxy_key("sp-full-key-value", "laptop")
        rows = await db.list_proxy_keys()
        created_at = rows[0]["created_at"]
        self.assertEqual(await db.get_proxy_key_by_created_at(created_at), "sp-full-key-value")
        self.assertIsNone(await db.get_proxy_key_by_created_at("2000-01-01T00:00:00+00:00"))


class SnapshotRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """`tests/test_db_snapshot_roundtrip.py` seeds specific tables by hand, so a
    new SNAPSHOT_TABLE_SPECS entry with rows in it is only covered here."""

    async def test_new_tables_round_trip_through_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            source = Database(str(Path(td) / "source.db"))
            target = Database(str(Path(td) / "target.db"))
            await source.connect()
            await target.connect()
            try:
                await source.set_proxy_key_limit("sp-a", "daily_usd", 25.0)
                await source.upsert_usage_hourly_batch([
                    ("2026-07-30T14", "sp-a", "claude-opus-4-5", 10, 5, 0, 0, 0, 0, 0, 1),
                ])
                snapshot = await source.export_snapshot()
                self.assertEqual(len(snapshot["proxy_key_limits"]), 1)
                self.assertEqual(len(snapshot["usage_key_hourly"]), 1)
                await target.replace_snapshot(snapshot)
                self.assertEqual(await target.export_snapshot(), snapshot)
            finally:
                await source.close()
                await target.close()

    async def test_replace_snapshot_deletes_stale_rows_in_new_tables(self):
        """Regression test: replace_snapshot must DELETE existing rows before INSERT.
        Without SNAPSHOT_DELETE_ORDER entries, stale rows persist and cause PK conflicts."""
        with tempfile.TemporaryDirectory() as td:
            source = Database(str(Path(td) / "source.db"))
            target = Database(str(Path(td) / "target.db"))
            await source.connect()
            await target.connect()
            try:
                # Seed target with stale rows that won't be in the snapshot
                await target.set_proxy_key_limit("sp-stale", "daily_usd", 99.0)
                await target.upsert_usage_hourly_batch([
                    ("2026-07-30T00", "sp-stale", "old-model", 999, 0, 0, 0, 0, 0, 0, 1),
                ])

                # Create snapshot with different data
                await source.set_proxy_key_limit("sp-new", "daily_usd", 50.0)
                await source.upsert_usage_hourly_batch([
                    ("2026-07-30T14", "sp-new", "claude-opus-4-5", 10, 5, 0, 0, 0, 0, 0, 1),
                ])

                # Replace target snapshot; if DELETE isn't working, stale rows persist
                snapshot = await source.export_snapshot()
                await target.replace_snapshot(snapshot)
                target_snapshot = await target.export_snapshot()

                # Verify target has only the new data, no stale rows
                self.assertEqual(len(target_snapshot["proxy_key_limits"]), 1)
                self.assertEqual(target_snapshot["proxy_key_limits"][0]["proxy_key"], "sp-new")
                self.assertNotIn("sp-stale", [r["proxy_key"] for r in target_snapshot["proxy_key_limits"]])

                self.assertEqual(len(target_snapshot["usage_key_hourly"]), 1)
                self.assertEqual(target_snapshot["usage_key_hourly"][0]["proxy_key"], "sp-new")
                self.assertNotIn("sp-stale", [r["proxy_key"] for r in target_snapshot["usage_key_hourly"]])

                # Verify target matches source exactly
                self.assertEqual(target_snapshot, snapshot)
            finally:
                await source.close()
                await target.close()


if __name__ == "__main__":
    unittest.main()
