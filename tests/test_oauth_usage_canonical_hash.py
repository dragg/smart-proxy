from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import _canonical_usage_hash


def payload(five_hour_resets: str, seven_day_util: float = 74.0) -> dict:
    return {
        "five_hour": {"utilization": 6.0, "resets_at": five_hour_resets},
        "seven_day": {
            "utilization": seven_day_util,
            "resets_at": "2026-09-03T11:00:00.252962+00:00",
        },
        "nimbus_quill": {"utilization": 0.0, "resets_at": None},
        "extra_usage": {"is_enabled": False, "user_disabled": True},
        "limits": [
            {
                "kind": "session",
                "percent": 6,
                "resets_at": five_hour_resets,
                "scope": None,
                "is_active": True,
            },
        ],
    }


class CanonicalUsageHashTest(unittest.TestCase):
    def test_sub_second_jitter_does_not_change_the_hash(self):
        """Upstream jitters the fractional seconds of resets_at between polls.

        Left alone it would make every single poll a new snapshot row.
        """
        a = payload("2026-09-01T23:00:00.252940+00:00")
        b = payload("2026-09-01T23:00:00.874141+00:00")
        self.assertEqual(_canonical_usage_hash(a), _canonical_usage_hash(b))

    def test_whole_minute_drift_does_change_the_hash(self):
        """Minute-level movement is real upstream behaviour worth a row."""
        a = payload("2026-09-01T23:00:00.252940+00:00")
        b = payload("2026-09-01T23:01:00.252940+00:00")
        self.assertNotEqual(_canonical_usage_hash(a), _canonical_usage_hash(b))

    def test_utilization_change_changes_the_hash(self):
        a = payload("2026-09-01T23:00:00.252940+00:00", seven_day_util=74.0)
        b = payload("2026-09-01T23:00:00.252940+00:00", seven_day_util=0.0)
        self.assertNotEqual(_canonical_usage_hash(a), _canonical_usage_hash(b))

    def test_key_order_does_not_change_the_hash(self):
        a = payload("2026-09-01T23:00:00.252940+00:00")
        b = {k: a[k] for k in reversed(list(a))}
        self.assertEqual(_canonical_usage_hash(a), _canonical_usage_hash(b))

    def test_fields_the_extractor_discards_still_affect_the_hash(self):
        """extra_usage and the null-resets_at codename buckets are signal.

        The extractor drops them; the snapshot must not, or a payload change
        confined to them would be invisible.
        """
        a = payload("2026-09-01T23:00:00.252940+00:00")
        b = payload("2026-09-01T23:00:00.252940+00:00")
        b["nimbus_quill"] = {"utilization": 12.0, "resets_at": None}
        self.assertNotEqual(_canonical_usage_hash(a), _canonical_usage_hash(b))

        c = payload("2026-09-01T23:00:00.252940+00:00")
        c["extra_usage"] = {"is_enabled": True, "user_disabled": False}
        self.assertNotEqual(_canonical_usage_hash(a), _canonical_usage_hash(c))

    def test_nested_resets_at_inside_limits_is_truncated_too(self):
        a = payload("2026-09-01T23:00:00.252940+00:00")
        b = payload("2026-09-01T23:00:00.252940+00:00")
        b["limits"][0]["resets_at"] = "2026-09-01T23:00:00.999999+00:00"
        self.assertEqual(_canonical_usage_hash(a), _canonical_usage_hash(b))

    def test_non_dict_payload_is_not_fatal(self):
        self.assertIsInstance(_canonical_usage_hash({}), str)


if __name__ == "__main__":
    unittest.main()
