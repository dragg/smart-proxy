from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from collections.abc import Sequence
from typing import Any

from smart_proxy.db import Database, DbUnavailable

logger = logging.getLogger(__name__)

# How long the breaker waits between reconnect attempts. The database is local
# (127.0.0.1 in production), so a restart is measured in seconds; probing every
# 5s detects recovery quickly without hammering a server that is still starting.
_PROBE_INTERVAL = 5.0

# Only applied when the DSN does not set one itself. Without it the operation
# that discovers a dead database waits out the OS-level TCP timeout; everything
# after breaker-open fails in microseconds regardless.
_CONNECT_TIMEOUT_SECONDS = 5


def _translate_sql(sql: str) -> str:
    return sql.replace("?", "%s")


class _PostgresConnectionAdapter:
    """One psycopg connection, guarded by a connection breaker.

    The whole process shares this single connection, so its death used to be
    permanent: nothing reconnected, and every database operation failed for the
    remaining life of the process. The breaker turns that into a temporary
    outage — fail fast while it is down, reconnect on a timer, and let the
    proxy's best-effort call sites keep serving in the meantime.

    Two states. CLOSED is normal. OPEN means the connection is known dead:
    operations raise ``DbUnavailable`` without touching the socket, except one
    every ``_PROBE_INTERVAL`` which attempts a reconnect (the half-open probe)
    and closes the breaker on success.
    """

    def __init__(
        self,
        conn: Any,
        *,
        reconnect: Any | None = None,
        on_state_change: Any | None = None,
    ) -> None:
        self._conn = conn
        self._reconnect = reconnect
        self.on_state_change = on_state_change
        self._open_since: float | None = None   # None == CLOSED
        self._last_probe = 0.0
        self._lock = asyncio.Lock()
        # One connection cannot isolate concurrent transactions: a bare
        # statement issued by another task while a transaction is open simply
        # joins it. So every operation serialises on this lock, and the task
        # holding it re-enters freely.
        self._tx_lock = asyncio.Lock()
        self._tx_owner: object | None = None

    # -- breaker ------------------------------------------------------------

    def is_available(self) -> bool:
        return self._open_since is None

    def _connection_is_dead(self) -> bool:
        """Classify by connection STATE, never by exception type.

        `QueryCanceled` (statement timeout, admin cancel) and `AdminShutdown`
        are both `OperationalError` subclasses, so a type-based check would open
        the breaker on a merely cancelled statement and mute a healthy database.
        """
        return bool(getattr(self._conn, "closed", False)
                    or getattr(self._conn, "broken", False))

    def _open_breaker(self, exc: BaseException | None) -> None:
        if self._open_since is not None:
            return
        now = time.monotonic()
        self._open_since = now
        self._last_probe = now      # first probe waits a full interval
        logger.error("PostgreSQL connection lost; failing fast until it returns: %s", exc)
        self._emit_state("open", exc, None)

    def _close_breaker(self) -> None:
        outage = time.monotonic() - (self._open_since or time.monotonic())
        self._open_since = None
        logger.info("PostgreSQL connection restored after %.0fs", outage)
        self._emit_state("closed", None, outage)

    def _emit_state(self, state: str, exc: BaseException | None,
                    outage_seconds: float | None) -> None:
        callback = self.on_state_change
        if callback is None:
            return
        try:
            callback(state, exc, outage_seconds)
        except Exception:
            # Reporting a database outage must never become a second failure.
            logger.exception("breaker state callback failed for state=%s", state)

    async def _acquire(self) -> Any:
        """Return a usable connection, or raise DbUnavailable.

        Reconnects happen under a lock so a burst of coroutines hitting an open
        breaker produces one attempt, not a stampede; the losers re-check the
        state after acquiring it.
        """
        if self._open_since is None:
            return self._conn
        async with self._lock:
            if self._open_since is None:
                return self._conn
            now = time.monotonic()
            if now - self._last_probe < _PROBE_INTERVAL or self._reconnect is None:
                raise DbUnavailable("PostgreSQL is unavailable (breaker open)")
            self._last_probe = now
            try:
                self._conn = await self._reconnect()
            except Exception as exc:
                raise DbUnavailable("PostgreSQL is still unavailable") from exc
            self._close_breaker()
            return self._conn

    async def _handle_operation_error(self, exc: BaseException) -> None:
        """Open the breaker on a connection death; otherwise roll back and return.

        Never retries the failed statement: after a connection dies its fate is
        unknown (it may have committed server-side), so the caller's own error
        handling decides what to do with a fresh operation.
        """
        if self._connection_is_dead():
            self._open_breaker(exc)
            raise DbUnavailable("PostgreSQL connection lost") from exc
        if self._holds_tx():
            # Inside an explicit transaction psycopg forbids an explicit
            # rollback: letting the exception leave the block is how the
            # transaction is meant to unwind, and it rolls back for us.
            return
        await self._rollback_on_error()
        if self._connection_is_dead():
            # A rollback that could not complete is itself evidence the socket
            # died. Previously this left a poisoned connection in place.
            self._open_breaker(exc)
            raise DbUnavailable("PostgreSQL connection lost during rollback") from exc

    async def _rollback_on_error(self) -> None:
        """Clear aborted-transaction state so the next query can run."""
        try:
            await self._conn.rollback()
        except Exception:
            logger.exception("PostgreSQL rollback after failed command failed")

    # -- transactions -------------------------------------------------------

    @asynccontextmanager
    async def transaction(self):
        """Group statements so they commit or roll back together."""
        task = asyncio.current_task()
        if self._tx_owner is task:
            yield self          # already inside one — re-entering is a no-op
            return
        async with self._tx_lock:
            self._tx_owner = task
            try:
                conn = await self._acquire()
                async with conn.transaction():
                    yield self
            finally:
                self._tx_owner = None

    def _holds_tx(self) -> bool:
        return self._tx_owner is asyncio.current_task()

    # -- operations ---------------------------------------------------------

    async def execute(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> Any:
        if self._holds_tx():
            return await self._execute(sql, params)
        async with self._tx_lock:
            return await self._execute(sql, params)

    async def _execute(self, sql: str, params: Sequence[Any] | None) -> Any:
        conn = await self._acquire()
        try:
            return await conn.execute(_translate_sql(sql), params or ())
        except DbUnavailable:
            raise
        except Exception as exc:
            await self._handle_operation_error(exc)
            raise

    async def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if self._holds_tx():
            await self._executemany(sql, rows)
            return
        async with self._tx_lock:
            await self._executemany(sql, rows)

    async def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        conn = await self._acquire()
        try:
            async with conn.cursor() as cur:
                await cur.executemany(_translate_sql(sql), rows)
        except DbUnavailable:
            raise
        except Exception as exc:
            await self._handle_operation_error(exc)
            raise

    async def commit(self) -> None:
        """No-op: the connection runs in autocommit, transactions are explicit.

        Kept so the many existing `execute(...) ... commit()` call sites keep
        working unchanged; only the multi-statement units that must be atomic
        were converted to `transaction()`.
        """
        return None

    async def close(self) -> None:
        await self._conn.close()


class PostgresDatabase(Database):
    def __init__(self, database_url: str) -> None:
        super().__init__(database_url)
        self._database_url = database_url
        self._backend = "postgres"
        # Set by the proxy after the notifier exists, so a breaker transition
        # can reach Telegram. The adapter itself has no notifier.
        self.on_db_state_change: Any | None = None

    async def _open_connection(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError(
                "psycopg[binary] is required for PostgreSQL support. "
                "Install project dependencies again after updating pyproject."
            ) from exc

        # autocommit: without it every statement in the process joins ONE
        # implicit transaction on this shared connection, so a rollback after
        # any failed statement silently discards other tasks' uncommitted work.
        kwargs: dict[str, Any] = {"row_factory": dict_row, "autocommit": True}
        if "connect_timeout" not in self._database_url:
            kwargs["connect_timeout"] = _CONNECT_TIMEOUT_SECONDS
        # row_factory must be reapplied on every reconnect: without it every
        # subsequent read silently changes shape from dict to tuple.
        return await psycopg.AsyncConnection.connect(self._database_url, **kwargs)

    async def connect(self) -> None:
        conn = await self._open_connection()
        self._db = _PostgresConnectionAdapter(
            conn,
            reconnect=self._open_connection,
            on_state_change=self._emit_db_state,
        )
        logger.info("PostgreSQL database connected")

    def _emit_db_state(self, state: str, exc: BaseException | None,
                       outage_seconds: float | None) -> None:
        callback = self.on_db_state_change
        if callback is not None:
            callback(state, exc, outage_seconds)

    def is_available(self) -> bool:
        adapter = self._db
        return adapter.is_available() if adapter is not None else False

    def transaction(self):
        return self.db.transaction()

    async def ensure_migration_table(self) -> None:
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        await self.db.commit()

    async def get_applied_migrations(self) -> set[str]:
        cur = await self.db.execute(
            "SELECT name FROM schema_migrations ORDER BY name"
        )
        return {row["name"] for row in await cur.fetchall()}

    async def apply_migration(
        self,
        name: str,
        statements: tuple[str, ...],
    ) -> None:
        # A savepoint needs an enclosing transaction, which autocommit removes.
        # An explicit transaction gives identical semantics with less ceremony.
        async with self.transaction():
            for statement in statements:
                await self.db.execute(statement)
            await self.db.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, datetime.now(timezone.utc).isoformat()),
            )

    async def _after_replace_snapshot(self, snapshot: dict[str, list[dict]]) -> None:
        del snapshot
        for table in (
            "anthropic_key_snapshots",
            "anthropic_key_events",
            "rate_limit_log",
            "oauth_window_log",
            "oauth_window_drop_log",
            "oauth_usage_snapshot",
            "oauth_limit_wipe",
        ):
            await self.db.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence(%s, 'id'),
                    COALESCE((SELECT MAX(id) FROM """ + table + """), 1),
                    EXISTS(SELECT 1 FROM """ + table + """)
                )
                """,
                (table,),
            )

