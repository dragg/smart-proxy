from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database

WEEK = "2026-09-03T11:00:00+00:00"


def obs(kind: str, utilization: float | None, resets_at: str = WEEK) -> dict:
    return {
        "window_kind": kind,
        "resets_at": resets_at,
        "resets_at_raw": resets_at.replace("+00:00", ".252962+00:00"),
        "utilization": utilization,
    }


class ReconcileLimitWipesTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    async def _db(self, td):
        db = Database(str(Path(td) / "t.db"))
        await db.connect()
        return db

    def test_weekly_zero_drop_below_the_threshold_reaches_the_drop_log(self):
        """Otherwise reconciliation has nothing to recover a small wipe from."""
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 3.0)],
                        seen_at="2026-09-01T17:58:00+00:00")
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 0.0)],
                        seen_at="2026-09-01T18:01:00+00:00")
                    drops = await db.list_oauth_window_drops("k1")
                    self.assertEqual(len(drops), 1, drops)
                    self.assertEqual(drops[0]["from_utilization"], 3.0)
                finally:
                    await db.close()

        self._run(scenario())

    def test_a_small_non_weekly_drop_still_obeys_the_threshold(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    for util, at in ((3.0, "17:58"), (1.0, "18:01")):
                        await db.record_oauth_window_observations(
                            "k1", [obs("five_hour", util,
                                       "2026-09-01T23:00:00+00:00")],
                            seen_at=f"2026-09-01T{at}:00+00:00")
                    self.assertEqual(await db.list_oauth_window_drops("k1"), [])
                finally:
                    await db.close()

        self._run(scenario())

    def test_reconciliation_recovers_a_wipe_the_detector_missed(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    # a five_hour window rolls in the same observation
                    await db.record_oauth_window_observations(
                        "k1",
                        [obs("seven_day", 74.0),
                         obs("five_hour", 6.0, "2026-09-01T20:40:00+00:00")],
                        seen_at="2026-09-01T17:58:40+00:00")
                    await db.record_oauth_window_observations(
                        "k1",
                        [obs("seven_day", 0.0),
                         obs("five_hour", 0.0, "2026-09-01T23:00:00+00:00")],
                        seen_at="2026-09-01T18:01:00+00:00")

                    self.assertEqual(await db.list_oauth_limit_wipes("k1"), [])
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 1)

                    wipes = await db.list_oauth_limit_wipes("k1")
                    self.assertEqual(len(wipes), 1)
                    w = wipes[0]
                    self.assertEqual(w["window_kind"], "seven_day")
                    self.assertEqual(w["from_utilization"], 74.0)
                    self.assertEqual(w["source"], "backfill")
                    self.assertEqual(w["prev_seen_at"], "2026-09-01T17:58:40+00:00")
                    self.assertEqual(w["five_hour_rolled"], 1)
                    self.assertAlmostEqual(w["five_hour_early_minutes"], 159.0, places=0)
                    self.assertAlmostEqual(w["hours_before_claimed"], 41.0, places=0)
                    self.assertIsNone(w["snapshot_id"])
                finally:
                    await db.close()

        self._run(scenario())

    def test_reconciliation_is_idempotent(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 74.0)],
                        seen_at="2026-09-01T17:58:40+00:00")
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 0.0)],
                        seen_at="2026-09-01T18:01:00+00:00")
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 1)
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 0)
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 0)
                    self.assertEqual(len(await db.list_oauth_limit_wipes("k1")), 1)
                finally:
                    await db.close()

        self._run(scenario())

    def test_a_wipe_the_detector_already_wrote_is_not_duplicated(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 74.0)],
                        seen_at="2026-09-01T17:58:40+00:00")
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 0.0)],
                        seen_at="2026-09-01T18:01:00+00:00")
                    # the detector's own row, at the same observation instant
                    await db.record_oauth_limit_wipe({
                        "key_id": "k1", "window_kind": "seven_day",
                        "observed_at": "2026-09-01T18:01:00+00:00",
                        "prev_seen_at": "2026-09-01T17:58:40+00:00",
                        "from_utilization": 74.0, "resets_at_claimed": WEEK,
                        "source": "poll", "snapshot_id": 7,
                    })
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 0)
                    wipes = await db.list_oauth_limit_wipes("k1")
                    self.assertEqual(len(wipes), 1)
                    self.assertEqual(wipes[0]["source"], "poll")
                    self.assertEqual(wipes[0]["snapshot_id"], 7)
                finally:
                    await db.close()

        self._run(scenario())

    def test_duplicate_kinds_collapse_to_one_recovered_row(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    for util, at in ((74.0, "17:58"), (0.0, "18:01")):
                        await db.record_oauth_window_observations(
                            "k1",
                            [obs("seven_day", util), obs("limit:weekly_all", util)],
                            seen_at=f"2026-09-01T{at}:00+00:00")
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 1)
                    wipes = await db.list_oauth_limit_wipes("k1")
                    self.assertEqual([w["window_kind"] for w in wipes], ["seven_day"])
                finally:
                    await db.close()

        self._run(scenario())

    def test_alias_only_key_is_not_discarded_by_the_collapse(self):
        """A payload with no top-level seven_day must still yield a wipe."""
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    for util, at in ((74.0, "17:58"), (0.0, "18:01")):
                        await db.record_oauth_window_observations(
                            "k1", [obs("limit:weekly_all", util)],
                            seen_at=f"2026-09-01T{at}:00+00:00")
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 1)
                    wipes = await db.list_oauth_limit_wipes("k1")
                    self.assertEqual([w["window_kind"] for w in wipes], ["seven_day"])
                finally:
                    await db.close()

        self._run(scenario())

    def test_a_five_hour_window_born_after_the_drop_is_not_the_roll(self):
        """Two independently cached call sites can land observations a minute
        apart, so a 5h window opening *after* the drop is reachable. Counting
        it as the roll would also make the 'previous' window the wrong one, and
        would contradict what the live detector records for the same event."""
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 74.0)],
                        seen_at="2026-09-01T17:58:40+00:00")
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 0.0)],
                        seen_at="2026-09-01T18:01:00+00:00")
                    # the 5h window rolls a minute LATER, via the other path
                    await db.record_oauth_window_observations(
                        "k1", [obs("five_hour", 0.0, "2026-09-01T23:00:00+00:00")],
                        seen_at="2026-09-01T18:02:00+00:00")

                    recovered = await db.reconcile_limit_wipes_from_drops()
                    self.assertEqual(len(recovered), 1)
                    self.assertEqual(recovered[0]["five_hour_rolled"], 0)
                    self.assertIsNone(recovered[0]["five_hour_early_minutes"])
                finally:
                    await db.close()

        self._run(scenario())

    def test_recovered_rows_are_returned_for_alerting(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 74.0)],
                        seen_at="2026-09-01T17:58:40+00:00")
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 0.0)],
                        seen_at="2026-09-01T18:01:00+00:00")
                    recovered = await db.reconcile_limit_wipes_from_drops()
                    self.assertEqual(len(recovered), 1)
                    row = recovered[0]
                    # the shape alert_limit_wipe reads
                    for field in ("key_id", "window_kind", "observed_at",
                                  "from_utilization", "resets_at_claimed",
                                  "hours_before_claimed", "five_hour_rolled",
                                  "five_hour_early_minutes", "source"):
                        self.assertIn(field, row)
                    self.assertEqual(row["source"], "backfill")
                finally:
                    await db.close()

        self._run(scenario())

    def test_an_ordinary_weekly_rollover_is_not_recovered(self):
        """resets_at moved, so the drop log holds nothing to recover."""
        async def scenario():
            with tempfile.TemporaryDirectory() as td:
                db = await self._db(td)
                try:
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 100.0)],
                        seen_at="2026-08-27T10:59:00+00:00")
                    await db.record_oauth_window_observations(
                        "k1", [obs("seven_day", 0.0, "2026-09-10T11:00:00+00:00")],
                        seen_at="2026-09-03T11:01:00+00:00")
                    self.assertEqual(len(await db.reconcile_limit_wipes_from_drops()), 0)
                finally:
                    await db.close()

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
