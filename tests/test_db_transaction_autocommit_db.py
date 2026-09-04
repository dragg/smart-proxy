# tests/test_db_transaction_autocommit_db.py
"""One shared implicit transaction could silently discard another task's work.

`autocommit` was never set, so psycopg opened one implicit transaction that the
whole process shared, and the adapter rolled it back after *any* failed
statement. That rollback discarded whatever other coroutines had written and not
yet committed — and the victim never learned about it:

    task A: update_anthropic_oauth_tokens  -- snapshot, UPDATE token, event,
                                           -- then one commit() at the end
    task B: any failing query              -- its rollback discards A's UPDATE
    task A: commit()                       -- commits an empty transaction, no error

The row then keeps the retired refresh token, and the key bricks on the next
restart. That is a permanent key loss reachable with a perfectly healthy
database, which is why this is not cleanliness work.

With autocommit on, each statement stands alone and multi-statement units take
an explicit transaction guarded by a process-wide lock — a shared connection
cannot isolate concurrent transactions, so they must be serialised.
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


class TransactionIsolationTests(unittest.TestCase):
    def _skip_without_postgres(self) -> None:
        if not get_test_database_url():
            self.skipTest(
                "no local Postgres configured (TEST_DATABASE_URL unset) — this "
                "pins shared-connection transaction behaviour, which sqlite "
                "does not exhibit"
            )

    def test_a_failing_query_cannot_discard_another_tasks_write(self) -> None:
        """The regression test for the silent brick vector."""
        self._skip_without_postgres()

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "t.db")
                )
                if db._backend != "postgres":
                    self.skipTest("needs the Postgres backend")
                try:
                    await db.insert_anthropic_key(
                        id="key-tx", key_type="oauth", access_token="a0",
                        refresh_token="r0", client_id="c",
                        expires_at=1_700_000_000_000, scopes='["user:inference"]',
                        name="tx",
                    )

                    async def writer() -> None:
                        await db.update_anthropic_oauth_tokens(
                            "key-tx", "a1", 1_800_000_000_000, "r1",
                            audit_source="test", audit_event_type="refresh_succeeded",
                            audit_decision="update_tokens",
                        )

                    async def breaker() -> None:
                        await asyncio.sleep(0)
                        try:
                            await db.db.execute("SELECT * FROM does_not_exist")
                        except Exception:
                            pass

                    await asyncio.gather(writer(), breaker())

                    row = await db.get_anthropic_key("key-tx")
                    assert row is not None
                    # The rotated token must survive the other task's failure.
                    self.assertEqual(row["refresh_token"], "r1")
                    self.assertEqual(row["expires_at"], 1_800_000_000_000)
                finally:
                    for table, col in (
                        ("anthropic_key_events", "key_id"),
                        ("anthropic_key_snapshots", "key_id"),
                        ("anthropic_keys", "id"),
                    ):
                        await db.db.execute(
                            f"DELETE FROM {table} WHERE {col} = ?", ("key-tx",)
                        )
                    await db.db.commit()
                    await db.close()

        asyncio.run(run())

    def test_a_failed_statement_does_not_poison_the_next_one(self) -> None:
        self._skip_without_postgres()

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "t.db")
                )
                if db._backend != "postgres":
                    self.skipTest("needs the Postgres backend")
                try:
                    with self.assertRaises(Exception):
                        await db.db.execute("SELECT * FROM does_not_exist")
                    cur = await db.db.execute("SELECT 7 AS n")
                    row = await cur.fetchone()
                    self.assertEqual(row["n"], 7)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_a_transaction_is_reentrant_for_the_owning_task(self) -> None:
        """db.py methods run `execute` inside `transaction()` — that must not deadlock."""
        self._skip_without_postgres()

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "t.db")
                )
                try:
                    async with db.transaction():
                        cur = await db.db.execute("SELECT 1 AS n")
                        row = await cur.fetchone()
                        self.assertEqual(row["n"], 1)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_concurrent_transactions_serialise(self) -> None:
        self._skip_without_postgres()

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "t.db")
                )
                order: list[str] = []
                try:
                    async def block(tag: str) -> None:
                        async with db.transaction():
                            order.append(f"{tag}-in")
                            await asyncio.sleep(0)
                            order.append(f"{tag}-out")

                    await asyncio.gather(block("a"), block("b"))
                    # No interleaving: each block finishes before the next starts.
                    self.assertIn(order, (
                        ["a-in", "a-out", "b-in", "b-out"],
                        ["b-in", "b-out", "a-in", "a-out"],
                    ))
                finally:
                    await db.close()

        asyncio.run(run())


class SqliteTransactionRollbackTests(unittest.TestCase):
    """`transaction()` promises "commit or roll back together" — sqlite must keep it.

    sqlite opens an implicit transaction on the first DML. Without an explicit
    rollback, an exception inside the block leaves that transaction open, and
    the *next* unrelated `commit()` commits the half-written work. That breaks
    the usage_daily/usage_bucket pair, whose whole point is that the two tables
    cannot disagree.
    """

    def _sqlite_db(self, td: str):
        from smart_proxy.db import build_database_from_config
        return build_database_from_config(
            database_url="", db_path=str(Path(td) / "t.db")
        )

    def test_transaction_rolls_back_on_exception(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = self._sqlite_db(td)
                await db.connect()
                try:
                    with self.assertRaises(RuntimeError):
                        async with db.transaction():
                            await db.db.execute(
                                "INSERT INTO proxy_api_keys (key, name, active, created_at)"
                                " VALUES ('sp-rollback', 'x', 1, '2026-09-04')"
                            )
                            raise RuntimeError("boom")

                    cur = await db.db.execute(
                        "SELECT COUNT(*) AS n FROM proxy_api_keys WHERE key = 'sp-rollback'"
                    )
                    self.assertEqual((await cur.fetchone())["n"], 0)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_rolled_back_work_does_not_leak_into_the_next_commit(self) -> None:
        """The regression the rollback exists to prevent."""
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = self._sqlite_db(td)
                await db.connect()
                try:
                    with self.assertRaises(RuntimeError):
                        async with db.transaction():
                            await db.db.execute(
                                "INSERT INTO proxy_api_keys (key, name, active, created_at)"
                                " VALUES ('sp-leak', 'x', 1, '2026-09-04')"
                            )
                            raise RuntimeError("boom")

                    # An unrelated, successful unit of work.
                    async with db.transaction():
                        await db.db.execute(
                            "INSERT INTO proxy_api_keys (key, name, active, created_at)"
                            " VALUES ('sp-ok', 'y', 1, '2026-09-04')"
                        )
                        await db.db.commit()

                    cur = await db.db.execute(
                        "SELECT key FROM proxy_api_keys ORDER BY key"
                    )
                    keys = [row["key"] for row in await cur.fetchall()]
                    self.assertEqual(keys, ["sp-ok"])
                finally:
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
