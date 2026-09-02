# tests/test_usage_counter_bigint_postgres.py
"""Postgres regression test: usage counters must be 64-bit.

The `usage_*` counter columns were created as Postgres `INTEGER` (int4, max
2_147_483_647). SQLite's INTEGER is 64-bit, so every sqlite-backed test passed
while production silently walked a long-lived Claude Code session's
`cache_read_tokens` up to the int4 ceiling. Once there, two things broke at
once:

* `upsert_usage_session_batch` -- `cache_read_tokens = usage_session.cache_read_tokens
  + excluded.cache_read_tokens` overflowed, raising `NumericValueOutOfRange`.
  That aborts `UsageTracker.flush` *after* it has already swapped its buffers
  and written `usage_daily`/`usage_kind_daily`, so `usage_key_hourly` -- the
  source the per-key spend limiter re-seeds from -- silently stopped being
  written.
* `query_top_sessions` -- its ranking subquery adds four int4 columns per row
  (`input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens`)
  *before* SUM() promotes anything to bigint, so `GET /api/sessions` answered
  500 on every call.

Migration `0012_usage_counters_bigint` widens all four usage tables' counters
to BIGINT. These tests only mean something on the Postgres backend and skip
gracefully when no local Postgres is configured.
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

_INT4_MAX = 2_147_483_647

# Fixed (not random) so a failure is easy to re-query by hand. usage_session
# isn't in SNAPSHOT_TABLE_SPECS, so connect_test_database's replace_snapshot({})
# won't clear these for us -- the tests delete their own rows either side.
_SESSION_ID = "sess-bigint-counter-regression"
_PROXY_KEY = "sp-bigint-counter-regression"

_COUNTER_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "web_search_requests",
    "requests",
)
_COUNTER_TABLES = (
    "usage_daily",
    "usage_kind_daily",
    "usage_session",
    "usage_key_hourly",
)


class UsageCounterBigintTests(unittest.TestCase):
    def test_session_counter_accumulates_past_int4_ceiling(self) -> None:
        """A session at the int4 ceiling must keep accepting usage.

        Reproduces the production break: one session's cache_read_tokens sat at
        2_147_293_013 and the next request's ~200k cache-read delta overflowed
        the ON CONFLICT ... DO UPDATE sum.
        """

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    if db._backend != "postgres":
                        self.skipTest(
                            "no local Postgres configured (TEST_DATABASE_URL unset) "
                            "-- int4 overflow can only be exercised on Postgres, "
                            "sqlite INTEGER is already 64-bit"
                        )
                    await self._delete_session(db)

                    # Seed just under the int4 ceiling, the way production got
                    # there: many flushes of a long-running session.
                    near_ceiling = _INT4_MAX - 190_634
                    await db.upsert_usage_session_batch([
                        (
                            _SESSION_ID, _PROXY_KEY, "subagent", "anthropic",
                            "claude-opus-5", "2026-08-20", "2026-08-20", "", "",
                            0, 0, near_ceiling, 0, 0, 0, 0, 1,
                        )
                    ])

                    # One more ordinary request whose cache-read delta crosses
                    # the ceiling. This raised NumericValueOutOfRange.
                    await db.upsert_usage_session_batch([
                        (
                            _SESSION_ID, _PROXY_KEY, "subagent", "anthropic",
                            "claude-opus-5", "2026-08-21", "2026-08-21", "", "",
                            0, 0, 200_000, 0, 0, 0, 0, 1,
                        )
                    ])

                    cur = await db.db.execute(
                        """SELECT cache_read_tokens, requests FROM usage_session
                           WHERE session_id = ? AND proxy_key = ?""",
                        (_SESSION_ID, _PROXY_KEY),
                    )
                    row = await cur.fetchone()
                    self.assertIsNotNone(row)
                    self.assertEqual(
                        int(row["cache_read_tokens"]), near_ceiling + 200_000
                    )
                    self.assertGreater(int(row["cache_read_tokens"]), _INT4_MAX)
                    self.assertEqual(int(row["requests"]), 2)
                finally:
                    await self._cleanup(db)

        asyncio.run(run())

    def test_query_top_sessions_ranks_a_session_past_the_int4_ceiling(self) -> None:
        """The ranking subquery must not overflow adding four counters per row."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    if db._backend != "postgres":
                        self.skipTest(
                            "no local Postgres configured (TEST_DATABASE_URL unset) "
                            "-- int4 overflow can only be exercised on Postgres"
                        )
                    await self._delete_session(db)

                    # A single row whose four ranked counters sum past int4 max
                    # without any one column doing so on its own -- exactly the
                    # `SUM(a + b + c + d)` overflow, not a column overflow.
                    await db.upsert_usage_session_batch([
                        (
                            _SESSION_ID, _PROXY_KEY, "subagent", "anthropic",
                            "claude-opus-5", "2026-08-21", "2026-08-21", "", "",
                            600_000_000, 600_000_000, 600_000_000, 600_000_000,
                            0, 0, 0, 1,
                        )
                    ])

                    rows = await db.query_top_sessions(50)
                    mine = [r for r in rows if r["session_id"] == _SESSION_ID]
                    self.assertEqual(len(mine), 1)
                    self.assertEqual(int(mine[0]["cache_read_tokens"]), 600_000_000)
                finally:
                    await self._cleanup(db)

        asyncio.run(run())

    def test_every_usage_counter_column_is_bigint(self) -> None:
        """Guard the invariant across all four tables, not just the one that broke."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    if db._backend != "postgres":
                        self.skipTest("no local Postgres configured (TEST_DATABASE_URL unset)")

                    cur = await db.db.execute(
                        """SELECT table_name, column_name, data_type
                           FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = ANY(?) AND column_name = ANY(?)""",
                        (list(_COUNTER_TABLES), list(_COUNTER_COLUMNS)),
                    )
                    found = {
                        (r["table_name"], r["column_name"]): r["data_type"]
                        for r in await cur.fetchall()
                    }
                    for table in _COUNTER_TABLES:
                        for column in _COUNTER_COLUMNS:
                            with self.subTest(table=table, column=column):
                                self.assertEqual(
                                    found.get((table, column)),
                                    "bigint",
                                    f"{table}.{column} must be BIGINT; a 32-bit "
                                    f"counter overflows on a busy key or session",
                                )
                finally:
                    await db.close()

        asyncio.run(run())

    # ------------------------------------------------------------------

    async def _delete_session(self, db: object) -> None:
        await db.db.execute(
            "DELETE FROM usage_session WHERE session_id = ?", (_SESSION_ID,)
        )
        await db.db.commit()

    async def _cleanup(self, db: object) -> None:
        if db._backend == "postgres":
            await self._delete_session(db)
        await db.close()


if __name__ == "__main__":
    unittest.main()
