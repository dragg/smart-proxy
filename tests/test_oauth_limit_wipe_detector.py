from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import _WindowState, _detect_limit_wipes

WEEK = "2026-09-03T11:00:00+00:00"
FIVE_H = "2026-09-01T20:40:00+00:00"


def obs(kind: str, resets_at: str, utilization: float | None, raw: str | None = None):
    return {
        "window_kind": kind,
        "resets_at": resets_at,
        "resets_at_raw": raw or resets_at.replace("+00:00", ".252940+00:00"),
        "utilization": utilization,
    }


def state(utilization, resets_at, seen_at, raw=None):
    return _WindowState(
        utilization=utilization,
        resets_at=resets_at,
        resets_at_raw=raw or resets_at.replace("+00:00", ".252940+00:00"),
        seen_at=seen_at,
    )


class DetectLimitWipesTest(unittest.TestCase):
    def test_the_real_2026_09_01_event(self):
        """74% -> 0% with resets_at unchanged, alongside an early 5h re-open."""
        prev = {
            "seven_day": state(74.0, WEEK, "2026-09-01T17:58:40+00:00"),
            "five_hour": state(6.0, FIVE_H, "2026-09-01T17:58:40+00:00"),
        }
        observations = [
            obs("five_hour", "2026-09-01T23:00:00+00:00", 0.0),
            obs("seven_day", WEEK, 0.0),
            obs("limit:weekly_all", WEEK, 0.0),
        ]
        wipes = _detect_limit_wipes(
            prev, observations, key_id="k1", seen_at="2026-09-01T18:01:00+00:00")
        self.assertEqual(len(wipes), 1, wipes)
        w = wipes[0]
        self.assertEqual(w["window_kind"], "seven_day")
        self.assertEqual(w["from_utilization"], 74.0)
        self.assertEqual(w["prev_seen_at"], "2026-09-01T17:58:40+00:00")
        self.assertTrue(w["five_hour_rolled"])
        # opened 159 min before the previous 5h window's own claimed reset
        self.assertAlmostEqual(w["five_hour_early_minutes"], 159.0, places=0)
        self.assertAlmostEqual(w["hours_before_claimed"], 41.0, places=0)

    def test_duplicate_kinds_collapse_to_one_wipe(self):
        """seven_day and limit:weekly_all are one upstream counter.

        Counting them separately would double every wipe.
        """
        prev = {"seven_day": state(74.0, WEEK, "2026-09-01T17:58:40+00:00")}
        observations = [obs("seven_day", WEEK, 0.0), obs("limit:weekly_all", WEEK, 0.0)]
        wipes = _detect_limit_wipes(
            prev, observations, key_id="k1", seen_at="2026-09-01T18:01:00+00:00")
        self.assertEqual([w["window_kind"] for w in wipes], ["seven_day"])

    def test_ordinary_five_hour_rollover_is_not_a_wipe(self):
        """The rule rejected in review would have fired here, ~294 times."""
        prev = {
            "five_hour": state(26.0, "2026-09-01T15:50:00+00:00", "2026-09-01T15:49:00+00:00"),
            "limit:session": state(26.0, "2026-09-01T15:50:00+00:00", "2026-09-01T15:49:00+00:00"),
        }
        observations = [
            obs("five_hour", "2026-09-01T20:40:00+00:00", 0.0),
            obs("limit:session", "2026-09-01T20:40:00+00:00", 0.0),
        ]
        wipes = _detect_limit_wipes(
            prev, observations, key_id="k1", seen_at="2026-09-01T15:52:09+00:00")
        self.assertEqual(wipes, [])

    def test_ordinary_weekly_rollover_is_not_a_wipe(self):
        """At the real Thursday boundary resets_at moves; that is a reset."""
        prev = {"seven_day": state(100.0, "2026-08-27T11:00:00+00:00", "2026-08-27T10:59:20+00:00")}
        observations = [obs("seven_day", "2026-09-03T11:00:00+00:00", 0.0)]
        wipes = _detect_limit_wipes(
            prev, observations, key_id="k1", seen_at="2026-08-27T11:01:27+00:00")
        self.assertEqual(wipes, [])

    def test_wipe_below_the_drop_log_threshold_still_counts(self):
        """3% -> 0 is under UTILIZATION_DROP_THRESHOLD_PP but is still a wipe."""
        prev = {"seven_day": state(3.0, WEEK, "2026-09-01T17:58:40+00:00")}
        observations = [obs("seven_day", WEEK, 0.0)]
        wipes = _detect_limit_wipes(
            prev, observations, key_id="k1", seen_at="2026-09-01T18:01:00+00:00")
        self.assertEqual(len(wipes), 1)
        self.assertEqual(wipes[0]["from_utilization"], 3.0)

    def test_model_scoped_wipe_without_a_five_hour_roll(self):
        """The Fable-scoped counter zeroed 2h47m after the main wipe, alone."""
        prev = {"limit:weekly_scoped:Fable": state(37.0, WEEK, "2026-09-01T20:45:00+00:00")}
        observations = [obs("limit:weekly_scoped:Fable", WEEK, 0.0)]
        wipes = _detect_limit_wipes(
            prev, observations, key_id="k1", seen_at="2026-09-01T20:48:36+00:00")
        self.assertEqual(len(wipes), 1)
        self.assertEqual(wipes[0]["window_kind"], "limit:weekly_scoped:Fable")
        self.assertFalse(wipes[0]["five_hour_rolled"])
        self.assertIsNone(wipes[0]["five_hour_early_minutes"])

    def test_decline_to_nonzero_is_not_a_wipe(self):
        prev = {"seven_day": state(74.0, WEEK, "2026-09-01T17:58:40+00:00")}
        observations = [obs("seven_day", WEEK, 60.0)]
        self.assertEqual(
            _detect_limit_wipes(prev, observations, key_id="k1",
                                seen_at="2026-09-01T18:01:00+00:00"), [])

    def test_already_zero_is_not_a_wipe(self):
        prev = {"seven_day": state(0.0, WEEK, "2026-09-01T17:58:40+00:00")}
        observations = [obs("seven_day", WEEK, 0.0)]
        self.assertEqual(
            _detect_limit_wipes(prev, observations, key_id="k1",
                                seen_at="2026-09-01T18:01:00+00:00"), [])

    def test_first_observation_is_not_a_wipe(self):
        observations = [obs("seven_day", WEEK, 0.0)]
        self.assertEqual(
            _detect_limit_wipes({}, observations, key_id="k1",
                                seen_at="2026-09-01T18:01:00+00:00"), [])

    def test_missing_utilization_is_ignored(self):
        prev = {"seven_day": state(74.0, WEEK, "2026-09-01T17:58:40+00:00")}
        observations = [obs("seven_day", WEEK, None)]
        self.assertEqual(
            _detect_limit_wipes(prev, observations, key_id="k1",
                                seen_at="2026-09-01T18:01:00+00:00"), [])


if __name__ == "__main__":
    unittest.main()
