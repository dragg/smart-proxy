from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import AnthropicKeyPool, _record_window_observations
from smart_proxy.db import Database

WEEK_RAW = "2026-09-03T11:00:00.252962+00:00"
FIVE_RAW = "2026-09-01T20:40:00.252940+00:00"
FIVE_NEXT_RAW = "2026-09-01T23:00:00.252940+00:00"


def usage(seven_day: float, five_hour_resets: str, five_hour: float = 6.0) -> dict:
    return {
        "five_hour": {"utilization": five_hour, "resets_at": five_hour_resets},
        "seven_day": {"utilization": seven_day, "resets_at": WEEK_RAW},
        "limits": [
            {"kind": "weekly_all", "percent": seven_day,
             "resets_at": WEEK_RAW, "scope": None},
        ],
    }


class LimitWipeRecordingTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _pool(self):
        pool = AnthropicKeyPool(MagicMock())
        pool._notifier = None
        return pool

    async def _record(self, db, pool, payload, seen_at):
        await _record_window_observations(
            db, "key-1", payload,
            pool=pool,
            raw_body=json.dumps(payload),
            headers={"request-id": "req_1", "anthropic-organization-id": "org-1",
                     "x-ignored": "no"},
            seen_at=seen_at,
        )

    def test_wipe_is_detected_stored_and_linked_to_snapshots(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    pool = self._pool()
                    await self._record(db, pool, usage(74.0, FIVE_RAW),
                                       "2026-09-01T17:58:40+00:00")
                    await self._record(db, pool, usage(0.0, FIVE_NEXT_RAW, 0.0),
                                       "2026-09-01T18:01:00+00:00")

                    wipes = await db.list_oauth_limit_wipes("key-1")
                    self.assertEqual(len(wipes), 1, wipes)
                    w = wipes[0]
                    self.assertEqual(w["window_kind"], "seven_day")
                    self.assertEqual(w["from_utilization"], 74.0)
                    self.assertEqual(w["source"], "poll")
                    self.assertEqual(w["five_hour_rolled"], 1)
                    self.assertAlmostEqual(w["five_hour_early_minutes"], 159.0, places=0)
                    # both snapshots exist and the wipe points at each side
                    self.assertIsNotNone(w["snapshot_id"])
                    self.assertIsNotNone(w["prev_snapshot_id"])
                    self.assertNotEqual(w["snapshot_id"], w["prev_snapshot_id"])
                finally:
                    await db.close()

        self._run(scenario())

    def test_identical_payloads_dedup_into_one_snapshot_row(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    pool = self._pool()
                    payload = usage(74.0, FIVE_RAW)
                    for minute in range(3):
                        await self._record(db, pool, payload,
                                           f"2026-09-01T17:5{minute}:00+00:00")
                    cur = await db.db.execute(
                        "SELECT payload_hash, seen_count, first_seen_at, last_seen_at "
                        "FROM oauth_usage_snapshot WHERE key_id = 'key-1'")
                    rows = [dict(r) for r in await cur.fetchall()]
                    self.assertEqual(len(rows), 1, rows)
                    self.assertEqual(rows[0]["seen_count"], 3)
                    self.assertEqual(rows[0]["first_seen_at"], "2026-09-01T17:50:00+00:00")
                    # last_seen_at is what proves the state was still being
                    # served, rather than polling having merely stopped
                    self.assertEqual(rows[0]["last_seen_at"], "2026-09-01T17:52:00+00:00")
                finally:
                    await db.close()

        self._run(scenario())

    def test_only_whitelisted_headers_are_stored(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await self._record(db, self._pool(), usage(74.0, FIVE_RAW),
                                       "2026-09-01T17:58:40+00:00")
                    cur = await db.db.execute(
                        "SELECT headers_json FROM oauth_usage_snapshot LIMIT 1")
                    stored = json.loads((await cur.fetchone())["headers_json"])
                    self.assertEqual(stored.get("request-id"), "req_1")
                    self.assertEqual(stored.get("anthropic-organization-id"), "org-1")
                    self.assertNotIn("x-ignored", stored)
                finally:
                    await db.close()

        self._run(scenario())

    def test_ordinary_weekly_rollover_records_no_wipe(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    pool = self._pool()
                    await self._record(db, pool, usage(100.0, FIVE_RAW),
                                       "2026-08-27T10:59:00+00:00")
                    rolled = usage(0.0, FIVE_RAW)
                    rolled["seven_day"]["resets_at"] = "2026-09-10T11:00:00.252962+00:00"
                    rolled["limits"][0]["resets_at"] = "2026-09-10T11:00:00.252962+00:00"
                    await self._record(db, pool, rolled, "2026-09-03T11:01:00+00:00")
                    self.assertEqual(await db.list_oauth_limit_wipes("key-1"), [])
                finally:
                    await db.close()

        self._run(scenario())

    def test_detection_survives_an_unavailable_database(self):
        """The breaker fails every query; the wipe must still be detected."""
        async def scenario():
            class DeadDb:
                async def record_oauth_usage_snapshot(self, *a, **k):
                    raise RuntimeError("db unavailable")

                async def record_oauth_limit_wipe(self, *a, **k):
                    raise RuntimeError("db unavailable")

                async def record_oauth_window_observations(self, *a, **k):
                    raise RuntimeError("db unavailable")

            pool = self._pool()
            alerted: list[dict] = []
            pool.alert_limit_wipe = alerted.append  # type: ignore[method-assign]
            dead = DeadDb()
            await self._record(dead, pool, usage(74.0, FIVE_RAW),
                               "2026-09-01T17:58:40+00:00")
            await self._record(dead, pool, usage(0.0, FIVE_NEXT_RAW, 0.0),
                               "2026-09-01T18:01:00+00:00")
            self.assertEqual(len(alerted), 1, alerted)
            self.assertEqual(alerted[0]["from_utilization"], 74.0)

        self._run(scenario())

    def test_prune_keeps_snapshots_a_wipe_points_at(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    pool = self._pool()
                    await self._record(db, pool, usage(74.0, FIVE_RAW),
                                       "2026-09-01T17:58:40+00:00")
                    await self._record(db, pool, usage(0.0, FIVE_NEXT_RAW, 0.0),
                                       "2026-09-01T18:01:00+00:00")
                    # a third, unreferenced state
                    await self._record(db, pool, usage(5.0, FIVE_NEXT_RAW, 1.0),
                                       "2026-09-01T18:20:00+00:00")

                    deleted = await db.prune_oauth_usage_snapshots(
                        before="2026-12-01T00:00:00+00:00")
                    self.assertEqual(deleted, 1)
                    cur = await db.db.execute(
                        "SELECT COUNT(*) AS n FROM oauth_usage_snapshot")
                    self.assertEqual((await cur.fetchone())["n"], 2)
                finally:
                    await db.close()

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
