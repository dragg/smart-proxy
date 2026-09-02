from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import (
    _extract_window_observations,
    _truncate_resets_at_minute,
)

# Real /api/oauth/usage shape observed 2026-07-01 (trimmed).
REAL_PAYLOAD = {
    "five_hour": {
        "utilization": 4.0,
        "resets_at": "2026-07-02T02:10:00.028978+00:00",
        "limit_dollars": None,
    },
    "seven_day": {
        "utilization": 1.0,
        "resets_at": "2026-07-02T11:00:00.028998+00:00",
        "limit_dollars": None,
    },
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": False, "monthly_limit": None},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 4,
            "severity": "normal",
            "resets_at": "2026-07-02T02:10:00.874141+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 1,
            "severity": "normal",
            "resets_at": "2026-07-02T11:00:00.874164+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 0,
            "severity": "normal",
            "resets_at": "2026-07-02T11:00:00.874513+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ],
}


class TruncateResetsAtTests(unittest.TestCase):
    def test_truncates_to_minute_in_utc(self) -> None:
        self.assertEqual(
            _truncate_resets_at_minute("2026-07-02T02:10:00.874141+00:00"),
            "2026-07-02T02:10:00+00:00",
        )

    def test_jittered_fractions_map_to_same_identity(self) -> None:
        a = _truncate_resets_at_minute("2026-07-02T11:00:00.028998+00:00")
        b = _truncate_resets_at_minute("2026-07-02T11:00:00.874164+00:00")
        self.assertEqual(a, b)

    def test_z_suffix_and_offsets_normalize_to_utc(self) -> None:
        self.assertEqual(
            _truncate_resets_at_minute("2026-07-02T14:00:59Z"),
            "2026-07-02T14:00:00+00:00",
        )
        self.assertEqual(
            _truncate_resets_at_minute("2026-07-02T13:00:00+02:00"),
            "2026-07-02T11:00:00+00:00",
        )

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(_truncate_resets_at_minute("not-a-date"))
        self.assertIsNone(_truncate_resets_at_minute(""))


class ExtractWindowObservationsTests(unittest.TestCase):
    def test_real_payload_yields_all_windows(self) -> None:
        obs = _extract_window_observations(REAL_PAYLOAD)
        by_kind = {o["window_kind"]: o for o in obs}
        self.assertEqual(
            set(by_kind),
            {
                "five_hour",
                "seven_day",
                "limit:session",
                "limit:weekly_all",
                "limit:weekly_scoped:Fable",
            },
        )
        seven = by_kind["seven_day"]
        self.assertEqual(seven["resets_at"], "2026-07-02T11:00:00+00:00")
        self.assertEqual(
            seven["resets_at_raw"], "2026-07-02T11:00:00.028998+00:00"
        )
        self.assertEqual(seven["utilization"], 1.0)
        scoped = by_kind["limit:weekly_scoped:Fable"]
        self.assertEqual(scoped["utilization"], 0.0)  # limits use int percent
        self.assertEqual(by_kind["limit:session"]["utilization"], 4.0)

    def test_null_windows_and_extra_usage_are_skipped(self) -> None:
        kinds = {o["window_kind"] for o in _extract_window_observations(REAL_PAYLOAD)}
        self.assertNotIn("seven_day_opus", kinds)
        self.assertNotIn("extra_usage", kinds)

    def test_unscoped_limit_kind_has_no_suffix(self) -> None:
        payload = {"limits": [{"kind": "weekly_all", "percent": 5,
                               "resets_at": "2026-07-09T11:00:00+00:00"}]}
        obs = _extract_window_observations(payload)
        self.assertEqual(obs[0]["window_kind"], "limit:weekly_all")

    def test_malformed_input_is_tolerated(self) -> None:
        self.assertEqual(_extract_window_observations({}), [])
        self.assertEqual(_extract_window_observations(None), [])
        self.assertEqual(
            _extract_window_observations(
                {
                    "five_hour": {"utilization": 3.0, "resets_at": "garbage"},
                    "seven_day": "not-a-dict",
                    "limits": [None, {"kind": None, "resets_at": "2026-07-09T11:00:00Z"},
                               {"kind": "x"}],
                }
            ),
            [],
        )

    def test_missing_utilization_becomes_none(self) -> None:
        payload = {"five_hour": {"resets_at": "2026-07-02T02:10:00+00:00"}}
        obs = _extract_window_observations(payload)
        self.assertEqual(len(obs), 1)
        self.assertIsNone(obs[0]["utilization"])


if __name__ == "__main__":
    unittest.main()
