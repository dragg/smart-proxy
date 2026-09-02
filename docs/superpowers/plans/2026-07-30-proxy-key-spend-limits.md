# Per-Proxy-Key Spend Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each `sp-*` proxy key an optional USD spend cap over a 24h window that resets at local midnight, enforced by the proxy with a 429, and editable from `/_app/#keys` with immediate effect.

**Architecture:** Two new tables — `proxy_key_limits` (config, one row per key+kind) and `usage_key_hourly` (token buckets per hour, written by the existing 60s usage flush). A new `KeyLimiter` object holds limits and live per-key spend in memory, so the request-path check is a dict lookup with no I/O; the DB is the recovery source at startup, window rollover, and reload. Enforcement is a single gate in `_proxy_handler`; OpenAI-compat is covered for free because it loops back through `POST /v1/messages`.

**Tech Stack:** Python 3, aiohttp, aiosqlite + psycopg (dual backend), Svelte 5 (runes), `unittest.IsolatedAsyncioTestCase` + pytest runner.

**Design spec:** `docs/superpowers/specs/2026-07-30-proxy-key-spend-limits-design.md`

## Global Constraints

- **Run tests with:** `.venv/bin/python -m pytest tests/<file> -v` from the repo root.
- **Dual DB backend.** Every schema change lands in **both** `SCHEMA_SQL` in `src/smart_proxy/db.py` (SQLite, executed via `executescript` on every `connect()`, so `CREATE TABLE IF NOT EXISTS` reaches existing DBs) **and** `POSTGRES_MIGRATIONS` in `src/smart_proxy/db_migrations.py` (Postgres does **not** run `SCHEMA_SQL`). SQLite `REAL` → Postgres `DOUBLE PRECISION`.
- **Postgres migration names must be globally unique** in the `POSTGRES_MIGRATIONS` tuple. The tuple has historical duplicate `0002`/`0003` prefixes; the current highest is `0009_anthropic_keys_role`, so this work uses `0010_proxy_key_limits`.
- **SQL placeholders are always `?`.** `_translate_sql` in `src/smart_proxy/db_postgres.py` rewrites them to `%s` for Postgres. Never write `%s` by hand.
- **No new tables in the `MIGRATIONS` list** (`db.py:273`) — that list is `ALTER` statements run inside a swallow-all try/except.
- **User-facing 429 message, verbatim:** `SmartProxy: you have reached your 24h limit. Retry in {time}` — no trailing period; `{time}` comes from the existing `_humanize_seconds`.
- **`/_oauth_usage` limit entry field names, verbatim:** `kind` = `smartproxy_daily_usd`, plus `source`, `limit_usd`, `spent_usd`, `percent`, `resets_at`.
- **Never mutate the `/_oauth_usage` cached payload.** It is shared across all callers for 60s; per-key data must be injected into a copy on every request.
- **Unlimited is the default and needs no data.** Absent row or `amount <= 0` means unlimited, so no backfill for existing keys.
- **Timezone setting:** `ANTHROPIC_PROXY_LIMIT_WINDOW_TZ`, default `Europe/Paris`.
- Commit after each task with the message given in that task's final step.

---

### Task 1: Database layer — two new tables and their accessors

**Files:**
- Modify: `src/smart_proxy/db.py` (`SCHEMA_SQL` ~line 61-270, `_KIND_COUNTERS` block ~line 766, `SNAPSHOT_TABLE_SPECS` ~line 474, `Database` methods near `upsert_usage_kind_batch` ~line 1143 and `get_active_proxy_keys` ~line 1116)
- Modify: `src/smart_proxy/db_migrations.py` (append to `POSTGRES_MIGRATIONS`)
- Test: `tests/test_key_limits_db.py` (create)

**Interfaces:**
- Consumes: nothing — this is the base task.
- Produces, all on `Database`:
  - `async upsert_usage_hourly_batch(rows: list[tuple]) -> None` — each tuple is `(hour_utc, proxy_key, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)`; additive on conflict.
  - `async query_usage_key_hourly(start_hour: str, end_hour: str) -> list[dict]` — rows with keys `proxy_key, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests`; range is `hour_utc >= start_hour AND hour_utc < end_hour`.
  - `async prune_usage_key_hourly(before_hour: str) -> None`
  - `async list_proxy_key_limits() -> list[dict]` — rows with keys `proxy_key, kind, amount`.
  - `async set_proxy_key_limit(proxy_key: str, kind: str, amount: float) -> None`
  - `async delete_proxy_key_limit(proxy_key: str, kind: str) -> None`
  - `async get_proxy_key_by_created_at(created_at: str) -> str | None`
  - Module-level `build_usage_hourly_upsert_sql(backend: str) -> str`
  - Hour-bucket string format is `"%Y-%m-%dT%H"`, e.g. `2026-07-30T14`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_key_limits_db.py`:

```python
# tests/test_key_limits_db.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import Database, build_database_from_config


class HourlyBucketTests(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        return db

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_hourly_upsert_accumulates_per_hour_key_model(self):
        db = await self._db()
        row = ("2026-07-30T14", "sp-a", "claude-opus-4-5", 10, 5, 0, 0, 0, 0, 0, 1)
        await db.upsert_usage_hourly_batch([row])
        await db.upsert_usage_hourly_batch([row])
        rows = await db.query_usage_key_hourly("2026-07-30T00", "2026-07-31T00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["input_tokens"]), 20)
        self.assertEqual(int(rows[0]["output_tokens"]), 10)
        self.assertEqual(int(rows[0]["requests"]), 2)
        self.assertEqual(rows[0]["proxy_key"], "sp-a")
        self.assertEqual(rows[0]["model"], "claude-opus-4-5")

    async def test_query_range_is_start_inclusive_end_exclusive(self):
        db = await self._db()
        await db.upsert_usage_hourly_batch([
            ("2026-07-29T23", "sp-a", "m", 1, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T00", "sp-a", "m", 2, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T23", "sp-a", "m", 4, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-31T00", "sp-a", "m", 8, 0, 0, 0, 0, 0, 0, 1),
        ])
        rows = await db.query_usage_key_hourly("2026-07-30T00", "2026-07-31T00")
        self.assertEqual(int(rows[0]["input_tokens"]), 6)   # 2 + 4 only

    async def test_query_groups_by_key_and_model(self):
        db = await self._db()
        await db.upsert_usage_hourly_batch([
            ("2026-07-30T01", "sp-a", "m1", 1, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T02", "sp-a", "m1", 2, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T02", "sp-a", "m2", 4, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T02", "sp-b", "m1", 8, 0, 0, 0, 0, 0, 0, 1),
        ])
        rows = await db.query_usage_key_hourly("2026-07-30T00", "2026-07-31T00")
        got = {(r["proxy_key"], r["model"]): int(r["input_tokens"]) for r in rows}
        self.assertEqual(got, {("sp-a", "m1"): 3, ("sp-a", "m2"): 4, ("sp-b", "m1"): 8})

    async def test_prune_deletes_only_older_hours(self):
        db = await self._db()
        await db.upsert_usage_hourly_batch([
            ("2026-06-01T00", "sp-a", "m", 1, 0, 0, 0, 0, 0, 0, 1),
            ("2026-07-30T00", "sp-a", "m", 2, 0, 0, 0, 0, 0, 0, 1),
        ])
        await db.prune_usage_key_hourly("2026-07-01T00")
        rows = await db.query_usage_key_hourly("2026-01-01T00", "2027-01-01T00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["input_tokens"]), 2)


class ProxyKeyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        return db

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_set_is_upsert_and_list_returns_rows(self):
        db = await self._db()
        await db.set_proxy_key_limit("sp-a", "daily_usd", 25.0)
        await db.set_proxy_key_limit("sp-a", "daily_usd", 40.0)
        rows = await db.list_proxy_key_limits()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proxy_key"], "sp-a")
        self.assertEqual(rows[0]["kind"], "daily_usd")
        self.assertAlmostEqual(float(rows[0]["amount"]), 40.0)

    async def test_delete_removes_the_row(self):
        db = await self._db()
        await db.set_proxy_key_limit("sp-a", "daily_usd", 25.0)
        await db.delete_proxy_key_limit("sp-a", "daily_usd")
        self.assertEqual(await db.list_proxy_key_limits(), [])

    async def test_delete_is_idempotent(self):
        db = await self._db()
        await db.delete_proxy_key_limit("sp-missing", "daily_usd")
        self.assertEqual(await db.list_proxy_key_limits(), [])

    async def test_get_proxy_key_by_created_at(self):
        db = await self._db()
        await db.add_proxy_key("sp-full-key-value", "laptop")
        rows = await db.list_proxy_keys()
        created_at = rows[0]["created_at"]
        self.assertEqual(await db.get_proxy_key_by_created_at(created_at), "sp-full-key-value")
        self.assertIsNone(await db.get_proxy_key_by_created_at("2000-01-01T00:00:00+00:00"))


class SnapshotRoundTripTests(unittest.IsolatedAsyncioTestCase):
    """`tests/test_db_snapshot_roundtrip.py` seeds specific tables by hand, so a
    new SNAPSHOT_TABLE_SPECS entry with rows in it is only covered here."""

    async def test_new_tables_round_trip_through_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            source = Database(str(Path(td) / "source.db"))
            target = Database(str(Path(td) / "target.db"))
            await source.connect()
            await target.connect()
            try:
                await source.set_proxy_key_limit("sp-a", "daily_usd", 25.0)
                await source.upsert_usage_hourly_batch([
                    ("2026-07-30T14", "sp-a", "claude-opus-4-5", 10, 5, 0, 0, 0, 0, 0, 1),
                ])
                snapshot = await source.export_snapshot()
                self.assertEqual(len(snapshot["proxy_key_limits"]), 1)
                self.assertEqual(len(snapshot["usage_key_hourly"]), 1)
                await target.replace_snapshot(snapshot)
                self.assertEqual(await target.export_snapshot(), snapshot)
            finally:
                await source.close()
                await target.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_key_limits_db.py -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'upsert_usage_hourly_batch'`

- [ ] **Step 3: Add both tables to `SCHEMA_SQL`**

In `src/smart_proxy/db.py`, append to the `SCHEMA_SQL` string (it ends just before `MIGRATIONS = [`; add these right after the `CREATE TABLE IF NOT EXISTS oauth_window_usage_pending (...)` block, keeping the trailing `"""`):

```sql
CREATE TABLE IF NOT EXISTS proxy_key_limits (
    proxy_key  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    amount     REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (proxy_key, kind)
);

CREATE TABLE IF NOT EXISTS usage_key_hourly (
    hour_utc                 TEXT    NOT NULL,
    proxy_key                TEXT    NOT NULL,
    model                    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_utc, proxy_key, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_key_hourly_hour ON usage_key_hourly(hour_utc);
```

- [ ] **Step 4: Add the upsert SQL builder**

In `src/smart_proxy/db.py`, directly after `build_usage_session_upsert_sql` (~line 808), add:

```python
def build_usage_hourly_upsert_sql(backend: str) -> str:
    q = "usage_key_hourly." if backend == "postgres" else ""
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_key_hourly\n"
        "    (hour_utc, proxy_key, model,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(hour_utc, proxy_key, model) DO UPDATE SET\n"
        f"                   {sets}"
    )
```

- [ ] **Step 5: Add the `Database` methods**

In `src/smart_proxy/db.py`, add right after `upsert_usage_session_batch` (~line 1168):

```python
    async def upsert_usage_hourly_batch(self, rows: list[tuple]) -> None:
        """Batch-upsert hourly per-key usage buckets (spend-limit source).

        Each tuple: (hour_utc, proxy_key, model,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_creation_tokens, cache_creation_5m_tokens,
                      cache_creation_1h_tokens, web_search_requests, requests)
        """
        if not rows:
            return
        await self.db.executemany(build_usage_hourly_upsert_sql(self._backend), rows)
        await self.db.commit()

    async def query_usage_key_hourly(
        self, start_hour: str, end_hour: str
    ) -> list[dict]:
        """Token sums per (proxy_key, model) over [start_hour, end_hour).

        Hours are ``'%Y-%m-%dT%H'`` strings, which sort lexicographically in
        chronological order, so a plain string range works on both backends.
        """
        cur = await self.db.execute(
            """SELECT proxy_key, model,
                      SUM(input_tokens)  AS input_tokens,
                      SUM(output_tokens) AS output_tokens,
                      SUM(cache_read_tokens) AS cache_read_tokens,
                      SUM(cache_creation_tokens) AS cache_creation_tokens,
                      SUM(cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                      SUM(cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                      SUM(web_search_requests) AS web_search_requests,
                      SUM(requests) AS requests
               FROM usage_key_hourly
               WHERE hour_utc >= ? AND hour_utc < ?
               GROUP BY proxy_key, model""",
            (start_hour, end_hour),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def prune_usage_key_hourly(self, before_hour: str) -> None:
        """Drop hourly buckets older than ``before_hour`` (retention)."""
        await self.db.execute(
            "DELETE FROM usage_key_hourly WHERE hour_utc < ?", (before_hour,)
        )
        await self.db.commit()
```

And right after `get_active_proxy_keys` (~line 1120):

```python
    async def get_proxy_key_by_created_at(self, created_at: str) -> str | None:
        """Full proxy key with this exact ``created_at``, or None when zero or
        multiple rows match. ``created_at`` is the stable per-row identity; the
        display prefix is not (distinct keys can share leading characters)."""
        cur = await self.db.execute(
            "SELECT key FROM proxy_api_keys WHERE created_at = ?", (created_at,)
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        return str(rows[0]["key"])

    # ------------------------------------------------------------------
    # Proxy key spend limits
    # ------------------------------------------------------------------

    async def list_proxy_key_limits(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT proxy_key, kind, amount FROM proxy_key_limits"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def set_proxy_key_limit(
        self, proxy_key: str, kind: str, amount: float
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """INSERT INTO proxy_key_limits (proxy_key, kind, amount, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(proxy_key, kind) DO UPDATE SET
                   amount = excluded.amount,
                   updated_at = excluded.updated_at""",
            (proxy_key, kind, float(amount), now),
        )
        await self.db.commit()

    async def delete_proxy_key_limit(self, proxy_key: str, kind: str) -> None:
        await self.db.execute(
            "DELETE FROM proxy_key_limits WHERE proxy_key = ? AND kind = ?",
            (proxy_key, kind),
        )
        await self.db.commit()
```

- [ ] **Step 6: Add both tables to `SNAPSHOT_TABLE_SPECS`**

In `src/smart_proxy/db.py`, append two entries to the `SNAPSHOT_TABLE_SPECS` tuple (~line 474), before its closing `)`:

```python
    (
        "proxy_key_limits",
        ("proxy_key", "kind", "amount", "updated_at"),
        "proxy_key, kind",
    ),
    (
        "usage_key_hourly",
        (
            "hour_utc", "proxy_key", "model",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cache_creation_5m_tokens",
            "cache_creation_1h_tokens", "web_search_requests", "requests",
        ),
        "hour_utc, proxy_key, model",
    ),
```

- [ ] **Step 7: Add the Postgres migration**

In `src/smart_proxy/db_migrations.py`, append this entry to the `POSTGRES_MIGRATIONS` tuple (after `0009_anthropic_keys_role`):

```python
    (
        "0010_proxy_key_limits",
        (
            """
            CREATE TABLE IF NOT EXISTS proxy_key_limits (
                proxy_key  TEXT NOT NULL,
                kind       TEXT NOT NULL,
                amount     DOUBLE PRECISION NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (proxy_key, kind)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS usage_key_hourly (
                hour_utc                 TEXT    NOT NULL,
                proxy_key                TEXT    NOT NULL,
                model                    TEXT    NOT NULL,
                input_tokens             INTEGER NOT NULL DEFAULT 0,
                output_tokens            INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
                cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
                cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
                web_search_requests      INTEGER NOT NULL DEFAULT 0,
                requests                 INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hour_utc, proxy_key, model)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_usage_key_hourly_hour ON usage_key_hourly(hour_utc)",
        ),
    ),
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_key_limits_db.py tests/test_db_snapshot_roundtrip.py -v`
Expected: PASS (all). The existing roundtrip test exercises `export_snapshot` over every
`SNAPSHOT_TABLE_SPECS` entry, so it catches a malformed column list in the two new entries.

- [ ] **Step 9: Commit**

```bash
git add src/smart_proxy/db.py src/smart_proxy/db_migrations.py tests/test_key_limits_db.py
git commit -m "feat(db): hourly usage buckets and proxy-key limit tables"
```

---

### Task 2: Hourly buckets written by `UsageTracker`

**Files:**
- Modify: `src/smart_proxy/usage.py` (`UsageTracker.__init__` ~line 593, `record` ~line 603, `flush` ~line 663)
- Test: `tests/test_usage_tracker_hourly.py` (create)

**Interfaces:**
- Consumes: `Database.upsert_usage_hourly_batch` from Task 1.
- Produces: `UsageTracker._hour_buf` keyed `(hour_utc, proxy_key, model)`; `flush()` continues to return the **`usage_daily` rows only** (its existing contract — `_window_usage_deltas` in `anthropic_proxy.py` depends on that shape and must not change).

- [ ] **Step 1: Write the failing test**

Create `tests/test_usage_tracker_hourly.py`:

```python
# tests/test_usage_tracker_hourly.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from datetime import datetime, timezone

from smart_proxy.db import build_database_from_config
from smart_proxy.usage import UsageTracker


class _FrozenDatetime(datetime):
    _now = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._now if tz is None else cls._now.astimezone(tz)


class HourlyBufferTests(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        return db

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_record_buckets_by_utc_hour_and_flush_writes_them(self):
        db = await self._db()
        tracker = UsageTracker()
        with patch("smart_proxy.usage.datetime", _FrozenDatetime):
            tracker.record("sp-a", "cred-1", "anthropic", "claude-opus-4-5", 100, 20,
                           cache_read_tokens=5, web_search_requests=1)
            tracker.record("sp-a", "cred-1", "anthropic", "claude-opus-4-5", 50, 10)
            tracker.record("sp-b", "cred-1", "anthropic", "claude-sonnet-5", 7, 3)
        await tracker.flush(db)

        rows = await db.query_usage_key_hourly("2026-07-30T14", "2026-07-30T15")
        got = {(r["proxy_key"], r["model"]): (int(r["input_tokens"]),
                                              int(r["output_tokens"]),
                                              int(r["requests"])) for r in rows}
        self.assertEqual(got, {
            ("sp-a", "claude-opus-4-5"): (150, 30, 2),
            ("sp-b", "claude-sonnet-5"): (7, 3, 1),
        })

    async def test_flush_still_returns_usage_daily_rows_only(self):
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 1, 1)
        rows = await tracker.flush(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 15)   # usage_daily upsert arity

    async def test_flush_clears_the_hour_buffer(self):
        db = await self._db()
        tracker = UsageTracker()
        tracker.record("sp-a", "cred-1", "anthropic", "m", 1, 1)
        await tracker.flush(db)
        await tracker.flush(db)
        rows = await db.query_usage_key_hourly("2000-01-01T00", "2100-01-01T00")
        self.assertEqual(int(rows[0]["requests"]), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_tracker_hourly.py -v`
Expected: FAIL — `query_usage_key_hourly` returns `[]`, so the dict comparison fails.

- [ ] **Step 3: Add the hour buffer to `UsageTracker`**

In `src/smart_proxy/usage.py`, in `UsageTracker.__init__` (~line 593), add after the `_session_buf` line:

```python
        # hour key: (hour_utc, proxy_key, model) -> 8 counters
        self._hour_buf: dict[tuple, list[int]] = {}
```

In `record()`, replace the existing first line

```python
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
```

with a single clock read used for both keys (two separate `now()` calls could straddle an hour
boundary and split one request across buckets):

```python
        now = datetime.now(timezone.utc)
        date = now.strftime("%Y-%m-%d")
        hour = now.strftime("%Y-%m-%dT%H")
```

Then, immediately after the `kacc` accumulation loop and before the `if session_id:` block, add:

```python
        hkey = (hour, proxy_key, model)
        hacc = self._hour_buf.get(hkey)
        if hacc is None:
            hacc = [0] * 8
            self._hour_buf[hkey] = hacc
        for i, c in enumerate(counters):
            hacc[i] += c
```

- [ ] **Step 4: Flush the hour buffer**

In `flush()`, inside the `async with self._lock:` block, after `self._session_buf = {}`, add:

```python
            hour_snapshot = self._hour_buf
            self._hour_buf = {}
```

And after the existing `await db.upsert_usage_session_batch(session_rows)` line, add:

```python
        hour_rows = [(k[0], k[1], k[2], *v) for k, v in hour_snapshot.items()]
        await db.upsert_usage_hourly_batch(hour_rows)
```

Note: `flush()` returns early with `[]` when `self._buf` is empty. That is correct here — `_hour_buf` is only ever populated by the same `record()` call that populates `_buf`, so they empty together.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_usage_tracker_hourly.py tests/test_usage_tracker_kind_session.py tests/test_anthropic_cache_usage.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/usage.py tests/test_usage_tracker_hourly.py
git commit -m "feat(usage): buffer and flush hourly per-key usage buckets"
```

---

### Task 3: `KeyLimiter` — window math, limits, live spend

**Files:**
- Create: `src/smart_proxy/key_limits.py`
- Test: `tests/test_key_limits.py` (create)

**Interfaces:**
- Consumes: `Database.list_proxy_key_limits`, `set_proxy_key_limit`, `delete_proxy_key_limit`, `query_usage_key_hourly`, `get_all_model_prices` (existing, `db.py:1333`); `smart_proxy.usage.build_price_lookup`, `calculate_cost`.
- Produces:
  - `LIMIT_KINDS: dict[str, LimitKind]` — keys are limit ids; `LimitKind` has `.id`, `.window_hours`, `.label`.
  - `class KeyLimiter(db, *, tz: str = "Europe/Paris")` with:
    - `async load() -> None`
    - `async seed(now: datetime | None = None) -> None`
    - `check(proxy_key: str, now=None) -> LimitBlock | None`
    - `add(proxy_key: str, model: str, usage: tuple[int, ...], now=None) -> None`
    - `async set_limit(proxy_key: str, kind: str, amount: float | None) -> None`
    - `limits_for(proxy_key: str) -> dict[str, float]`
    - `snapshot(proxy_key: str, now=None) -> dict[str, dict]`
    - `window_start(now=None) -> datetime`, `window_end(now=None) -> datetime`, `retry_after(now=None) -> int`
  - `LimitBlock` dataclass: `.kind`, `.label`, `.retry_after` (int seconds), `.limit_usd`, `.spent_usd`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_key_limits.py`:

```python
# tests/test_key_limits.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
from datetime import datetime, timezone
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import build_database_from_config
from smart_proxy.key_limits import LIMIT_KINDS, KeyLimiter


def _utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


class WindowMathTests(unittest.IsolatedAsyncioTestCase):
    async def _limiter(self, tz="Europe/Paris"):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self._db = db
        return KeyLimiter(db, tz=tz)

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_paris_summer_window_starts_at_22utc(self):
        lim = await self._limiter()
        # 2026-07-30 12:00 UTC is 14:00 Paris (UTC+2, CEST).
        now = _utc(2026, 7, 30, 12)
        self.assertEqual(lim.window_start(now), _utc(2026, 7, 29, 22))
        self.assertEqual(lim.window_end(now), _utc(2026, 7, 30, 22))

    async def test_paris_winter_window_starts_at_23utc(self):
        lim = await self._limiter()
        # 2026-01-15 12:00 UTC is 13:00 Paris (UTC+1, CET).
        now = _utc(2026, 1, 15, 12)
        self.assertEqual(lim.window_start(now), _utc(2026, 1, 14, 23))
        self.assertEqual(lim.window_end(now), _utc(2026, 1, 15, 23))

    async def test_utc_tz_setting_gives_utc_midnight(self):
        lim = await self._limiter(tz="UTC")
        now = _utc(2026, 7, 30, 12)
        self.assertEqual(lim.window_start(now), _utc(2026, 7, 30, 0))
        self.assertEqual(lim.window_end(now), _utc(2026, 7, 31, 0))

    async def test_retry_after_is_seconds_until_window_end(self):
        lim = await self._limiter(tz="UTC")
        self.assertEqual(lim.retry_after(_utc(2026, 7, 30, 23, 0)), 3600)
        self.assertGreaterEqual(lim.retry_after(_utc(2026, 7, 30, 23, 59)), 1)


class LimitEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def _limiter(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self._db = db
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        return lim

    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_key_without_a_limit_is_never_blocked(self):
        lim = await self._limiter()
        lim.add("sp-a", "claude-opus-4-5", (10_000_000, 10_000_000, 0, 0, 0, 0, 0))
        self.assertIsNone(lim.check("sp-a"))

    async def test_block_once_spend_reaches_the_limit(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        self.assertIsNone(lim.check("sp-a"))
        # 200k input tokens of claude-opus-4-5 at $5/Mtok = $1.00
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))
        block = lim.check("sp-a")
        self.assertIsNotNone(block)
        self.assertEqual(block.kind, "daily_usd")
        self.assertEqual(block.label, "24h")
        self.assertGreater(block.retry_after, 0)

    async def test_raising_the_limit_unblocks_immediately(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))
        self.assertIsNotNone(lim.check("sp-a"))
        await lim.set_limit("sp-a", "daily_usd", 5.0)
        self.assertIsNone(lim.check("sp-a"))

    async def test_clearing_the_limit_makes_the_key_unlimited(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))
        self.assertIsNotNone(lim.check("sp-a"))
        await lim.set_limit("sp-a", "daily_usd", None)
        self.assertIsNone(lim.check("sp-a"))
        self.assertEqual(await self._db.list_proxy_key_limits(), [])

    async def test_unknown_model_costs_nothing_and_never_blocks(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 0.01)
        lim.add("sp-a", "totally-unknown-model", (1_000_000, 1_000_000, 0, 0, 0, 0, 0))
        self.assertIsNone(lim.check("sp-a"))

    async def test_window_rollover_clears_spend(self):
        lim = await self._limiter()
        await lim.set_limit("sp-a", "daily_usd", 1.0)
        day1 = _utc(2026, 7, 30, 12)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0), now=day1)
        self.assertIsNotNone(lim.check("sp-a", now=day1))
        day2 = _utc(2026, 7, 31, 12)
        self.assertIsNone(lim.check("sp-a", now=day2))

    async def test_set_limit_rejects_unknown_kind(self):
        lim = await self._limiter()
        with self.assertRaises(ValueError):
            await lim.set_limit("sp-a", "hourly_bananas", 1.0)


class SeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_seed_reads_spend_from_hourly_buckets_in_window(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        await db.upsert_usage_hourly_batch([
            # in window (UTC day 2026-07-30): 200k input opus = $1.00
            ("2026-07-30T09", "sp-a", "claude-opus-4-5", 200_000, 0, 0, 0, 0, 0, 0, 1),
            # out of window — previous day
            ("2026-07-29T09", "sp-a", "claude-opus-4-5", 200_000, 0, 0, 0, 0, 0, 0, 1),
        ])
        await db.set_proxy_key_limit("sp-a", "daily_usd", 1.0)
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        await lim.seed(now=_utc(2026, 7, 30, 12))
        block = lim.check("sp-a", now=_utc(2026, 7, 30, 12))
        self.assertIsNotNone(block)
        self.assertAlmostEqual(block.spent_usd, 1.0, places=6)


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        tmp = getattr(self, "_tmp", None)
        if tmp:
            tmp.cleanup()

    async def test_snapshot_shape_for_limited_and_unlimited_keys(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        await lim.set_limit("sp-a", "daily_usd", 4.0)
        lim.add("sp-a", "claude-opus-4-5", (200_000, 0, 0, 0, 0, 0, 0))

        limited = lim.snapshot("sp-a")["daily_usd"]
        self.assertAlmostEqual(limited["limit_usd"], 4.0)
        self.assertAlmostEqual(limited["spent_usd"], 1.0, places=6)
        self.assertAlmostEqual(limited["remaining_usd"], 3.0, places=6)
        self.assertAlmostEqual(limited["percent"], 25.0, places=1)
        self.assertFalse(limited["exceeded"])
        self.assertTrue(limited["resets_at"].startswith("20"))

        unlimited = lim.snapshot("sp-b")["daily_usd"]
        self.assertIsNone(unlimited["limit_usd"])
        self.assertIsNone(unlimited["remaining_usd"])
        self.assertIsNone(unlimited["percent"])
        self.assertEqual(unlimited["spent_usd"], 0.0)
        self.assertFalse(unlimited["exceeded"])

    async def test_limits_for_returns_configured_amounts(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        lim = KeyLimiter(db, tz="UTC")
        await lim.load()
        self.assertEqual(lim.limits_for("sp-a"), {})
        await lim.set_limit("sp-a", "daily_usd", 12.5)
        self.assertEqual(lim.limits_for("sp-a"), {"daily_usd": 12.5})


class KindRegistryTests(unittest.TestCase):
    def test_daily_usd_is_registered_with_a_24h_label(self):
        self.assertIn("daily_usd", LIMIT_KINDS)
        self.assertEqual(LIMIT_KINDS["daily_usd"].label, "24h")
        self.assertEqual(LIMIT_KINDS["daily_usd"].window_hours, 24)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_key_limits.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'smart_proxy.key_limits'`

- [ ] **Step 3: Write the implementation**

Create `src/smart_proxy/key_limits.py`:

```python
"""Per-proxy-key spend limits.

Holds the configured limits and the live per-key spend for the current window
in memory, so the proxy's request-path check is a dict lookup with no I/O. The
database is the recovery source: :meth:`KeyLimiter.load` re-reads limits and
prices and re-seeds spend from ``usage_key_hourly`` at startup and on reload.

The counter is exact for the lifetime of the process; a restart re-seeds from
the hourly buckets and therefore loses at most the <60s the previous process
had not yet flushed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo

from smart_proxy.usage import build_price_lookup, calculate_cost

logger = logging.getLogger(__name__)

HOUR_FMT = "%Y-%m-%dT%H"
DEFAULT_WINDOW_TZ = "Europe/Paris"


@dataclass(frozen=True)
class LimitKind:
    """A configurable limit type. ``label`` appears in the 429 message."""

    id: str
    window_hours: int
    label: str


# Every kind here currently shares one window (local midnight → local midnight),
# which is why spend is a single scalar per key. A future kind with a *different*
# window needs `_spent` keyed by kind and a per-kind rollover check.
LIMIT_KINDS: dict[str, LimitKind] = {
    "daily_usd": LimitKind(id="daily_usd", window_hours=24, label="24h"),
}


@dataclass(frozen=True)
class LimitBlock:
    """A key that has reached one of its limits."""

    kind: str
    label: str
    retry_after: int
    limit_usd: float
    spent_usd: float


class KeyLimiter:
    def __init__(self, db, *, tz: str = DEFAULT_WINDOW_TZ) -> None:
        self._db = db
        self._tz = ZoneInfo(tz)
        self._limits: dict[str, dict[str, float]] = {}
        self._spent: dict[str, float] = {}
        self._prices: dict | None = None
        self._window_start: datetime | None = None

    # -- window math ----------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def window_start(self, now: datetime | None = None) -> datetime:
        """Most recent local midnight, as a UTC instant."""
        current = now or self._now()
        local = current.astimezone(self._tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(timezone.utc)

    def window_end(self, now: datetime | None = None) -> datetime:
        """Next local midnight, as a UTC instant.

        The ``+ timedelta(days=1)`` is wall-clock arithmetic on a zone-aware
        datetime, so a DST day correctly yields a 23h or 25h window.
        """
        current = now or self._now()
        local = current.astimezone(self._tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight + timedelta(days=1)).astimezone(timezone.utc)

    def retry_after(self, now: datetime | None = None) -> int:
        current = now or self._now()
        return max(1, ceil((self.window_end(current) - current).total_seconds()))

    def _roll_if_needed(self, now: datetime | None = None) -> None:
        start = self.window_start(now or self._now())
        if self._window_start != start:
            self._window_start = start
            self._spent = {}

    # -- loading --------------------------------------------------------

    async def load(self) -> None:
        """Re-read limits and prices from the DB, then re-seed spend."""
        rows = await self._db.list_proxy_key_limits()
        limits: dict[str, dict[str, float]] = {}
        for row in rows:
            kind = str(row["kind"])
            if kind not in LIMIT_KINDS:
                continue
            amount = float(row["amount"] or 0.0)
            if amount <= 0:
                continue     # <= 0 means unlimited
            limits.setdefault(str(row["proxy_key"]), {})[kind] = amount
        self._limits = limits
        self._prices = build_price_lookup(await self._db.get_all_model_prices())
        await self.seed()
        logger.info(
            "Key limits loaded: %d key(s) limited, window starts %s",
            len(self._limits), self.window_start().isoformat(),
        )

    async def seed(self, now: datetime | None = None) -> None:
        """Recompute per-key spend for the current window from hourly buckets."""
        current = now or self._now()
        start = self.window_start(current).strftime(HOUR_FMT)
        end = self.window_end(current).strftime(HOUR_FMT)
        rows = await self._db.query_usage_key_hourly(start, end)
        spent: dict[str, float] = {}
        for row in rows:
            key = str(row["proxy_key"])
            spent[key] = spent.get(key, 0.0) + self._row_cost(row)
        self._spent = spent
        self._window_start = self.window_start(current)

    def _row_cost(self, row: dict) -> float:
        return calculate_cost(
            model=str(row["model"]),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cache_read_tokens=int(row["cache_read_tokens"] or 0),
            cache_creation_tokens=int(row["cache_creation_tokens"] or 0),
            cache_creation_5m_tokens=int(row["cache_creation_5m_tokens"] or 0),
            cache_creation_1h_tokens=int(row["cache_creation_1h_tokens"] or 0),
            web_search_requests=int(row["web_search_requests"] or 0),
            prices=self._prices,
        ).total_cost or 0.0

    # -- request path ---------------------------------------------------

    def check(self, proxy_key: str, now: datetime | None = None) -> LimitBlock | None:
        """Return the blocking limit, or None. Dict lookups only — no I/O."""
        current = now or self._now()
        self._roll_if_needed(current)
        limits = self._limits.get(proxy_key)
        if not limits:
            return None
        spent = self._spent.get(proxy_key, 0.0)
        for kind, amount in limits.items():
            if spent >= amount:
                return LimitBlock(
                    kind=kind,
                    label=LIMIT_KINDS[kind].label,
                    retry_after=self.retry_after(current),
                    limit_usd=amount,
                    spent_usd=spent,
                )
        return None

    def add(
        self,
        proxy_key: str,
        model: str,
        usage: tuple[int, ...],
        now: datetime | None = None,
    ) -> None:
        """Add one request's cost. ``usage`` is the 7-tuple from
        ``extract_usage`` / ``extract_usage_from_sse``."""
        self._roll_if_needed(now or self._now())
        padded = tuple(usage) + (0,) * (7 - len(usage))
        cost = calculate_cost(
            model=model,
            input_tokens=int(padded[0] or 0),
            output_tokens=int(padded[1] or 0),
            cache_read_tokens=int(padded[2] or 0),
            cache_creation_tokens=int(padded[3] or 0),
            cache_creation_5m_tokens=int(padded[4] or 0),
            cache_creation_1h_tokens=int(padded[5] or 0),
            web_search_requests=int(padded[6] or 0),
            prices=self._prices,
        ).total_cost
        if cost:
            self._spent[proxy_key] = self._spent.get(proxy_key, 0.0) + cost

    # -- configuration --------------------------------------------------

    async def set_limit(
        self, proxy_key: str, kind: str, amount: float | None
    ) -> None:
        """Persist a limit and apply it in-process, so the very next request
        sees it. ``None`` or ``<= 0`` removes the limit (unlimited)."""
        if kind not in LIMIT_KINDS:
            raise ValueError(f"unknown limit kind: {kind}")
        if amount is None or float(amount) <= 0:
            await self._db.delete_proxy_key_limit(proxy_key, kind)
            per_key = self._limits.get(proxy_key)
            if per_key is not None:
                per_key.pop(kind, None)
                if not per_key:
                    self._limits.pop(proxy_key, None)
            return
        await self._db.set_proxy_key_limit(proxy_key, kind, float(amount))
        self._limits.setdefault(proxy_key, {})[kind] = float(amount)

    def limits_for(self, proxy_key: str) -> dict[str, float]:
        return dict(self._limits.get(proxy_key) or {})

    def snapshot(self, proxy_key: str, now: datetime | None = None) -> dict[str, dict]:
        """Per-kind status for the dashboard and /_oauth_usage."""
        current = now or self._now()
        self._roll_if_needed(current)
        spent = round(self._spent.get(proxy_key, 0.0), 6)
        resets_at = self.window_end(current).astimezone(self._tz).isoformat()
        out: dict[str, dict] = {}
        for kind in LIMIT_KINDS:
            amount = (self._limits.get(proxy_key) or {}).get(kind)
            out[kind] = {
                "limit_usd": amount,
                "spent_usd": spent,
                "remaining_usd": None if amount is None else round(max(0.0, amount - spent), 6),
                "percent": None if not amount else round(spent / amount * 100, 1),
                "resets_at": resets_at,
                "exceeded": bool(amount is not None and spent >= amount),
            }
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_key_limits.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/key_limits.py tests/test_key_limits.py
git commit -m "feat(limits): KeyLimiter with window math, limits and live spend"
```

---

### Task 4: Enforce the limit in the proxy

**Files:**
- Modify: `src/smart_proxy/config.py` (add setting)
- Modify: `src/smart_proxy/anthropic_proxy.py` (import, `_is_billable_path` helper, gate in `_proxy_handler` ~line 1926, spend accumulation after `tracker.record` ~line 2205, `_reload_handler` ~line 3119, retention prune in `_usage_flush_loop` ~line 3163, `_on_startup` ~line 3500, `create_app` ~line 3542, `main` ~line 3636)
- Modify: `.env.example`
- Test: `tests/test_anthropic_proxy_key_limit.py` (create)

**Interfaces:**
- Consumes: `KeyLimiter`, `LimitBlock` from Task 3; `Database.prune_usage_key_hourly` from Task 1.
- Produces:
  - `app["key_limiter"]: KeyLimiter`
  - `app["limit_window_tz"]: str`
  - `_is_billable_path(method: str, path: str) -> bool` in `anthropic_proxy.py`
  - `async _resync_limiter(app: web.Application) -> None` in `anthropic_proxy.py` — the only correct way to reload the limiter at runtime; Task 5 calls it too
  - `create_app(..., limit_window_tz: str = "Europe/Paris")`

- [ ] **Step 1: Write the failing test**

Create `tests/test_anthropic_proxy_key_limit.py`:

```python
# tests/test_anthropic_proxy_key_limit.py
from __future__ import annotations
import json, sys, unittest
from pathlib import Path
from unittest.mock import MagicMock
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aiohttp.test_utils import make_mocked_request

from smart_proxy import anthropic_proxy
from smart_proxy.key_limits import LimitBlock


class BillablePathTests(unittest.TestCase):
    def test_messages_post_is_billable(self):
        self.assertTrue(anthropic_proxy._is_billable_path("POST", "/v1/messages"))

    def test_count_tokens_is_not_billable(self):
        self.assertFalse(
            anthropic_proxy._is_billable_path("POST", "/v1/messages/count_tokens"))

    def test_non_post_and_other_paths_are_not_billable(self):
        self.assertFalse(anthropic_proxy._is_billable_path("GET", "/v1/messages"))
        self.assertFalse(anthropic_proxy._is_billable_path("POST", "/v1/models"))


class LimitGateTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, block):
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.pick.side_effect = AssertionError("pool must not be consulted when blocked")
        limiter = MagicMock()
        limiter.check.return_value = block
        return {"anthropic_pool": pool, "http_client": MagicMock(), "key_limiter": limiter}

    async def test_over_limit_returns_429_with_smartproxy_message(self):
        block = LimitBlock(kind="daily_usd", label="24h", retry_after=3554,
                           limit_usd=25.0, spent_usd=25.4)
        req = make_mocked_request(
            "POST", "/v1/messages", app=self._app(block),
            headers={"Authorization": "Bearer sp-a"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        resp = await anthropic_proxy._proxy_handler(req)
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers["retry-after"], "3554")
        self.assertEqual(resp.headers["x-should-retry"], "false")
        payload = json.loads(resp.body)
        self.assertEqual(payload["error"]["type"], "rate_limit_error")
        self.assertEqual(
            payload["error"]["message"],
            "SmartProxy: you have reached your 24h limit. Retry in 59m 14s",
        )

    async def test_count_tokens_is_not_gated(self):
        block = LimitBlock(kind="daily_usd", label="24h", retry_after=10,
                           limit_usd=1.0, spent_usd=2.0)
        app = self._app(block)
        app["anthropic_pool"].pick.side_effect = None
        app["anthropic_pool"].pick.return_value = None
        app["anthropic_pool"].next_available_in.return_value = 0
        app["anthropic_pool"]._keys = []
        req = make_mocked_request(
            "POST", "/v1/messages/count_tokens", app=app,
            headers={"Authorization": "Bearer sp-a"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        resp = await anthropic_proxy._proxy_handler(req)
        # Not a 429 from the limiter: it fell through to the normal no-keys path.
        self.assertEqual(resp.status, 503)

    async def test_under_limit_falls_through_to_the_pool(self):
        app = self._app(None)
        app["anthropic_pool"].pick.side_effect = None
        app["anthropic_pool"].pick.return_value = None
        app["anthropic_pool"].next_available_in.return_value = 0
        app["anthropic_pool"]._keys = []
        req = make_mocked_request(
            "POST", "/v1/messages", app=app, headers={"Authorization": "Bearer sp-a"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        resp = await anthropic_proxy._proxy_handler(req)
        self.assertEqual(resp.status, 503)

    async def test_passthrough_token_is_checked_under_its_own_bucket(self):
        app = self._app(None)
        app["anthropic_pool"].pick.side_effect = None
        app["anthropic_pool"].pick.return_value = None
        app["anthropic_pool"].next_available_in.return_value = 0
        app["anthropic_pool"]._keys = []
        req = make_mocked_request(
            "POST", "/v1/messages", app=app,
            headers={"Authorization": "Bearer sk-ant-xyz"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        await anthropic_proxy._proxy_handler(req)
        app["key_limiter"].check.assert_called_once_with("claude-passthrough")


class ResyncLimiterTests(unittest.IsolatedAsyncioTestCase):
    """Seeding replaces the live counter with the hourly-bucket totals, so a
    reload must flush buffered usage first or it forgives unflushed spend."""

    async def test_resync_flushes_usage_before_reloading_the_limiter(self):
        calls = []
        tracker = MagicMock()
        db = MagicMock()
        limiter = MagicMock()
        limiter.load = _record_async(calls, "load")
        app = {"key_limiter": limiter, "usage_tracker": tracker, "db": db}
        with patch.object(
            anthropic_proxy, "_flush_usage", _record_async(calls, "flush")
        ):
            await anthropic_proxy._resync_limiter(app)
        self.assertEqual(calls, ["flush", "load"])

    async def test_resync_is_a_noop_without_a_limiter(self):
        calls = []
        app = {"usage_tracker": MagicMock(), "db": MagicMock()}
        with patch.object(
            anthropic_proxy, "_flush_usage", _record_async(calls, "flush")
        ):
            await anthropic_proxy._resync_limiter(app)
        self.assertEqual(calls, [])

    async def test_resync_still_loads_when_there_is_nothing_to_flush(self):
        calls = []
        limiter = MagicMock()
        limiter.load = _record_async(calls, "load")
        await anthropic_proxy._resync_limiter({"key_limiter": limiter})
        self.assertEqual(calls, ["load"])


def _record_async(calls, name):
    async def _inner(*_args, **_kwargs):
        calls.append(name)
    return _inner


def _returns(value):
    async def _inner():
        return value
    return _inner


if __name__ == "__main__":
    unittest.main()
```

Add `patch` to the `unittest.mock` import line in this file.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_key_limit.py -v`
Expected: FAIL — `AttributeError: module 'smart_proxy.anthropic_proxy' has no attribute '_is_billable_path'`

- [ ] **Step 3: Add the setting**

In `src/smart_proxy/config.py`, add to `Settings` next to the other proxy settings:

```python
    # Local timezone whose midnight starts the per-key spend-limit window.
    anthropic_proxy_limit_window_tz: str = "Europe/Paris"
```

In `.env.example`, under the "Anthropic proxy" block, add:

```bash
# Timezone whose midnight starts the per-key 24h spend-limit window (see /_app/#keys).
# Set to UTC to align the window with the usage dashboard's UTC days.
ANTHROPIC_PROXY_LIMIT_WINDOW_TZ=Europe/Paris
```

- [ ] **Step 4: Add the gate to `_proxy_handler`**

In `src/smart_proxy/anthropic_proxy.py`, add the import near the other `smart_proxy` imports at the top:

```python
from smart_proxy.key_limits import DEFAULT_WINDOW_TZ, KeyLimiter
```

Add this helper directly above `async def _proxy_handler` (~line 1908):

```python
def _is_billable_path(method: str, path: str) -> bool:
    """True for requests that can consume budget.

    ``count_tokens`` is free and clients call it constantly, so gating it would
    break them without protecting the budget.
    """
    return (
        method == "POST"
        and "/v1/messages" in path
        and not path.endswith("/count_tokens")
    )
```

Inside `_proxy_handler`, immediately after `path = request.path` (~line 1925) and before the
`if request.method == "POST" and "/v1/messages" in path:` block, insert:

```python
    limiter: KeyLimiter | None = request.app.get("key_limiter")
    if limiter is not None and _is_billable_path(request.method, path):
        block = limiter.check(usage_proxy_key)
        if block is not None:
            message = (
                f"SmartProxy: you have reached your {block.label} limit. "
                f"Retry in {_humanize_seconds(block.retry_after)}"
            )
            logger.info(
                "429 spend limit: key=%s spent=%.4f limit=%.2f retry_after=%ds",
                _mask(usage_proxy_key), block.spent_usd, block.limit_usd,
                block.retry_after,
            )
            return web.Response(
                status=429,
                headers={
                    "retry-after": str(block.retry_after),
                    "x-should-retry": "false",
                },
                body=json.dumps({
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": message,
                    },
                }).encode(),
                content_type="application/json",
            )
```

- [ ] **Step 5: Accumulate spend after each recorded response**

In `_proxy_handler`, immediately after the `tracker.record(...)` call closes (~line 2205), still inside `if usage:`, add:

```python
                if limiter is not None:
                    try:
                        limiter.add(usage_proxy_key, model, usage)
                    except Exception:
                        # The response is already streamed; never fail it here.
                        logger.exception("spend accounting failed")
```

- [ ] **Step 6: Wire the limiter into startup, reload, and the flush loop**

In `_on_startup` (~line 3500), after `app["usage_tracker"] = tracker`:

```python
    limiter = KeyLimiter(db, tz=app.get("limit_window_tz") or DEFAULT_WINDOW_TZ)
    await limiter.load()
    app["key_limiter"] = limiter
```

Add this helper directly after `_flush_usage` (~line 3161):

```python
async def _resync_limiter(app: web.Application) -> None:
    """Re-read limits and prices, then re-seed spend from the hourly buckets.

    Flushes buffered usage first. Seeding *replaces* the live spend counter with
    the hourly-bucket totals, so whatever the tracker has not flushed yet would
    otherwise be silently forgiven — every reload would hand each key back up to
    a flush interval's worth of budget.
    """
    limiter: KeyLimiter | None = app.get("key_limiter")
    if limiter is None:
        return
    tracker: UsageTracker | None = app.get("usage_tracker")
    db: Database | None = app.get("db")
    if tracker is not None and db is not None:
        await _flush_usage(tracker, db)
    await limiter.load()
```

In `_reload_handler` (~line 3119), replace `await pool.reload()` with:

```python
    await pool.reload()
    await _resync_limiter(request.app)
```

In `_usage_flush_loop` (~line 3163), replace the body of the `try:` with a version that also
prunes at most once an hour:

```python
async def _usage_flush_loop(app: web.Application) -> None:
    tracker: UsageTracker = app["usage_tracker"]
    db: Database = app["db"]
    last_prune = 0.0
    while True:
        await asyncio.sleep(_USAGE_FLUSH_INTERVAL)
        try:
            n = await _flush_usage(tracker, db)
            if n:
                logger.info("Flushed %d usage rows to DB", n)
        except Exception as exc:
            logger.warning("Usage flush error: %s", exc)
        now = time.monotonic()
        if now - last_prune >= 3600:
            last_prune = now
            try:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=_HOURLY_RETENTION_DAYS)
                ).strftime("%Y-%m-%dT%H")
                await db.prune_usage_key_hourly(cutoff)
            except Exception as exc:
                logger.warning("Hourly usage prune error: %s", exc)
```

Add next to `_USAGE_FLUSH_INTERVAL` (~line 3130):

```python
_HOURLY_RETENTION_DAYS = 35  # covers any window up to monthly; bounds the table
```

- [ ] **Step 7: Thread the setting through `create_app` and `main`**

In `create_app` (~line 3542), add the keyword parameter after `oauth_usage_cache_seconds`:

```python
    limit_window_tz: str = DEFAULT_WINDOW_TZ,
```

and inside the body, next to the other `app[...]` assignments:

```python
    app["limit_window_tz"] = (limit_window_tz or DEFAULT_WINDOW_TZ).strip()
```

In `main()` (~line 3636), add to the `create_app(...)` call:

```python
        limit_window_tz=settings.anthropic_proxy_limit_window_tz,
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_key_limit.py tests/test_anthropic_proxy_oauth_messages.py tests/test_proxy_classify_wiring.py -v`
Expected: PASS (all)

- [ ] **Step 9: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py src/smart_proxy/config.py .env.example tests/test_anthropic_proxy_key_limit.py
git commit -m "feat(proxy): enforce per-key 24h spend limits with a 429"
```

---

### Task 5: Dashboard API — read and edit limits

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (`_api_keys` ~line 142, new `_api_key_limits`, `_api_reload`, route table ~line 553)
- Test: `tests/test_dashboard_api.py` (extend)

**Interfaces:**
- Consumes: `KeyLimiter.limits_for`, `.snapshot`, `.set_limit`, `LIMIT_KINDS` from Task 3; `Database.get_proxy_key_by_created_at` from Task 1.
- Produces:
  - `GET /api/keys` entries gain `"limits": dict[str, float]` and `"usage": dict[str, dict]`.
  - `POST /api/keys/limits` accepting `{"created_at": str, "limits": {"daily_usd": float | null}}`, returning `{"ok": true, "limits": {...}}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard_api.py`:

```python
class KeyLimitsApiTests(unittest.IsolatedAsyncioTestCase):
    def _app(self):
        pool = _pool("sp-team")
        db = MagicMock()
        db.list_proxy_keys = AsyncMock(return_value=[{
            "key": "sp-team-full", "name": "laptop", "active": 1,
            "created_at": "2026-07-30T10:00:00+00:00",
        }])
        db.get_proxy_key_by_created_at = AsyncMock(return_value="sp-team-full")
        limiter = MagicMock()
        limiter.limits_for.return_value = {"daily_usd": 25.0}
        limiter.snapshot.return_value = {"daily_usd": {
            "limit_usd": 25.0, "spent_usd": 18.42, "remaining_usd": 6.58,
            "percent": 73.7, "resets_at": "2026-07-31T00:00:00+02:00",
            "exceeded": False,
        }}
        limiter.set_limit = AsyncMock()
        return {"anthropic_pool": pool, "db": db, "key_limiter": limiter}, db, limiter

    async def test_list_keys_includes_limits_and_usage(self):
        app, _db, _limiter = self._app()
        req = make_mocked_request("GET", "/api/keys", app=app,
                                  headers={"Authorization": "Bearer sp-team"})
        resp = await dashboard_api._api_keys(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["keys"][0]["limits"], {"daily_usd": 25.0})
        self.assertAlmostEqual(body["keys"][0]["usage"]["daily_usd"]["spent_usd"], 18.42)

    async def test_set_limit_persists_and_applies(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": "Bearer sp-team"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"daily_usd": 30.0},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 200)
        limiter.set_limit.assert_awaited_once_with("sp-team-full", "daily_usd", 30.0)

    async def test_null_amount_clears_the_limit(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": "Bearer sp-team"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"daily_usd": None},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 200)
        limiter.set_limit.assert_awaited_once_with("sp-team-full", "daily_usd", None)

    async def test_unknown_kind_is_rejected_before_any_write(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": "Bearer sp-team"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"weekly_bananas": 3},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 400)
        limiter.set_limit.assert_not_awaited()

    async def test_negative_amount_is_rejected_before_any_write(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": "Bearer sp-team"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"daily_usd": -1},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 400)
        limiter.set_limit.assert_not_awaited()

    async def test_unknown_created_at_is_404(self):
        app, db, limiter = self._app()
        db.get_proxy_key_by_created_at = AsyncMock(return_value=None)
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": "Bearer sp-team"})
        req.json = AsyncMock(return_value={
            "created_at": "nope", "limits": {"daily_usd": 1.0}})
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 404)
        limiter.set_limit.assert_not_awaited()

    async def test_requires_action_auth(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": "Bearer sk-ant-passthrough"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00", "limits": {"daily_usd": 1.0}})
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 401)
        limiter.set_limit.assert_not_awaited()
```

`tests/test_dashboard_api.py` does not currently import `json` — add `import json` to its import block.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_dashboard_api.py -v -k KeyLimits`
Expected: FAIL — `AttributeError: module 'smart_proxy.dashboard_api' has no attribute '_api_key_limits'`

- [ ] **Step 3: Extend `_api_keys` and add `_api_key_limits`**

In `src/smart_proxy/dashboard_api.py`, add the import next to the other `smart_proxy` imports:

```python
from smart_proxy.key_limits import LIMIT_KINDS
```

Replace the body of `_api_keys` (~line 142) with:

```python
async def _api_keys(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    limiter = request.app.get("key_limiter")
    rows = await db.list_proxy_keys()
    keys = []
    for r in rows:
        full_key = str(r["key"])
        keys.append({
            "key_prefix": full_key[:12],
            "name": r.get("name") or "",
            "active": bool(r["active"]),
            "created_at": r.get("created_at"),
            "limits": limiter.limits_for(full_key) if limiter else {},
            "usage": limiter.snapshot(full_key) if limiter else {},
        })
    return web.json_response({"keys": keys})
```

Add the new handler directly after `_api_key_active` (~line 493):

```python
async def _api_key_limits(request: web.Request) -> web.Response:
    """Set or clear spend limits on a proxy key.

    Partial update: only the kinds present in ``limits`` are touched. A numeric
    value upserts that kind, an explicit ``null`` clears it (unlimited). The
    whole payload is validated before anything is written.
    """
    if not _action_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    limiter = request.app.get("key_limiter")
    if limiter is None:
        return web.json_response({"error": "limiter unavailable"}, status=500)
    try:
        body = await request.json()
    except Exception:
        body = {}

    created_at = str(body.get("created_at", "")).strip()
    if not created_at:
        return web.json_response({"error": "created_at required"}, status=400)
    raw_limits = body.get("limits")
    if not isinstance(raw_limits, dict) or not raw_limits:
        return web.json_response({"error": "limits object required"}, status=400)

    parsed: dict[str, float | None] = {}
    for kind, raw in raw_limits.items():
        if kind not in LIMIT_KINDS:
            return web.json_response(
                {"error": f"unknown limit kind: {kind}"}, status=400)
        if raw is None:
            parsed[kind] = None
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": f"{kind} must be a number or null"}, status=400)
        if amount < 0:
            return web.json_response({"error": f"{kind} must be >= 0"}, status=400)
        parsed[kind] = amount

    # Identify the key by its exact created_at, not the display prefix: two
    # keys can share leading characters (prefix matching would be ambiguous).
    full_key = await db.get_proxy_key_by_created_at(created_at)
    if full_key is None:
        return web.json_response({"error": "key not found or ambiguous"}, status=404)

    for kind, amount in parsed.items():
        await limiter.set_limit(full_key, kind, amount)
    return web.json_response({"ok": True, "limits": limiter.limits_for(full_key)})
```

- [ ] **Step 4: Reload the limiter from `/api/reload` and register the route**

In `_api_reload`, after the existing `await pool.reload()`, add:

```python
    # Lazy import: anthropic_proxy imports this module at startup.
    from smart_proxy.anthropic_proxy import _resync_limiter

    await _resync_limiter(request.app)
```

Do **not** call `limiter.load()` directly here. `_resync_limiter` (Task 4) flushes buffered usage
before re-seeding; calling `load()` on its own would discard every not-yet-flushed dollar, so each
press of the dashboard's "Reload proxy" button would hand every key back part of its budget.

In `register_dashboard_api`, next to the other key routes (~line 553):

```python
    app.router.add_post("/api/keys/limits", _api_key_limits)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dashboard_api.py tests/test_proxy_key_active.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_api.py
git commit -m "feat(api): expose and edit per-key spend limits"
```

---

### Task 6: Report the limit on `/_oauth_usage?key=`

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` (`_oauth_usage_handler` ~line 2479, new `_inject_smartproxy_limits` helper)
- Test: `tests/test_anthropic_proxy_oauth_usage_endpoint.py` (extend)

**Interfaces:**
- Consumes: `KeyLimiter.snapshot` from Task 3; `AnthropicKeyPool.is_proxy_key` (existing, `anthropic_proxy.py:326`).
- Produces: `_inject_smartproxy_limits(payload: dict, limiter, proxy_key: str) -> dict` — returns a **new** dict; never mutates its argument.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_anthropic_proxy_oauth_usage_endpoint.py`:

```python
class SmartProxyLimitBlockTests(unittest.IsolatedAsyncioTestCase):
    def _limiter(self, limit=25.0):
        limiter = MagicMock()
        limiter.snapshot.return_value = {"daily_usd": {
            "limit_usd": limit, "spent_usd": 18.42, "remaining_usd": 6.58,
            "percent": 73.7, "resets_at": "2026-07-31T00:00:00+02:00",
            "exceeded": False,
        }}
        return limiter

    def test_injects_entry_into_each_usage_limits_array(self):
        payload = {"keys": [{"id": "k1", "usage": {
            "five_hour": {"utilization": 12},
            "limits": [{"kind": "weekly_scoped", "percent": 3}],
        }}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(), "sp-a")
        limits = out["keys"][0]["usage"]["limits"]
        self.assertEqual(len(limits), 2)
        self.assertEqual(limits[0]["kind"], "weekly_scoped")
        self.assertEqual(limits[1], {
            "kind": "smartproxy_daily_usd",
            "source": "smartproxy",
            "limit_usd": 25.0,
            "spent_usd": 18.42,
            "percent": 73.7,
            "resets_at": "2026-07-31T00:00:00+02:00",
        })

    def test_creates_the_limits_array_when_upstream_has_none(self):
        payload = {"keys": [{"id": "k1", "usage": {"five_hour": {"utilization": 12}}}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(), "sp-a")
        self.assertEqual(len(out["keys"][0]["usage"]["limits"]), 1)

    def test_does_not_mutate_the_input_payload(self):
        payload = {"keys": [{"id": "k1", "usage": {"limits": []}}]}
        anthropic_proxy._inject_smartproxy_limits(payload, self._limiter(), "sp-a")
        self.assertEqual(payload["keys"][0]["usage"]["limits"], [])

    def test_unlimited_key_appends_nothing(self):
        payload = {"keys": [{"id": "k1", "usage": {"limits": []}}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(limit=None), "sp-a")
        self.assertEqual(out["keys"][0]["usage"]["limits"], [])

    def test_entry_without_usage_is_left_alone(self):
        payload = {"keys": [{"id": "k1", "error": "no_valid_oauth_token"}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(), "sp-a")
        self.assertEqual(out["keys"][0], {"id": "k1", "error": "no_valid_oauth_token"})
```

Also append this handler-level test, which is the one that catches the cache-leak hazard:

```python
class SmartProxyLimitHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_payload_is_not_poisoned_between_keys(self):
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.side_effect = lambda t: t in ("sp-a", "sp-b")
        limiter = MagicMock()

        def snapshot(key, now=None):
            spent = {"sp-a": 1.0, "sp-b": 2.0}[key]
            return {"daily_usd": {
                "limit_usd": 10.0, "spent_usd": spent, "remaining_usd": 10.0 - spent,
                "percent": spent * 10, "resets_at": "2026-07-31T00:00:00+02:00",
                "exceeded": False,
            }}
        limiter.snapshot.side_effect = snapshot

        app = {
            "anthropic_pool": pool,
            "http_client": MagicMock(),
            "db": MagicMock(),
            "key_limiter": limiter,
            "oauth_usage_cache_seconds": 60,
            "_oauth_usage_cache_lock": asyncio.Lock(),
            "_oauth_usage_cache_entry": None,
        }
        built = [{"id": "k1", "usage": {"limits": []}}]
        with patch.object(
            anthropic_proxy, "_build_oauth_usage_payload",
            AsyncMock(return_value=(built, None)),
        ):
            first = await anthropic_proxy._oauth_usage_handler(
                make_mocked_request("GET", "/_oauth_usage?key=sp-a", app=app))
            second = await anthropic_proxy._oauth_usage_handler(
                make_mocked_request("GET", "/_oauth_usage?key=sp-b", app=app))

        a = json.loads(first.body)["keys"][0]["usage"]["limits"]
        b = json.loads(second.body)["keys"][0]["usage"]["limits"]
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)      # not 2 — the cache must not accumulate
        self.assertEqual(a[0]["spent_usd"], 1.0)
        self.assertEqual(b[0]["spent_usd"], 2.0)   # served from cache, but live

    async def test_no_key_param_leaves_the_payload_untouched(self):
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.return_value = False
        app = {
            "anthropic_pool": pool, "http_client": MagicMock(), "db": MagicMock(),
            "key_limiter": MagicMock(), "oauth_usage_cache_seconds": 0,
        }
        built = [{"id": "k1", "usage": {"limits": []}}]
        with patch.object(
            anthropic_proxy, "_build_oauth_usage_payload",
            AsyncMock(return_value=(built, None)),
        ):
            resp = await anthropic_proxy._oauth_usage_handler(
                make_mocked_request("GET", "/_oauth_usage", app=app))
        self.assertEqual(json.loads(resp.body)["keys"][0]["usage"]["limits"], [])
```

The file already imports `asyncio`, `json`, `unittest`, `AsyncMock`, `MagicMock`, `patch` and
`make_mocked_request`, but it imports **only the names** `AnthropicKeyPool, _oauth_usage_handler`
from `smart_proxy.anthropic_proxy`. The new tests reach for the module itself, so add:

```python
from smart_proxy import anthropic_proxy
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_oauth_usage_endpoint.py -v -k SmartProxy`
Expected: FAIL — `AttributeError: module 'smart_proxy.anthropic_proxy' has no attribute '_inject_smartproxy_limits'`

- [ ] **Step 3: Write the injector**

In `src/smart_proxy/anthropic_proxy.py`, add directly above `async def _oauth_usage_handler` (~line 2479):

```python
def _inject_smartproxy_limits(payload: dict, limiter, proxy_key: str) -> dict:
    """Append this proxy key's SmartProxy limits to each entry's ``usage.limits``.

    Returns a copy — the payload handed in may be the shared /_oauth_usage cache
    entry, which is served to every caller for up to a minute, so mutating it
    would leak one key's spend to another.
    """
    entries = [
        {
            "kind": f"smartproxy_{kind}",
            "source": "smartproxy",
            "limit_usd": info["limit_usd"],
            "spent_usd": info["spent_usd"],
            "percent": info["percent"],
            "resets_at": info["resets_at"],
        }
        for kind, info in limiter.snapshot(proxy_key).items()
        if info.get("limit_usd")
    ]
    if not entries:
        return payload

    out = dict(payload)
    new_keys = []
    for entry in payload.get("keys") or []:
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            new_keys.append(entry)
            continue
        new_usage = dict(usage)
        new_usage["limits"] = list(usage.get("limits") or []) + entries
        new_entry = dict(entry)
        new_entry["usage"] = new_usage
        new_keys.append(new_entry)
    out["keys"] = new_keys
    return out


def _smartproxy_limit_key(request: web.Request) -> str:
    """The ``?key=`` proxy key whose limits should be reported, or ''."""
    candidate = request.query.get("key", "").strip()
    if not candidate:
        return ""
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    return candidate if pool.is_proxy_key(candidate) else ""
```

- [ ] **Step 4: Call it from both branches of `_oauth_usage_handler`**

In `_oauth_usage_handler`, add just after the `include_inactive_oauth = ...` assignment:

```python
    limiter = request.app.get("key_limiter")
    limit_key = _smartproxy_limit_key(request) if limiter is not None else ""

    def _finalize(built: dict) -> dict:
        return _inject_smartproxy_limits(built, limiter, limit_key) if limit_key else built
```

In the `ttl <= 0` branch, change the final line from `return web.json_response(payload)` to:

```python
        return web.json_response(_finalize(payload))
```

In the cached branch, change:

```python
            payload = dict(entry["payload"])
            payload["served_from_cache"] = True
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

to:

```python
            payload = dict(entry["payload"])
            payload["served_from_cache"] = True
            body = json.dumps(
                _finalize(payload), separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
```

and the fresh-build line at the end of the locked block from:

```python
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

to:

```python
        body = json.dumps(
            _finalize(payload), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
```

The cache entry assignment (`request.app["_oauth_usage_cache_entry"] = {... "payload": dict(payload)}`) stays exactly where it is — **above** this line — so what gets cached is the un-injected payload.

Finally, in the auth check at the top of the handler, accept `?key=` as a credential the way the usage dashboard does. Change:

```python
    if request.app.get("oauth_usage_require_auth") and not pool.check_auth(
        _extract_client_token(request)
    ):
```

to:

```python
    if request.app.get("oauth_usage_require_auth") and not pool.check_auth(
        _extract_client_token(request) or request.query.get("key", "").strip()
    ):
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_oauth_usage_endpoint.py -v`
Expected: PASS (all, including the pre-existing cache tests)

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_proxy_oauth_usage_endpoint.py
git commit -m "feat(oauth-usage): report SmartProxy key limits on ?key="
```

---

### Task 7: Keys tab — limits column and edit form

**Files:**
- Modify: `web/src/views/KeysView.svelte` (full rewrite of the file)

**Interfaces:**
- Consumes: `GET /api/keys` (`limits`, `usage` fields) and `POST /api/keys/limits` from Task 5.
- Produces: nothing consumed by later tasks.

This project has no frontend test runner, so verification is a build plus a manual check.

- [ ] **Step 1: Rewrite `KeysView.svelte`**

Replace the entire contents of `web/src/views/KeysView.svelte` with:

```svelte
<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet, apiPost } from '../lib/api'

  // One entry per supported limit kind. Adding a kind later is one entry here
  // plus one in LIMIT_KINDS on the server — the form and table adapt.
  const LIMIT_KINDS = [
    { id: 'daily_usd', label: '24h spend limit', unit: '$', short: '24h' },
  ] as const

  type LimitInfo = {
    limit_usd: number | null
    spent_usd: number
    remaining_usd: number | null
    percent: number | null
    resets_at: string
    exceeded: boolean
  }
  type Key = {
    key_prefix: string
    name: string
    active: boolean
    created_at: string | null
    limits: Record<string, number>
    usage: Record<string, LimitInfo>
  }

  let keys = $state<Key[]>([])
  let reloadMsg = $state('')
  let error = $state('')
  let busy = $state(false)
  let newName = $state('')
  let createdKey = $state('')
  let copied = $state(false)
  let editing = $state<string | null>(null)          // created_at of the open form
  let draft = $state<Record<string, string>>({})     // kind id -> raw input value

  async function load() {
    try { keys = (await apiGet<{ keys: Key[] }>('/api/keys')).keys }
    catch (e) { error = String(e) }
  }
  async function create(e: Event) {
    e.preventDefault()
    error = ''
    const name = newName.trim()
    if (!name) return
    busy = true
    copied = false
    try {
      const r = await apiPost<{ key: string; name: string }>('/api/keys', { name })
      createdKey = r.key       // full key — shown once, it's immediately usable
      newName = ''
      await load()
    } catch (e) { error = String(e) }
    finally { busy = false }
  }
  async function copyKey() {
    try { await navigator.clipboard.writeText(createdKey); copied = true }
    catch { copied = false }
  }
  async function toggle(k: Key) {
    error = ''
    busy = true
    try {
      // Identify by created_at, not key_prefix: distinct keys can share the
      // same 12-char prefix (prefix matching would be ambiguous on the server).
      await apiPost('/api/keys/active', { created_at: k.created_at, active: !k.active })
      await load()
    } catch (e) { error = String(e) }
    finally { busy = false }
  }
  async function reload() {
    error = ''
    reloadMsg = ''
    busy = true
    try {
      const r = await apiPost<{ status: string; active: number }>('/api/reload')
      reloadMsg = `${r.status} — ${r.active} active`
    } catch (e) { error = String(e) }
    finally { busy = false }
  }

  function openEdit(k: Key) {
    editing = k.created_at
    const next: Record<string, string> = {}
    for (const kind of LIMIT_KINDS) {
      const v = k.limits?.[kind.id]
      next[kind.id] = v == null ? '' : String(v)
    }
    draft = next
  }
  function closeEdit() {
    editing = null
    draft = {}
  }
  async function saveLimits(k: Key) {
    error = ''
    busy = true
    try {
      // Blank input means "no limit" → null clears the row server-side.
      // draft is declared Record<string, string>, but Svelte's number-input
      // binding coerces the bound value to a number (or null) at runtime, so
      // don't trust the declared type here — coerce to string before trim.
      const limits: Record<string, number | null> = {}
      for (const kind of LIMIT_KINDS) {
        const value = draft[kind.id]
        const raw = (value == null ? '' : String(value)).trim()
        limits[kind.id] = raw === '' ? null : Number(raw)
      }
      const bad = LIMIT_KINDS.find(
        (kind) => limits[kind.id] !== null && !Number.isFinite(limits[kind.id] as number))
      if (bad) {
        error = `${bad.label} must be a number`
        return
      }
      await apiPost('/api/keys/limits', { created_at: k.created_at, limits })
      closeEdit()
      await load()
    } catch (e) { error = String(e) }
    finally { busy = false }
  }

  function money(v: number | null | undefined): string {
    return v == null ? '—' : `$${v.toFixed(2)}`
  }
  function limitSummary(k: Key): string {
    const parts = LIMIT_KINDS
      .filter((kind) => k.limits?.[kind.id] != null)
      .map((kind) => `${money(k.limits[kind.id])} / ${kind.short}`)
    return parts.length ? parts.join(', ') : '—'
  }
  function spentSummary(k: Key): string {
    const info = k.usage?.['daily_usd']
    if (!info) return '—'
    const pct = info.percent == null ? '' : ` · ${Math.round(info.percent)}%`
    return `${money(info.spent_usd)}${pct}`
  }
  function resetsIn(k: Key): string {
    const iso = k.usage?.['daily_usd']?.resets_at
    if (!iso) return '—'
    const secs = (new Date(iso).getTime() - Date.now()) / 1000
    if (!Number.isFinite(secs) || secs <= 0) return 'now'
    const h = Math.floor(secs / 3600)
    const m = Math.floor((secs % 3600) / 60)
    return h ? `${h}h ${m}m` : `${m}m`
  }

  onMount(load)
</script>

<form class="create" onsubmit={create}>
  <input placeholder="new key name" bind:value={newName} disabled={busy} />
  <button class="primary" type="submit" disabled={busy || !newName.trim()}>Create key</button>
</form>

{#if createdKey}
  <div class="created">
    <span>New key — copy now, it won't be shown again (already active):</span>
    <code>{createdKey}</code>
    <button type="button" onclick={copyKey}>{copied ? 'Copied ✓' : 'Copy'}</button>
    <button type="button" onclick={() => { createdKey = ''; copied = false }}>Dismiss</button>
  </div>
{/if}

<div class="toolbar">
  <button class="primary" type="button" onclick={reload} disabled={busy}>Reload proxy</button>
  <span>{reloadMsg}</span>
</div>

{#if error}<p class="err">{error}</p>{/if}

<table>
  <thead>
    <tr>
      <th>Key</th><th>Name</th><th>Active</th>
      <th>Limits</th><th>Spent</th><th>Resets in</th><th></th>
    </tr>
  </thead>
  <tbody>
    {#each keys as k (k.created_at)}
      <tr class:over={k.usage?.['daily_usd']?.exceeded}>
        <td>{k.key_prefix}…</td>
        <td>{k.name}</td>
        <td>{k.active ? 'yes' : 'no'}</td>
        <td>{limitSummary(k)}</td>
        <td>{spentSummary(k)}</td>
        <td>{resetsIn(k)}</td>
        <td>
          <button type="button" onclick={() => openEdit(k)} disabled={busy}>Limits</button>
          <button type="button" onclick={() => toggle(k)} disabled={busy}>
            {k.active ? 'Disable' : 'Enable'}
          </button>
        </td>
      </tr>
      {#if editing === k.created_at}
        <tr class="limit-form">
          <td colspan="7">
            <form onsubmit={(e) => { e.preventDefault(); saveLimits(k) }}>
              {#each LIMIT_KINDS as kind}
                <label>
                  {kind.label} ({kind.unit})
                  <input
                    type="number" min="0" step="0.01" placeholder="unlimited"
                    bind:value={draft[kind.id]} disabled={busy} />
                </label>
              {/each}
              <span class="hint">empty = unlimited</span>
              <button class="primary" type="submit" disabled={busy}>Save</button>
              <button type="button" onclick={closeEdit} disabled={busy}>Cancel</button>
            </form>
          </td>
        </tr>
      {/if}
    {/each}
  </tbody>
</table>

<style>
  tr.over td { color: #b00020; }
  tr.limit-form td { background: rgba(127, 127, 127, 0.08); }
  tr.limit-form form { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
  tr.limit-form label { display: flex; align-items: center; gap: 0.4rem; }
  tr.limit-form input { width: 8rem; }
  tr.limit-form .hint { opacity: 0.6; font-size: 0.85em; }
</style>
```

- [ ] **Step 2: Build the SPA to verify it compiles**

Run: `cd web && npm ci && npm run build`
Expected: build succeeds, output written to `src/smart_proxy/static/app/`

- [ ] **Step 3: Manual check**

Start the proxy locally (`.venv/bin/python -m smart_proxy anthropic-proxy`), open `/_app/#keys`, and confirm: the table shows `—` under Limits for every key; **Limits** opens the inline form; saving `1` shows `$1.00 / 24h`; saving an empty value returns the row to `—`.

- [ ] **Step 4: Commit**

```bash
git add web/src/views/KeysView.svelte
git commit -m "feat(dashboard): spend-limit column and edit form on the Keys tab"
```

---

### Task 8: Statusline rendering and documentation

**Files:**
- Modify: `web/src/lib/statusline.sh` (~line 66, the `limits[]` loop)
- Modify: `README.md`

**Interfaces:**
- Consumes: the `smartproxy_daily_usd` entry from Task 6.
- Produces: nothing.

- [ ] **Step 1: Render the SmartProxy limit in the statusline**

In `web/src/lib/statusline.sh`, replace the `for lim in usage.get("limits") or []:` loop body so it
handles both kinds instead of skipping everything that is not `weekly_scoped`:

```python
                for lim in usage.get("limits") or []:
                    kind = lim.get("kind")
                    if kind == "smartproxy_daily_usd":
                        pct = round(lim.get("percent") or 0)
                        spent = lim.get("spent_usd") or 0
                        cap = lim.get("limit_usd") or 0
                        lim_reset = fmt_reset(lim["resets_at"])
                        parts.append(f"24h: {pct}% (${spent:.2f}/${cap:.0f} ↺ {lim_reset})")
                        continue
                    if kind != "weekly_scoped":
                        continue
                    scope = lim.get("scope") or {}
                    name = ((scope.get("model") or {}).get("display_name")
                            or scope.get("surface") or "scoped")
                    lim_reset = fmt_reset(lim["resets_at"])
                    lim_pct = round(lim["percent"])
                    parts.append(f"{name} 7d: {lim_pct}% (↺ {lim_reset})")
```

Note the statusline fetches `__PROXY_ORIGIN__/_oauth_usage` **without** `?key=`, so this branch only
renders once a user appends their key to that URL in their own copy of the script. Leave the fetch
URL alone — changing it would embed a key in a shared template.

- [ ] **Step 2: Document the feature in the README**

In `README.md`, in the paragraph describing the dashboard tabs (the one starting "The operator
dashboard (usage, OAuth quotas, compat stats, key toggles…"), add after that paragraph:

```markdown
The **Keys** tab also manages **per-key spend limits**: each `sp-*` key can carry a 24h USD cap
(blank = unlimited, which is the default for every key). The window resets at local midnight in
`ANTHROPIC_PROXY_LIMIT_WINDOW_TZ` (default `Europe/Paris`). Once a key is over its cap the proxy
answers `POST /v1/messages` with a 429 —
`SmartProxy: you have reached your 24h limit. Retry in 5h 12m` — until the window resets; editing
the limit in the UI takes effect on the next request, with no restart. `GET /_oauth_usage?key=sp-…`
reports the same numbers as a `smartproxy_daily_usd` entry inside each key's `usage.limits[]`.
```

- [ ] **Step 3: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (no regressions)

- [ ] **Step 4: Commit**

```bash
git add web/src/lib/statusline.sh README.md
git commit -m "docs: document per-key spend limits and render them in the statusline"
```

---

## Deployment notes for whoever ships this

1. `git pull && .venv/bin/pip install -e .`
2. `.venv/bin/python -m smart_proxy db migrate` — creates two new tables only. No lock is taken on
   `usage_daily` / `usage_session`, so writers do **not** need to be stopped first.
3. `cd web && npm ci && npm run build` (the SPA build output is gitignored and must be produced on
   the host or rsynced over).
4. Optionally set `ANTHROPIC_PROXY_LIMIT_WINDOW_TZ` in `.env`.
5. `systemctl restart smart-proxy`.

Hourly buckets only start filling from the first flush after the restart, so on deploy day a key's
window counts from the restart rather than from midnight. Expected, not a bug.
