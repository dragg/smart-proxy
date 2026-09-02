from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import AnthropicKeyPool

WEEK = "2026-09-03T11:00:00+00:00"


def alias_only(utilization: float) -> list[dict]:
    """A payload carrying the weekly counter solely under its limits[] alias."""
    return [{
        "window_kind": "limit:weekly_all",
        "resets_at": WEEK,
        "resets_at_raw": WEEK.replace("+00:00", ".252962+00:00"),
        "utilization": utilization,
    }]


def both_kinds(utilization: float) -> list[dict]:
    raw = WEEK.replace("+00:00", ".252962+00:00")
    return [
        {"window_kind": "seven_day", "resets_at": WEEK,
         "resets_at_raw": raw, "utilization": utilization},
        {"window_kind": "limit:weekly_all", "resets_at": WEEK,
         "resets_at_raw": raw, "utilization": utilization},
    ]


class ObserveWindowsAliasTests(unittest.TestCase):
    """The alias must keep advancing state when no top-level block exists."""

    def setUp(self):
        self.pool = AnthropicKeyPool(MagicMock())
        self.pool._notifier = None

    def test_alias_only_payload_keeps_advancing_state(self):
        for minute, utilization in enumerate((10.0, 20.0, 30.0)):
            wipes = self.pool.observe_windows(
                "k1", alias_only(utilization),
                seen_at=f"2026-09-01T17:0{minute}:00+00:00")
            self.assertEqual(wipes, [])
        state = self.pool._window_state[("k1", "seven_day")]
        self.assertEqual(state.utilization, 30.0)
        self.assertEqual(state.seen_at, "2026-09-01T17:02:00+00:00")

    def test_alias_only_wipe_reports_the_immediately_preceding_value(self):
        for minute, utilization in enumerate((10.0, 20.0, 30.0)):
            self.pool.observe_windows(
                "k1", alias_only(utilization),
                seen_at=f"2026-09-01T17:0{minute}:00+00:00")

        wipes = self.pool.observe_windows(
            "k1", alias_only(0.0), seen_at="2026-09-01T17:03:00+00:00")
        self.assertEqual(len(wipes), 1)
        self.assertEqual(wipes[0]["from_utilization"], 30.0)
        self.assertEqual(wipes[0]["prev_seen_at"], "2026-09-01T17:02:00+00:00")

    def test_recovery_after_a_wipe_does_not_re_wipe_from_stale_state(self):
        self.pool.observe_windows("k1", alias_only(10.0),
                                  seen_at="2026-09-01T17:00:00+00:00")
        self.pool.observe_windows("k1", alias_only(0.0),
                                  seen_at="2026-09-01T17:01:00+00:00")
        self.pool.observe_windows("k1", alias_only(5.0),
                                  seen_at="2026-09-01T17:02:00+00:00")
        wipes = self.pool.observe_windows(
            "k1", alias_only(0.0), seen_at="2026-09-01T17:03:00+00:00")
        # a second wipe is legitimate here, but it must carry the fresh values
        self.assertEqual(len(wipes), 1)
        self.assertEqual(wipes[0]["from_utilization"], 5.0)
        self.assertEqual(wipes[0]["prev_seen_at"], "2026-09-01T17:02:00+00:00")

    def test_primary_kind_still_wins_over_the_alias_within_one_batch(self):
        self.pool.observe_windows("k1", both_kinds(40.0),
                                  seen_at="2026-09-01T17:00:00+00:00")
        self.assertNotIn(("k1", "limit:weekly_all"), self.pool._window_state)
        self.assertEqual(
            self.pool._window_state[("k1", "seven_day")].utilization, 40.0)


class UnifiedHeaderChannelTests(unittest.TestCase):
    def setUp(self):
        self.pool = AnthropicKeyPool(MagicMock())
        self.pool._notifier = None

    def test_one_point_flicker_near_zero_is_not_a_wipe(self):
        """0.01 -> 0.00 is one quantization step, not a wipe."""
        seen = [
            self.pool.observe_unified_headers("k1", value, "1775552400")
            for value in (0.0, 1.0, 0.0, 1.0, 0.0)
        ]
        self.assertEqual([w for w in seen if w is not None], [])

    def test_a_real_wipe_is_reported_once_and_latched(self):
        self.assertIsNone(self.pool.observe_unified_headers("k1", 74.0, "1775552400"))
        wipe = self.pool.observe_unified_headers("k1", 0.0, "1775552400")
        self.assertIsNotNone(wipe)
        self.assertEqual(wipe["from_utilization"], 74.0)
        self.assertEqual(wipe["source"], "headers")
        # a late response reinstating a nonzero reading must not re-arm it
        self.assertIsNone(self.pool.observe_unified_headers("k1", 74.0, "1775552400"))
        self.assertIsNone(self.pool.observe_unified_headers("k1", 0.0, "1775552400"))

    def test_the_latch_clears_when_the_window_moves(self):
        self.pool.observe_unified_headers("k1", 74.0, "1775552400")
        self.assertIsNotNone(self.pool.observe_unified_headers("k1", 0.0, "1775552400"))
        # new weekly window; a wipe in it is a new event
        self.assertIsNone(self.pool.observe_unified_headers("k1", 60.0, "1776157200"))
        self.assertIsNotNone(
            self.pool.observe_unified_headers("k1", 0.0, "1776157200"))

    def test_prev_seen_at_is_the_earlier_observation_not_now(self):
        """Otherwise the bracket around a wipe collapses to zero width."""
        self.pool.observe_unified_headers("k1", 74.0, "1775552400")
        first_seen_at = self.pool._unified_state["k1"][2]
        wipe = self.pool.observe_unified_headers("k1", 0.0, "1775552400")
        self.assertEqual(wipe["prev_seen_at"], first_seen_at)
        self.assertNotEqual(wipe["prev_seen_at"], wipe["observed_at"])

    def test_a_moved_reset_epoch_is_an_ordinary_rollover(self):
        self.pool.observe_unified_headers("k1", 74.0, "1775552400")
        self.assertIsNone(self.pool.observe_unified_headers("k1", 0.0, "1776157200"))


if __name__ == "__main__":
    unittest.main()
