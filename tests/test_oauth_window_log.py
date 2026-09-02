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


def _obs(**overrides: object) -> dict:
    base = {
        "window_kind": "seven_day",
        "resets_at": "2026-07-02T11:00:00+00:00",
        "resets_at_raw": "2026-07-02T11:00:00.028998+00:00",
        "utilization": 0.0,
    }
    base.update(overrides)
    return base


class OauthWindowLogTests(unittest.TestCase):
    def _run(self, coro) -> None:
        asyncio.run(coro)

    def test_first_observation_inserts_row_without_reset_event(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    resets = await db.record_oauth_window_observations(
                        "key-1", [_obs()], seen_at="2026-07-01T05:00:00+00:00"
                    )
                    self.assertEqual(resets, [])
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertEqual(row["window_kind"], "seven_day")
                    self.assertEqual(row["resets_at"], "2026-07-02T11:00:00+00:00")
                    self.assertEqual(
                        row["resets_at_raw"], "2026-07-02T11:00:00.028998+00:00"
                    )
                    self.assertEqual(row["first_seen_at"], "2026-07-01T05:00:00+00:00")
                    self.assertEqual(row["last_seen_at"], "2026-07-01T05:00:00+00:00")
                    self.assertIsNone(row["first_active_at"])  # utilization == 0
                    self.assertEqual(row["observations"], 1)
                    self.assertEqual(row["last_utilization"], 0.0)
                    self.assertEqual(row["max_utilization"], 0.0)
                finally:
                    await db.close()

        self._run(scenario())

    def test_same_window_updates_in_place(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=0.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    # usage starts: first_active_at stamps, max/last update
                    resets = await db.record_oauth_window_observations(
                        "key-1",
                        [_obs(
                            utilization=55.0,
                            resets_at_raw="2026-07-02T11:00:00.874164+00:00",
                        )],
                        seen_at="2026-07-01T09:00:00+00:00",
                    )
                    self.assertEqual(resets, [])
                    # utilization dips: max stays, last follows
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=40.0)],
                        seen_at="2026-07-01T10:00:00+00:00",
                    )
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertEqual(row["observations"], 3)
                    self.assertEqual(row["first_seen_at"], "2026-07-01T05:00:00+00:00")
                    self.assertEqual(row["first_active_at"], "2026-07-01T09:00:00+00:00")
                    self.assertEqual(row["last_seen_at"], "2026-07-01T10:00:00+00:00")
                    self.assertEqual(row["last_utilization"], 40.0)
                    self.assertEqual(row["max_utilization"], 55.0)
                    self.assertEqual(
                        row["max_utilization_at"], "2026-07-01T09:00:00+00:00"
                    )
                    self.assertEqual(
                        row["resets_at_raw"], "2026-07-02T11:00:00.028998+00:00"
                    )
                finally:
                    await db.close()

        self._run(scenario())

    def test_changed_resets_at_creates_new_row_and_reset_event(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=71.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    resets = await db.record_oauth_window_observations(
                        "key-1",
                        [_obs(
                            resets_at="2026-07-06T16:00:00+00:00",
                            resets_at_raw="2026-07-06T16:00:00.111111+00:00",
                            utilization=1.0,
                        )],
                        seen_at="2026-07-02T12:00:00+00:00",
                    )
                    self.assertEqual(len(resets), 1)
                    event = resets[0]
                    self.assertEqual(event["key_id"], "key-1")
                    self.assertEqual(event["window_kind"], "seven_day")
                    self.assertEqual(
                        event["prev_resets_at"], "2026-07-02T11:00:00+00:00"
                    )
                    self.assertEqual(
                        event["new_resets_at"], "2026-07-06T16:00:00+00:00"
                    )
                    self.assertEqual(event["span_days"], 4.21)
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(rows[0]["resets_at"], "2026-07-02T11:00:00+00:00")
                    self.assertEqual(rows[1]["resets_at"], "2026-07-06T16:00:00+00:00")
                    self.assertEqual(
                        rows[1]["first_active_at"], "2026-07-02T12:00:00+00:00"
                    )
                finally:
                    await db.close()

        self._run(scenario())

    def test_sharp_utilization_drop_within_window_records_drop_event(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=85.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    # 85% -> 0% while resets_at unchanged: undeclared reset
                    events = await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=0.0)],
                        seen_at="2026-07-01T05:02:00+00:00",
                    )
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event["type"], "utilization_drop")
                    self.assertEqual(event["key_id"], "key-1")
                    self.assertEqual(event["window_kind"], "seven_day")
                    self.assertEqual(event["resets_at"], "2026-07-02T11:00:00+00:00")
                    self.assertEqual(event["from_utilization"], 85.0)
                    self.assertEqual(event["to_utilization"], 0.0)
                    self.assertEqual(event["prev_seen_at"], "2026-07-01T05:00:00+00:00")
                    self.assertEqual(event["dropped_at"], "2026-07-01T05:02:00+00:00")

                    drops = await db.list_oauth_window_drops("key-1")
                    self.assertEqual(len(drops), 1)
                    self.assertEqual(drops[0]["from_utilization"], 85.0)
                    self.assertEqual(drops[0]["to_utilization"], 0.0)
                    self.assertEqual(drops[0]["resets_at"], "2026-07-02T11:00:00+00:00")

                    # window row still one, updated in place, peak preserved
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["observations"], 2)
                    self.assertEqual(rows[0]["max_utilization"], 85.0)
                    self.assertEqual(rows[0]["last_utilization"], 0.0)
                finally:
                    await db.close()

        self._run(scenario())

    def test_gradual_decline_does_not_record_drop(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=85.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    # rolling-window ageing: -2pp per poll, below threshold
                    events = await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=83.0)],
                        seen_at="2026-07-01T05:02:00+00:00",
                    )
                    self.assertEqual(events, [])
                    # exactly at threshold boundary: 83 -> 78 (=5pp) records
                    events = await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=78.0)],
                        seen_at="2026-07-01T05:04:00+00:00",
                    )
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["type"], "utilization_drop")
                    self.assertEqual(await db.list_oauth_window_drops("key-2"), [])
                finally:
                    await db.close()

        self._run(scenario())

    def test_new_window_row_does_not_record_drop(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=85.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    # declared reset: resets_at changed, 85 -> 0 is NOT a drop
                    events = await db.record_oauth_window_observations(
                        "key-1",
                        [_obs(
                            resets_at="2026-07-06T16:00:00+00:00",
                            resets_at_raw="2026-07-06T16:00:00.111111+00:00",
                            utilization=0.0,
                        )],
                        seen_at="2026-07-02T12:00:00+00:00",
                    )
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["type"], "window_reset")
                    self.assertEqual(await db.list_oauth_window_drops("key-1"), [])
                finally:
                    await db.close()

        self._run(scenario())

    def test_minute_level_jitter_matches_same_window(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=85.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    # upstream moved resets_at by 5 minutes: same window,
                    # and the 85->0 fall is still detected as a drop
                    events = await db.record_oauth_window_observations(
                        "key-1",
                        [_obs(
                            resets_at="2026-07-02T11:05:00+00:00",
                            resets_at_raw="2026-07-02T11:05:00.123456+00:00",
                            utilization=0.0,
                        )],
                        seen_at="2026-07-01T05:02:00+00:00",
                    )
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["type"], "utilization_drop")
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    # identity keeps the first-observed resets_at
                    self.assertEqual(row["resets_at"], "2026-07-02T11:00:00+00:00")
                    self.assertEqual(
                        row["resets_at_raw"], "2026-07-02T11:05:00.123456+00:00"
                    )
                    self.assertEqual(row["observations"], 2)

                    # beyond the 120-minute tolerance: a genuinely new window
                    events = await db.record_oauth_window_observations(
                        "key-1",
                        [_obs(
                            resets_at="2026-07-02T14:00:00+00:00",
                            resets_at_raw="2026-07-02T14:00:00+00:00",
                            utilization=1.0,
                        )],
                        seen_at="2026-07-01T06:00:00+00:00",
                    )
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["type"], "window_reset")
                    self.assertEqual(len(await db.list_oauth_window_log("key-1")), 2)
                finally:
                    await db.close()

        self._run(scenario())

    def test_kinds_are_independent_and_keys_isolated(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    resets = await db.record_oauth_window_observations(
                        "key-1",
                        [
                            _obs(),
                            _obs(window_kind="five_hour",
                                 resets_at="2026-07-01T10:00:00+00:00",
                                 utilization=4.0),
                            _obs(window_kind="limit:weekly_scoped:Fable",
                                 utilization=None),
                        ],
                    )
                    self.assertEqual(resets, [])
                    await db.record_oauth_window_observations("key-2", [_obs()])
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 3)
                    kinds = [r["window_kind"] for r in rows]
                    self.assertEqual(
                        kinds,
                        ["five_hour", "limit:weekly_scoped:Fable", "seven_day"],
                    )
                    scoped = rows[1]
                    self.assertIsNone(scoped["last_utilization"])
                    self.assertIsNone(scoped["max_utilization"])
                    self.assertIsNone(scoped["first_active_at"])
                    self.assertEqual(len(await db.list_oauth_window_log("key-2")), 1)
                finally:
                    await db.close()

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
