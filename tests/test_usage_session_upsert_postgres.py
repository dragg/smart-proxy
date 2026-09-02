# tests/test_usage_session_upsert_postgres.py
"""Postgres-backend regression test for `upsert_usage_session_batch`.

`build_usage_session_upsert_sql` (src/smart_proxy/db.py) uses LEAST()/GREATEST()
on the postgres backend and MIN()/MAX() on sqlite for the `ON CONFLICT ...
DO UPDATE` merge of `first_date`/`last_date`. Postgres has no 2-arg scalar
MIN()/MAX() (only the aggregate forms), so the old code raised
`UndefinedFunction` on every second upsert to an already-seen session. Every
other test that touches `upsert_usage_session_batch` connects with
`database_url=""` (sqlite only) and never exercises this path.

This test uses `tests.db_test_utils.connect_test_database`, which connects to
a real local Postgres (taken from `TEST_DATABASE_URL`) when one is configured, mirroring the pattern in
`tests/test_anthropic_db_contract.py`. If no Postgres is reachable it skips
gracefully instead of hanging or hard-failing.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.db_test_utils import connect_test_database

# Fixed (not random) so a failure is easy to re-query by hand, and cleaned up
# both before and after so repeated runs don't accumulate cruft in the shared
# Postgres test DB (usage_session isn't part of SNAPSHOT_TABLE_SPECS, so
# connect_test_database's replace_snapshot({}) doesn't clear it for us).
_SESSION_ID = "sess-pg-least-greatest-regression"
_PROXY_KEY = "sp-pg-least-greatest-regression"


class UsageSessionPostgresUpsertTests(unittest.TestCase):
    def test_double_upsert_merges_first_last_date_and_sums_counters_on_postgres(
        self,
    ) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    if db._backend != "postgres":
                        self.skipTest(
                            "no local Postgres configured (TEST_DATABASE_URL unset) "
                            "-- this test needs the real Postgres ON CONFLICT "
                            "path to exercise LEAST()/GREATEST()"
                        )

                    await db.db.execute(
                        "DELETE FROM usage_session WHERE session_id = ?",
                        (_SESSION_ID,),
                    )
                    await db.db.commit()

                    # First upsert: a session first observed late in the
                    # month, with a small amount of usage.
                    await db.upsert_usage_session_batch([
                        (
                            _SESSION_ID, _PROXY_KEY, "unknown", "anthropic", "m",
                            "2026-07-20", "2026-07-20", "", "",
                            10, 0, 0, 0, 0, 0, 0, 1,
                        )
                    ])
                    # Second upsert to the SAME (session_id, proxy_key,
                    # request_kind, provider, model): usage that actually
                    # started earlier in the month arrives in a later flush
                    # (out-of-order batches are the normal steady-state
                    # case). This is the ON CONFLICT ... DO UPDATE path that
                    # crashed with `UndefinedFunction: min(text, text)` on
                    # Postgres before the LEAST/GREATEST fix.
                    await db.upsert_usage_session_batch([
                        (
                            _SESSION_ID, _PROXY_KEY, "unknown", "anthropic", "m",
                            "2026-07-05", "2026-07-05", "", "",
                            7, 0, 0, 0, 0, 0, 0, 2,
                        )
                    ])

                    cur = await db.db.execute(
                        """SELECT first_date, last_date, input_tokens, requests
                           FROM usage_session
                           WHERE session_id = ? AND proxy_key = ?
                             AND provider = ? AND model = ?""",
                        (_SESSION_ID, _PROXY_KEY, "anthropic", "m"),
                    )
                    row = await cur.fetchone()
                    self.assertIsNotNone(row)

                    # LEAST: first_date must move DOWN to the earlier date
                    # even though it arrived in the second upsert.
                    self.assertEqual(row["first_date"], "2026-07-05")
                    # GREATEST: last_date must stay at the later date already
                    # recorded by the first upsert.
                    self.assertEqual(row["last_date"], "2026-07-20")
                    # Counters are additive regardless of date ordering.
                    self.assertEqual(int(row["input_tokens"]), 17)
                    self.assertEqual(int(row["requests"]), 3)
                finally:
                    if db._backend == "postgres":
                        await db.db.execute(
                            "DELETE FROM usage_session WHERE session_id = ?",
                            (_SESSION_ID,),
                        )
                        await db.db.commit()
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
