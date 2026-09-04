# tests/test_usage_bucket_db.py
"""`usage_bucket` — hour-grain usage with the full `usage_daily` dimension set.

The table exists so the dashboard can answer "who spent what between 10:00 and
14:00" instead of only "on this date". `usage_daily` is a projection of it
(`GROUP BY substr(hour_utc, 1, 10)` with `request_kind` summed away), which is
only true if the two are written together — see
`test_usage_tracker_bucket.py` for that half. This module pins the storage
layer: the upsert's accumulate/COALESCE semantics and the three read shapes.

Runs on Postgres too when TEST_DATABASE_URL is set.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.db_test_utils import connect_test_database, get_test_database_url


def _row(
    hour: str,
    *,
    proxy_key: str = "sp-a",
    group_name: str | None = "acct-a",
    credential_id: str = "cred-1",
    provider: str = "anthropic",
    model: str = "claude-opus-4-8",
    compat: int = 0,
    kind: str = "main",
    counters: tuple[int, ...] = (10, 20, 1, 2, 3, 4, 5, 1),
) -> tuple:
    """One `usage_bucket` row in the canonical 16-column order."""
    return (
        hour, proxy_key, group_name, credential_id, provider, model,
        compat, kind, *counters,
    )


class UsageBucketDbTests(unittest.TestCase):
    def _run(self, coro_factory) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "t.db")
                )
                try:
                    await coro_factory(db)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_upsert_sums_counters_and_coalesces_group(self) -> None:
        """Same PK twice accumulates; a NULL group_name never erases a name."""

        async def body(db) -> None:
            await db.upsert_usage_bucket_batch([_row("2026-09-05T10")])
            await db.upsert_usage_bucket_batch(
                [_row("2026-09-05T10", group_name=None)]
            )

            cur = await db.db.execute(
                "SELECT group_name, input_tokens, output_tokens, requests"
                " FROM usage_bucket WHERE hour_utc = '2026-09-05T10'"
            )
            rows = [dict(r) for r in await cur.fetchall()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["group_name"], "acct-a")
            self.assertEqual(rows[0]["input_tokens"], 20)
            self.assertEqual(rows[0]["output_tokens"], 40)
            self.assertEqual(rows[0]["requests"], 2)

        self._run(body)

    def test_a_rename_inside_one_batch_lands(self) -> None:
        """executemany issues one statement per row, so two rows differing only
        in group_name (an Anthropic key renamed mid-interval) must not trip
        Postgres's "cannot affect row a second time"."""

        async def body(db) -> None:
            await db.upsert_usage_bucket_batch(
                [
                    _row("2026-09-05T10", group_name="old-name"),
                    _row("2026-09-05T10", group_name="new-name"),
                ]
            )
            cur = await db.db.execute(
                "SELECT group_name, requests FROM usage_bucket"
            )
            rows = [dict(r) for r in await cur.fetchall()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["group_name"], "new-name")
            self.assertEqual(rows[0]["requests"], 2)

        self._run(body)

    def test_query_by_key_model_is_inclusive_and_collapses_kinds(self) -> None:
        """Both bounds inclusive; kinds summed away; key_name joined in."""

        async def body(db) -> None:
            await db.add_proxy_key("sp-a", "Alice")
            await db.upsert_usage_bucket_batch(
                [
                    _row("2026-09-05T09"),                      # before range
                    _row("2026-09-05T10", kind="main"),         # in range
                    _row("2026-09-05T10", kind="subagent"),     # same, other kind
                    _row("2026-09-05T14"),                      # last bucket, in range
                    _row("2026-09-05T15"),                      # after range
                ]
            )

            rows = await db.query_usage_bucket_by_key_model(
                "2026-09-05T10", "2026-09-05T14"
            )
            self.assertEqual(len(rows), 1, "kinds must collapse into one row")
            row = rows[0]
            self.assertEqual(row["key_name"], "Alice")
            self.assertEqual(row["proxy_key"], "sp-a")
            self.assertEqual(row["model"], "claude-opus-4-8")
            # Three in-range records of 10 input each; T09 and T15 excluded.
            self.assertEqual(row["input_tokens"], 30)
            self.assertEqual(row["requests"], 3)

        self._run(body)

    def test_query_by_key_model_splits_on_compat_and_credential(self) -> None:
        """The dimensions usage_key_hourly could not carry are preserved."""

        async def body(db) -> None:
            await db.upsert_usage_bucket_batch(
                [
                    _row("2026-09-05T10", credential_id="cred-1", group_name="acct-a"),
                    _row("2026-09-05T10", credential_id="cred-2", group_name="acct-b"),
                    _row("2026-09-05T10", compat=1),
                ]
            )
            rows = await db.query_usage_bucket_by_key_model(
                "2026-09-05T10", "2026-09-05T10"
            )
            # cred-1 native, cred-2 native, cred-1 compat.
            self.assertEqual(len(rows), 3)
            self.assertEqual(
                sorted((r["group_name"], r["via_openai_compat"]) for r in rows),
                [("acct-a", 0), ("acct-a", 1), ("acct-b", 0)],
            )

        self._run(body)

    def test_query_series_is_per_hour_and_per_model(self) -> None:
        async def body(db) -> None:
            await db.upsert_usage_bucket_batch(
                [
                    _row("2026-09-05T10", model="claude-opus-4-8"),
                    _row("2026-09-05T10", model="claude-haiku-4-5"),
                    _row("2026-09-05T11", model="claude-opus-4-8"),
                    _row("2026-09-05T12", model="claude-opus-4-8"),
                ]
            )
            rows = await db.query_usage_bucket_series(
                "2026-09-05T10", "2026-09-05T11"
            )
            self.assertEqual(
                [(r["hour_utc"], r["model"]) for r in rows],
                [
                    ("2026-09-05T10", "claude-haiku-4-5"),
                    ("2026-09-05T10", "claude-opus-4-8"),
                    ("2026-09-05T11", "claude-opus-4-8"),
                ],
                "ordered by hour then model; T12 is outside the range",
            )

        self._run(body)

    def test_query_by_kind_keeps_kinds(self) -> None:
        async def body(db) -> None:
            await db.add_proxy_key("sp-a", "Alice")
            await db.upsert_usage_bucket_batch(
                [
                    _row("2026-09-05T10", kind="main"),
                    _row("2026-09-05T10", kind="subagent"),
                    _row("2026-09-05T10", kind="subagent"),
                ]
            )
            rows = await db.query_usage_bucket_by_kind(
                "2026-09-05T10", "2026-09-05T10"
            )
            by_kind = {r["request_kind"]: r for r in rows}
            self.assertEqual(set(by_kind), {"main", "subagent"})
            self.assertEqual(by_kind["main"]["requests"], 1)
            self.assertEqual(by_kind["subagent"]["requests"], 2)
            self.assertEqual(by_kind["main"]["key_name"], "Alice")

        self._run(body)

    def test_min_hour_is_none_when_empty_then_the_earliest_label(self) -> None:
        async def body(db) -> None:
            self.assertIsNone(await db.min_usage_bucket_hour())
            await db.upsert_usage_bucket_batch(
                [_row("2026-09-05T14"), _row("2026-09-04T18"), _row("2026-09-06T01")]
            )
            self.assertEqual(await db.min_usage_bucket_hour(), "2026-09-04T18")

        self._run(body)

    def test_empty_batch_is_a_noop(self) -> None:
        async def body(db) -> None:
            await db.upsert_usage_bucket_batch([])
            self.assertIsNone(await db.min_usage_bucket_hour())

        self._run(body)


class UsageBucketMigrationTests(unittest.TestCase):
    """`0014_usage_bucket` on real Postgres. sqlite gets the table from
    SCHEMA_SQL on every connect(), so there is nothing to migrate there."""

    def test_0014_creates_the_table_and_is_idempotent(self) -> None:
        url = get_test_database_url()
        if not url:
            self.skipTest("no local Postgres configured (TEST_DATABASE_URL unset)")

        async def run() -> None:
            from smart_proxy.db_migrations import run_postgres_migrations

            await run_postgres_migrations(url)
            # A second pass must be a no-op, not an error.
            applied_again = await run_postgres_migrations(url)
            self.assertNotIn("0014_usage_bucket", applied_again)

            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "unused.db")
                )
                try:
                    cur = await db.db.execute(
                        """SELECT column_name FROM information_schema.key_column_usage
                           WHERE table_name = 'usage_bucket'
                           ORDER BY ordinal_position"""
                    )
                    pk = [r["column_name"] for r in await cur.fetchall()]
                    self.assertEqual(
                        pk,
                        ["hour_utc", "proxy_key", "credential_id", "provider",
                         "model", "via_openai_compat", "request_kind"],
                    )
                finally:
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
