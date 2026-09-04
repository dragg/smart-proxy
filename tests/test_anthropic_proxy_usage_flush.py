# tests/test_anthropic_proxy_usage_flush.py
"""A partial flush must still attribute the rows that reached the database.

`UsageTracker.flush` now writes every usage table independently instead of
abandoning the buffers behind the first failure. When only some tables fail it
raises `UsageFlushError` carrying the `usage_daily` rows that were committed --
those rows are in the database, so if `_flush_usage` dropped them on the floor
they would never be counted against any OAuth rate-limit window.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import _flush_usage
from smart_proxy.usage import UsageFlushError


def _daily_row(credential_id: str, model: str, inp: int, out: int) -> tuple:
    """A usage_daily upsert row: 7 key columns then the 8 counters."""
    return (
        "2026-09-05", "sp-a", "acct-a", credential_id, "anthropic", model, 0,
        inp, out, 0, 0, 0, 0, 0, 1,
    )


class _Tracker:
    def __init__(self, result) -> None:
        self._result = result

    async def flush(self, db):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class PartialFlushAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_flush_still_attributes_the_landed_rows(self) -> None:
        rows = [_daily_row("cred-1", "claude-opus-4-8", 10, 2)]
        exc = UsageFlushError({"usage_session": RuntimeError("wedged")}, rows)
        db = AsyncMock()

        with self.assertRaises(UsageFlushError):
            await _flush_usage(_Tracker(exc), db, None)

        db.attribute_oauth_window_usage.assert_awaited_once()
        (deltas,) = db.attribute_oauth_window_usage.await_args.args
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["key_id"], "cred-1")
        self.assertEqual(deltas[0]["model"], "claude-opus-4-8")
        self.assertEqual(deltas[0]["input_tokens"], 10)

    async def test_pair_failure_attributes_nothing(self) -> None:
        exc = UsageFlushError({"usage_daily+usage_bucket": RuntimeError("wedged")}, [])
        db = AsyncMock()

        with self.assertRaises(UsageFlushError):
            await _flush_usage(_Tracker(exc), db, None)

        db.attribute_oauth_window_usage.assert_not_awaited()

    async def test_a_clean_flush_attributes_as_before(self) -> None:
        rows = [_daily_row("cred-1", "claude-opus-4-8", 10, 2)]
        db = AsyncMock()

        self.assertEqual(await _flush_usage(_Tracker(rows), db, None), 1)
        db.attribute_oauth_window_usage.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
