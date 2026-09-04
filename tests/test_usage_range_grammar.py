# tests/test_usage_range_grammar.py
"""The dashboard range grammar: `YYYY-MM-DD` vs `YYYY-MM-DDTHH`.

The parameter form alone decides which table answers. Coverage-based selection
was rejected: it would make the same date query change source as the hour table
fills up, and would let `/api/usage` disagree with `/_usage` for a date both
can serve.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from smart_proxy.usage_dashboard import (
    _iter_hours,
    _usage_range,
    build_usage_bucket_series,
    build_usage_cost_json,
)


class _FrozenDatetime(datetime):
    _now = datetime(2026, 9, 5, 14, 37, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._now if tz is None else cls._now.astimezone(tz)


def _range(query: str):
    return _usage_range(make_mocked_request("GET", f"/api/usage?{query}"))


class UsageRangeGrammarTests(unittest.TestCase):
    def test_day_grammar_is_unchanged(self) -> None:
        rng = _range("start=2026-04-01&end=2026-04-02")
        self.assertEqual((rng.granularity, rng.start, rng.end),
                         ("day", "2026-04-01", "2026-04-02"))

    def test_no_params_defaults_to_the_last_seven_days(self) -> None:
        with patch("smart_proxy.usage_dashboard.datetime", _FrozenDatetime):
            rng = _range("")
        self.assertEqual((rng.granularity, rng.start, rng.end),
                         ("day", "2026-08-30", "2026-09-05"))

    def test_hour_grammar_canonicalises_a_single_digit_hour(self) -> None:
        """strptime accepts "T9"; the string range 'hour_utc >= ...T9' would be wrong."""
        rng = _range("start=2026-09-05T9&end=2026-09-05T14")
        self.assertEqual((rng.granularity, rng.start, rng.end),
                         ("hour", "2026-09-05T09", "2026-09-05T14"))

    def test_mixed_grammar_is_rejected(self) -> None:
        resp = _range("start=2026-09-05&end=2026-09-05T14")
        self.assertIsInstance(resp, web.Response)
        self.assertEqual(resp.status, 400)
        self.assertIn("both use", resp.text)

    def test_a_24_hour_clock_value_is_rejected(self) -> None:
        resp = _range("start=2026-09-05T24&end=2026-09-05T24")
        self.assertEqual(resp.status, 400)

    def test_start_after_end_is_rejected(self) -> None:
        resp = _range("start=2026-09-05T14&end=2026-09-05T10")
        self.assertEqual(resp.status, 400)
        self.assertIn("before or equal", resp.text)

    def test_a_span_at_the_cap_is_accepted(self) -> None:
        """2208 inclusive buckets — one hour short of 92 days."""
        rng = _range("start=2026-01-01T00&end=2026-04-02T23")
        self.assertEqual(rng.granularity, "hour")
        self.assertEqual(len(_iter_hours(rng.start, rng.end)), 2208)

    def test_a_span_over_the_cap_is_rejected(self) -> None:
        resp = _range("start=2026-01-01T00&end=2026-04-03T00")
        self.assertEqual(resp.status, 400)
        self.assertIn("92 days", resp.text)

    def test_hour_defaults_fill_in_the_missing_bound(self) -> None:
        with patch("smart_proxy.usage_dashboard.datetime", _FrozenDatetime):
            only_start = _range("start=2026-09-05T10")
            only_end = _range("end=2026-09-05T10")
        self.assertEqual(only_start.end, "2026-09-05T14", "defaults to the current hour")
        self.assertEqual(only_end.start, "2026-09-04T11", "24 buckets, crossing midnight")

    def test_iter_hours_crosses_a_day_boundary(self) -> None:
        self.assertEqual(
            _iter_hours("2026-09-05T22", "2026-09-06T01"),
            ["2026-09-05T22", "2026-09-05T23", "2026-09-06T00", "2026-09-06T01"],
        )


class UsageSeriesTests(unittest.TestCase):
    def _rows(self):
        return [
            {"hour_utc": "2026-09-05T10", "provider": "anthropic",
             "model": "claude-opus-4-8", "input_tokens": 1_000_000,
             "output_tokens": 0, "requests": 2},
            {"hour_utc": "2026-09-05T10", "provider": "anthropic",
             "model": "totally-unpriced", "input_tokens": 500,
             "output_tokens": 0, "requests": 1},
            {"hour_utc": "2026-09-05T12", "provider": "anthropic",
             "model": "claude-opus-4-8", "input_tokens": 1_000_000,
             "output_tokens": 0, "requests": 1},
        ]

    def test_series_zero_fills_and_prices_each_model_separately(self) -> None:
        series = build_usage_bucket_series(
            self._rows(), None, "2026-09-05T10", "2026-09-05T13"
        )
        self.assertEqual([p["hour"] for p in series],
                         ["2026-09-05T10", "2026-09-05T11",
                          "2026-09-05T12", "2026-09-05T13"])

        first = series[0]
        # 1M input tokens of claude-opus-4-8 at $5/M; the unpriced model adds
        # nothing to cost but flags the hour.
        self.assertAlmostEqual(first["cost"], 5.0, places=6)
        self.assertTrue(first["unknown"])
        self.assertEqual(first["requests"], 3)

        idle = series[1]
        self.assertEqual((idle["requests"], idle["cost"]), (0, 0.0))
        self.assertFalse(idle["unknown"])

    def test_rows_outside_the_range_are_ignored(self) -> None:
        series = build_usage_bucket_series(
            self._rows(), None, "2026-09-05T11", "2026-09-05T11"
        )
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["requests"], 0)


class UsageCostJsonShapeTests(unittest.TestCase):
    def test_day_payload_has_no_series_or_coverage(self) -> None:
        payload = build_usage_cost_json("2026-04-01", "2026-04-02", [], None)
        self.assertEqual(payload["granularity"], "day")
        self.assertNotIn("series", payload)
        self.assertNotIn("covered_from", payload)

    def test_hour_payload_carries_series_and_coverage(self) -> None:
        payload = build_usage_cost_json(
            "2026-09-05T10", "2026-09-05T10", [], None,
            granularity="hour", covered_from="2026-09-04T18", series=[{"hour": "x"}],
        )
        self.assertEqual(payload["granularity"], "hour")
        self.assertEqual(payload["covered_from"], "2026-09-04T18")
        self.assertEqual(payload["series"], [{"hour": "x"}])


if __name__ == "__main__":
    unittest.main()
