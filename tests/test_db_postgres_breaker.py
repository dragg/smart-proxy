# tests/test_db_postgres_breaker.py
"""The DB connection must fail fast and heal itself.

The proxy holds ONE psycopg connection for the whole process
(`db_postgres.py`), created once at startup, with no reconnect anywhere. A
Postgres restart, a network blip, an idle timeout or somebody's
`pg_terminate_backend` therefore killed every database operation for the
remaining life of the process — until systemd was told to restart the service
by hand.

The breaker turns that permanent outage into a temporary one:

* a connection-level death opens it once, raising `DbUnavailable` instead of a
  driver error, so best-effort call sites keep the proxy serving;
* while open, operations fail in microseconds rather than eating a TCP timeout
  each;
* every `PROBE_INTERVAL` one operation is allowed to attempt a reconnect, and a
  success closes the breaker and reports the outage duration.

Two traps are pinned here because both are silent when wrong: classifying by
exception type would open the breaker on a merely cancelled statement
(`QueryCanceled` is an `OperationalError` subclass), and blind-retrying the
failed statement after a reconnect would re-run non-idempotent work.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import psycopg

from smart_proxy.db import DbUnavailable
from smart_proxy.db_postgres import _PROBE_INTERVAL, _PostgresConnectionAdapter


class _FakeConn:
    """Scriptable stand-in for psycopg.AsyncConnection."""

    def __init__(self, *, fail_with: Exception | None = None,
                 closed: bool = False, broken: bool = False) -> None:
        self.fail_with = fail_with
        self.closed = closed
        self.broken = broken
        self.executes = 0
        self.rollbacks = 0
        self.rollback_raises: Exception | None = None
        self.breaks_on_rollback = False

    async def execute(self, sql, params=None):  # noqa: ANN001
        self.executes += 1
        if self.fail_with is not None:
            raise self.fail_with
        return f"cursor:{sql}"

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.breaks_on_rollback:
            self.broken = True
        if self.rollback_raises is not None:
            raise self.rollback_raises

    async def commit(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _adapter(conn, reconnect=None, events=None):  # noqa: ANN001
    def on_state_change(state, exc, outage_seconds):  # noqa: ANN001
        if events is not None:
            events.append((state, outage_seconds))

    return _PostgresConnectionAdapter(
        conn, reconnect=reconnect, on_state_change=on_state_change
    )


class BreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_death_opens_the_breaker_and_raises_dbunavailable(self) -> None:
        conn = _FakeConn(fail_with=psycopg.OperationalError("server closed"), broken=True)
        events: list = []
        adapter = _adapter(conn, events=events)

        with self.assertRaises(DbUnavailable):
            await adapter.execute("SELECT 1")

        self.assertFalse(adapter.is_available())
        self.assertEqual([e[0] for e in events], ["open"])

    async def test_while_open_operations_fail_without_touching_the_connection(self) -> None:
        """The point of the breaker: no socket I/O and no timeout per operation."""
        conn = _FakeConn(fail_with=psycopg.OperationalError("server closed"), broken=True)
        adapter = _adapter(conn)
        with self.assertRaises(DbUnavailable):
            await adapter.execute("SELECT 1")
        executes_after_open = conn.executes

        for _ in range(5):
            with self.assertRaises(DbUnavailable):
                await adapter.execute("SELECT 1")

        self.assertEqual(conn.executes, executes_after_open)

    async def test_a_successful_probe_closes_the_breaker_and_reports_the_outage(self) -> None:
        dead = _FakeConn(fail_with=psycopg.OperationalError("server closed"), broken=True)
        healthy = _FakeConn()
        events: list = []

        async def reconnect():
            return healthy

        adapter = _adapter(dead, reconnect=reconnect, events=events)
        with self.assertRaises(DbUnavailable):
            await adapter.execute("SELECT 1")

        # Pretend the probe interval elapsed rather than sleeping through it.
        adapter._last_probe -= _PROBE_INTERVAL + 1
        result = await adapter.execute("SELECT 1")

        self.assertEqual(result, "cursor:SELECT 1")
        self.assertTrue(adapter.is_available())
        self.assertEqual([e[0] for e in events], ["open", "closed"])
        self.assertIsNotNone(events[1][1])          # outage duration reported

    async def test_a_failed_probe_keeps_the_breaker_open_without_realerting(self) -> None:
        dead = _FakeConn(fail_with=psycopg.OperationalError("server closed"), broken=True)
        events: list = []

        async def reconnect():
            raise psycopg.OperationalError("still down")

        adapter = _adapter(dead, reconnect=reconnect, events=events)
        with self.assertRaises(DbUnavailable):
            await adapter.execute("SELECT 1")
        adapter._last_probe -= _PROBE_INTERVAL + 1
        with self.assertRaises(DbUnavailable):
            await adapter.execute("SELECT 1")

        self.assertFalse(adapter.is_available())
        self.assertEqual([e[0] for e in events], ["open"])   # opened once, not twice

    async def test_a_cancelled_statement_is_not_a_dead_connection(self) -> None:
        """Trap #1: QueryCanceled is an OperationalError but the socket is fine.

        Classifying by exception type would mute a perfectly healthy database.
        """
        conn = _FakeConn(fail_with=psycopg.errors.QueryCanceled("statement timeout"))
        self.assertIsInstance(conn.fail_with, psycopg.OperationalError)
        adapter = _adapter(conn)

        with self.assertRaises(psycopg.errors.QueryCanceled):
            await adapter.execute("SELECT pg_sleep(60)")

        self.assertTrue(adapter.is_available())
        self.assertEqual(conn.rollbacks, 1)

    async def test_a_rollback_that_kills_the_connection_opens_the_breaker(self) -> None:
        """A rollback that cannot complete is itself evidence the socket died."""
        conn = _FakeConn(fail_with=psycopg.errors.UniqueViolation("dup"))
        conn.breaks_on_rollback = True
        adapter = _adapter(conn)

        with self.assertRaises(DbUnavailable):
            await adapter.execute("INSERT ...")

        self.assertFalse(adapter.is_available())

    async def test_concurrent_operations_attempt_exactly_one_reconnect(self) -> None:
        dead = _FakeConn(fail_with=psycopg.OperationalError("server closed"), broken=True)
        healthy = _FakeConn()
        attempts = 0

        async def reconnect():
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0)      # let the other waiters pile up on the lock
            return healthy

        adapter = _adapter(dead, reconnect=reconnect)
        with self.assertRaises(DbUnavailable):
            await adapter.execute("SELECT 1")
        adapter._last_probe -= _PROBE_INTERVAL + 1

        results = await asyncio.gather(
            *[adapter.execute("SELECT 1") for _ in range(8)],
            return_exceptions=True,
        )

        self.assertEqual(attempts, 1)
        self.assertTrue(any(r == "cursor:SELECT 1" for r in results))

    async def test_the_failed_statement_is_never_replayed_after_a_reconnect(self) -> None:
        """Trap #2: the dead statement may have committed server-side already."""
        dead = _FakeConn(fail_with=psycopg.OperationalError("server closed"), broken=True)
        healthy = _FakeConn()

        async def reconnect():
            return healthy

        adapter = _adapter(dead, reconnect=reconnect)
        with self.assertRaises(DbUnavailable):
            await adapter.executemany("INSERT INTO t VALUES (?)", [(1,)])

        # Reconnect happens on the NEXT operation, and only that one runs.
        adapter._last_probe -= _PROBE_INTERVAL + 1
        await adapter.execute("SELECT 1")
        self.assertEqual(healthy.executes, 1)


class BreakerAgainstRealPostgresTests(unittest.TestCase):
    """The fakes pin the logic; this pins that psycopg behaves as assumed.

    Specifically that a terminated backend actually sets `closed`/`broken` —
    the whole classifier rests on it — and that a reconnect restores dict rows.
    """

    def test_a_killed_backend_recovers_on_the_next_probe(self) -> None:
        from tests.db_test_utils import get_test_database_url

        dsn = get_test_database_url()
        if not dsn:
            self.skipTest("no local Postgres configured (TEST_DATABASE_URL unset)")

        async def run() -> None:
            import psycopg
            from smart_proxy.db_postgres import PostgresDatabase

            db = PostgresDatabase(dsn)
            await db.connect()
            try:
                cur = await db.db.execute("SELECT pg_backend_pid() AS pid")
                row = await cur.fetchone()
                pid = row["pid"]
                self.assertTrue(db.is_available())

                # Kill this connection's backend from a separate one.
                async with await psycopg.AsyncConnection.connect(dsn) as killer:
                    await killer.execute(
                        "SELECT pg_terminate_backend(%s)", (pid,)
                    )

                with self.assertRaises(DbUnavailable):
                    await db.db.execute("SELECT 1")
                self.assertFalse(db.is_available())

                # Fail fast while open, then heal on the probe.
                with self.assertRaises(DbUnavailable):
                    await db.db.execute("SELECT 1")
                db.db._last_probe -= _PROBE_INTERVAL + 1

                cur = await db.db.execute("SELECT 42 AS answer")
                row = await cur.fetchone()
                self.assertEqual(row["answer"], 42)      # dict rows survived
                self.assertTrue(db.is_available())
            finally:
                await db.close()

        asyncio.run(run())



if __name__ == "__main__":
    unittest.main()
