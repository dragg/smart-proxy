# Per-Window OAuth Token Usage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Exact per-window token accounting for OAuth keys: accumulate actual per-model token counters for each observed rate-limit window instance, and expose them (with cost) on `/_oauth_usage_history`.

**Architecture:** Two new tables — `oauth_window_usage` (counters per `(oauth_window_log.id, model)`) and `oauth_window_usage_pending` (tokens that arrive after a window expired but before the next poll observes its successor). Attribution happens on the existing 60-second `UsageTracker` flush in `anthropic_proxy`; pending drains into the new window row inside `record_oauth_window_observations`. Spec: `docs/superpowers/specs/2026-07-12-per-window-token-usage-design.md`.

**Tech Stack:** Python 3 / aiohttp / aiosqlite (+ PostgreSQL via `db_postgres.py`), unittest-style tests run with pytest.

## Global Constraints

- All new SQL must run on BOTH backends: sqlite (`?` placeholders, translated to `%s` for psycopg) and PostgreSQL. Upsert-increment must follow the `build_usage_upsert_sql` pattern: `ON CONFLICT(...) DO UPDATE SET col = {qualifier}col + excluded.col` where `qualifier` is `"<table>."` for postgres and `""` for sqlite (`src/smart_proxy/db.py:586`).
- Canonical counter order everywhere (tuples, SQL column lists, dict keys):
  `input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests`.
  This matches `UsageTracker` buffer indices 0–7 and `usage_daily` upsert row tail (indices 7–14 of the row tuple).
- Migrations are additive only: sqlite gets `CREATE TABLE IF NOT EXISTS` in both `SCHEMA_SQL` and `MIGRATIONS`; postgres gets a new named entry `0005_oauth_window_usage` in `POSTGRES_MIGRATIONS`.
- Attribution must never break the flush path: exceptions from `attribute_oauth_window_usage` are caught and logged in the proxy helper, not propagated.
- Tests: `uv run python -m pytest tests/<file> -q` from the repo root. Commit after every task, conventional-commit style (`feat(db): ...`, `feat(anthropic-proxy): ...`).

---

### Task 1: Schema — two new tables on both backends + snapshot specs

**Files:**
- Modify: `src/smart_proxy/db.py` (SCHEMA_SQL block ends near line 262; MIGRATIONS list starts line 262; SNAPSHOT_TABLE_SPECS / SNAPSHOT_DELETE_ORDER near lines 539–580)
- Modify: `src/smart_proxy/db_migrations.py` (POSTGRES_MIGRATIONS tuple, append after `0004_usage_via_openai_compat`)
- Test: `tests/test_oauth_window_usage_db.py` (create)

**Interfaces:**
- Consumes: existing `Database.connect()` (runs SCHEMA_SQL + idempotent MIGRATIONS on every connect).
- Produces: tables `oauth_window_usage`, `oauth_window_usage_pending`; module constant `WINDOW_USAGE_COUNTERS: tuple[str, ...]` in `smart_proxy.db` (imported by Tasks 2, 4, 5 and tests).

- [ ] **Step 1: Write the failing test**

Create `tests/test_oauth_window_usage_db.py`:

```python
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database, WINDOW_USAGE_COUNTERS


class WindowUsageSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_tables_exist_with_expected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                expected = {
                    "oauth_window_usage":
                        {"window_id", "model", *WINDOW_USAGE_COUNTERS},
                    "oauth_window_usage_pending":
                        {"key_id", "window_kind", "model", "updated_at",
                         *WINDOW_USAGE_COUNTERS},
                }
                for table, columns in expected.items():
                    cur = await db.db.execute(f"PRAGMA table_info({table})")
                    names = {row["name"] for row in await cur.fetchall()}
                    self.assertEqual(names, columns, table)
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py -q`
Expected: FAIL — `ImportError: cannot import name 'WINDOW_USAGE_COUNTERS'`.

- [ ] **Step 3: Implement schema**

3a. In `src/smart_proxy/db.py`, inside `SCHEMA_SQL`, after the `idx_owdl_key` index (just before the closing `"""`), add:

```sql
CREATE TABLE IF NOT EXISTS oauth_window_usage (
    window_id                INTEGER NOT NULL,   -- oauth_window_log.id
    model                    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (window_id, model)
);

CREATE TABLE IF NOT EXISTS oauth_window_usage_pending (
    key_id                   TEXT    NOT NULL,
    window_kind              TEXT    NOT NULL,
    model                    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    updated_at               TEXT    NOT NULL,
    PRIMARY KEY (key_id, window_kind, model)
);
```

3b. Append the same two statements (as separate strings, verbatim `CREATE TABLE IF NOT EXISTS ...`) to the end of the `MIGRATIONS` list in `db.py` (follows the existing pattern where new tables appear in both places).

3c. Near `build_usage_upsert_sql` (module level, before `class Database`), add:

```python
WINDOW_USAGE_COUNTERS: tuple[str, ...] = (
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "cache_creation_5m_tokens",
    "cache_creation_1h_tokens", "web_search_requests", "requests",
)
```

3d. In `SNAPSHOT_TABLE_SPECS`, after the `oauth_window_drop_log` spec, add:

```python
    (
        "oauth_window_usage",
        (
            "window_id", "model", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens",
            "cache_creation_5m_tokens", "cache_creation_1h_tokens",
            "web_search_requests", "requests",
        ),
        "window_id, model",
    ),
    (
        "oauth_window_usage_pending",
        (
            "key_id", "window_kind", "model", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens",
            "cache_creation_5m_tokens", "cache_creation_1h_tokens",
            "web_search_requests", "requests", "updated_at",
        ),
        "key_id, window_kind, model",
    ),
```

3e. In `SNAPSHOT_DELETE_ORDER`, add `"oauth_window_usage", "oauth_window_usage_pending",` as the FIRST two entries (children before `oauth_window_log`).

3f. In `src/smart_proxy/db_migrations.py`, append to `POSTGRES_MIGRATIONS` (after `0004_usage_via_openai_compat`):

```python
    (
        # Per-window token accounting: counters per observed window instance
        # plus a pending bucket for tokens arriving between a window's expiry
        # and the next poll that observes its successor.
        "0005_oauth_window_usage",
        (
            """
            CREATE TABLE IF NOT EXISTS oauth_window_usage (
                window_id                BIGINT NOT NULL,
                model                    TEXT   NOT NULL,
                input_tokens             BIGINT NOT NULL DEFAULT 0,
                output_tokens            BIGINT NOT NULL DEFAULT 0,
                cache_read_tokens        BIGINT NOT NULL DEFAULT 0,
                cache_creation_tokens    BIGINT NOT NULL DEFAULT 0,
                cache_creation_5m_tokens BIGINT NOT NULL DEFAULT 0,
                cache_creation_1h_tokens BIGINT NOT NULL DEFAULT 0,
                web_search_requests      BIGINT NOT NULL DEFAULT 0,
                requests                 BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (window_id, model)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS oauth_window_usage_pending (
                key_id                   TEXT   NOT NULL,
                window_kind              TEXT   NOT NULL,
                model                    TEXT   NOT NULL,
                input_tokens             BIGINT NOT NULL DEFAULT 0,
                output_tokens            BIGINT NOT NULL DEFAULT 0,
                cache_read_tokens        BIGINT NOT NULL DEFAULT 0,
                cache_creation_tokens    BIGINT NOT NULL DEFAULT 0,
                cache_creation_5m_tokens BIGINT NOT NULL DEFAULT 0,
                cache_creation_1h_tokens BIGINT NOT NULL DEFAULT 0,
                web_search_requests      BIGINT NOT NULL DEFAULT 0,
                requests                 BIGINT NOT NULL DEFAULT 0,
                updated_at               TEXT   NOT NULL,
                PRIMARY KEY (key_id, window_kind, model)
            )
            """,
        ),
    ),
```

(No `_after_replace_snapshot` change in `db_postgres.py` — the new tables have composite PKs, no id sequence.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py tests/test_db_snapshot_roundtrip.py tests/test_anthropic_db_contract.py -q`
Expected: PASS (snapshot/contract tests confirm the spec lists stay coherent).

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py src/smart_proxy/db_migrations.py tests/test_oauth_window_usage_db.py
git commit -m "feat(db): oauth_window_usage + pending tables on both backends"
```

---

### Task 2: `attribute_oauth_window_usage` + read methods

**Files:**
- Modify: `src/smart_proxy/db.py` (module-level helpers near `build_usage_upsert_sql`; methods on `Database` next to `record_oauth_window_observations` / `list_oauth_window_log`, near line 1839)
- Test: `tests/test_oauth_window_usage_db.py`

**Interfaces:**
- Consumes: Task 1 tables and `WINDOW_USAGE_COUNTERS`; existing `oauth_window_log` rows (`id`, `window_kind`, `resets_at`, `resets_at_raw`); existing `_parse`-style helpers pattern.
- Produces (used by Tasks 3–6):
  - `async def attribute_oauth_window_usage(self, deltas: list[dict], *, now: str | None = None) -> None` — each delta: `{"key_id": str, "model": str, <eight counters>: int}`.
  - `async def list_oauth_window_usage(self, key_id: str) -> list[dict]` — rows with `window_id`, `model`, eight counters.
  - `async def list_oauth_window_usage_pending(self, key_id: str) -> list[dict]` — rows with `key_id`, `window_kind`, `model`, eight counters, `updated_at`.
  - Module functions `build_window_usage_upsert_sql(backend: str) -> str`, `build_window_pending_upsert_sql(backend: str) -> str`, `_parse_iso_utc(raw: str | None) -> datetime | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_window_usage_db.py` (module level, below the imports):

```python
KEY_ID = "oauth-key-1"


def _minute_iso(dt: datetime) -> str:
    return dt.replace(second=0, microsecond=0).isoformat()


def _delta(key_id: str = KEY_ID, model: str = "claude-sonnet-5", **over: int) -> dict:
    base = {
        "key_id": key_id, "model": model,
        "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 10,
        "cache_creation_tokens": 5, "cache_creation_5m_tokens": 5,
        "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 2,
    }
    base.update(over)
    return base


async def _insert_key(db: Database, key_id: str = KEY_ID) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.db.execute(
        """INSERT INTO anthropic_keys
           (id, key_type, status, access_token, refresh_token,
            expires_at, name, created_at, updated_at)
           VALUES (?, 'oauth', 'active', 'tok', 'ref',
                   9999999999999, 't', ?, ?)""",
        (key_id, now, now),
    )
    await db.db.commit()


async def _observe(
    db: Database, key_id: str, kind: str, resets_at_dt: datetime,
    utilization: float = 50.0,
) -> str:
    resets_at = _minute_iso(resets_at_dt)
    await db.record_oauth_window_observations(key_id, [{
        "window_kind": kind,
        "resets_at": resets_at,
        "resets_at_raw": resets_at_dt.isoformat(),
        "utilization": utilization,
    }])
    return resets_at


class AttributeWindowUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._td.name) / "t.db"))
        await self.db.connect()
        await _insert_key(self.db)
        self.now = datetime.now(timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._td.cleanup()

    async def test_attributes_to_live_window_and_accumulates(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        await self.db.attribute_oauth_window_usage([_delta()])
        await self.db.attribute_oauth_window_usage([_delta()])
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "claude-sonnet-5")
        self.assertEqual(rows[0]["input_tokens"], 200)
        self.assertEqual(rows[0]["requests"], 4)
        self.assertEqual(
            await self.db.list_oauth_window_usage_pending(KEY_ID), [])

    async def test_expired_window_goes_to_pending(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        late = (self.now + timedelta(hours=2)).isoformat()
        await self.db.attribute_oauth_window_usage([_delta()], now=late)
        self.assertEqual(await self.db.list_oauth_window_usage(KEY_ID), [])
        pending = await self.db.list_oauth_window_usage_pending(KEY_ID)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["window_kind"], "seven_day")
        self.assertEqual(pending[0]["input_tokens"], 100)
        self.assertEqual(pending[0]["updated_at"], late)

    async def test_unobserved_kind_is_skipped(self) -> None:
        await self.db.attribute_oauth_window_usage([_delta()])
        self.assertEqual(await self.db.list_oauth_window_usage(KEY_ID), [])
        self.assertEqual(
            await self.db.list_oauth_window_usage_pending(KEY_ID), [])

    async def test_all_kinds_receive_same_deltas(self) -> None:
        await _observe(self.db, KEY_ID, "five_hour",
                       self.now + timedelta(hours=1))
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(days=3))
        await self.db.attribute_oauth_window_usage([_delta()])
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["input_tokens"] for r in rows}, {100})

    async def test_all_zero_delta_is_ignored(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        zero = _delta(**{c: 0 for c in WINDOW_USAGE_COUNTERS})
        await self.db.attribute_oauth_window_usage([zero])
        self.assertEqual(await self.db.list_oauth_window_usage(KEY_ID), [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py -q`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'attribute_oauth_window_usage'`.

- [ ] **Step 3: Implement**

3a. In `src/smart_proxy/db.py`, after `WINDOW_USAGE_COUNTERS`, add the SQL builders (same qualifier pattern as `build_usage_upsert_sql`):

```python
def _window_counter_updates(table: str, backend: str) -> str:
    qualifier = f"{table}." if backend == "postgres" else ""
    return ", ".join(
        f"{c} = {qualifier}{c} + excluded.{c}" for c in WINDOW_USAGE_COUNTERS
    )


def build_window_usage_upsert_sql(backend: str) -> str:
    cols = ", ".join(WINDOW_USAGE_COUNTERS)
    return (
        f"INSERT INTO oauth_window_usage (window_id, model, {cols}) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        f"ON CONFLICT(window_id, model) DO UPDATE SET "
        f"{_window_counter_updates('oauth_window_usage', backend)}"
    )


def build_window_pending_upsert_sql(backend: str) -> str:
    cols = ", ".join(WINDOW_USAGE_COUNTERS)
    return (
        f"INSERT INTO oauth_window_usage_pending "
        f"(key_id, window_kind, model, {cols}, updated_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        f"ON CONFLICT(key_id, window_kind, model) DO UPDATE SET "
        f"{_window_counter_updates('oauth_window_usage_pending', backend)}, "
        f"updated_at = excluded.updated_at"
    )
```

3b. Next to the existing `_iso_minutes_between` helper, add:

```python
def _parse_iso_utc(raw: str | None) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
```

3c. On `Database`, after `list_oauth_window_drops`, add:

```python
    async def attribute_oauth_window_usage(
        self,
        deltas: list[dict],
        *,
        now: str | None = None,
    ) -> None:
        """Attribute per-(key, model) usage deltas to observed rate-limit windows.

        For the latest observed window of every kind of ``delta["key_id"]``:
        still live -> increment ``oauth_window_usage``; expired -> increment
        ``oauth_window_usage_pending`` (drained into the next observed window
        by ``record_oauth_window_observations``); kind never observed -> the
        delta is skipped and remains visible in ``usage_daily`` only.
        """
        if not deltas:
            return
        now_iso = now or datetime.now(timezone.utc).isoformat()
        now_dt = _parse_iso_utc(now_iso)
        usage_sql = build_window_usage_upsert_sql(self._backend)
        pending_sql = build_window_pending_upsert_sql(self._backend)
        latest_by_key: dict[str, list[dict]] = {}
        changed = False
        for delta in deltas:
            key_id = delta["key_id"]
            if key_id not in latest_by_key:
                cur = await self.db.execute(
                    """SELECT w.id, w.window_kind, w.resets_at, w.resets_at_raw
                       FROM oauth_window_log w
                       JOIN (SELECT window_kind, MAX(resets_at) AS max_resets_at
                             FROM oauth_window_log
                             WHERE key_id = ?
                             GROUP BY window_kind) latest
                         ON latest.window_kind = w.window_kind
                        AND latest.max_resets_at = w.resets_at
                       WHERE w.key_id = ?""",
                    (key_id, key_id),
                )
                latest_by_key[key_id] = [dict(r) for r in await cur.fetchall()]
            counters = tuple(int(delta.get(c, 0)) for c in WINDOW_USAGE_COUNTERS)
            if not any(counters):
                continue
            for win in latest_by_key[key_id]:
                end = (
                    _parse_iso_utc(win["resets_at_raw"])
                    or _parse_iso_utc(win["resets_at"])
                )
                if end is not None and now_dt is not None and now_dt <= end:
                    await self.db.execute(
                        usage_sql, (win["id"], delta["model"], *counters)
                    )
                else:
                    await self.db.execute(
                        pending_sql,
                        (key_id, win["window_kind"], delta["model"],
                         *counters, now_iso),
                    )
                changed = True
        if changed:
            await self.db.commit()

    async def list_oauth_window_usage(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT u.window_id, u.model, u.input_tokens, u.output_tokens,
                      u.cache_read_tokens, u.cache_creation_tokens,
                      u.cache_creation_5m_tokens, u.cache_creation_1h_tokens,
                      u.web_search_requests, u.requests
               FROM oauth_window_usage u
               JOIN oauth_window_log w ON w.id = u.window_id
               WHERE w.key_id = ?
               ORDER BY u.window_id ASC, u.model ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def list_oauth_window_usage_pending(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT key_id, window_kind, model, input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens,
                      cache_creation_5m_tokens, cache_creation_1h_tokens,
                      web_search_requests, requests, updated_at
               FROM oauth_window_usage_pending
               WHERE key_id = ?
               ORDER BY window_kind ASC, model ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_oauth_window_usage_db.py
git commit -m "feat(db): attribute usage deltas to oauth windows with pending bucket"
```

---

### Task 3: Drain pending into newly observed windows

**Files:**
- Modify: `src/smart_proxy/db.py` — `record_oauth_window_observations` (near line 1839; the new-row `INSERT INTO oauth_window_log` is near line 1934) + new private method `_drain_window_pending`
- Test: `tests/test_oauth_window_usage_db.py`

**Interfaces:**
- Consumes: Task 2 (`build_window_usage_upsert_sql`, `WINDOW_USAGE_COUNTERS`, pending table).
- Produces: behavior only — after `record_oauth_window_observations` inserts a NEW window row for `(key_id, window_kind)`, all pending rows of that pair are moved into `oauth_window_usage` under the new window id and deleted. No public API change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_window_usage_db.py`:

```python
class PendingDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self._td.name) / "t.db"))
        await self.db.connect()
        await _insert_key(self.db)
        self.now = datetime.now(timezone.utc)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._td.cleanup()

    async def test_pending_drains_into_new_window(self) -> None:
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        late = (self.now + timedelta(hours=2)).isoformat()
        await self.db.attribute_oauth_window_usage([_delta()], now=late)
        self.assertEqual(
            len(await self.db.list_oauth_window_usage_pending(KEY_ID)), 1)

        new_resets = await _observe(self.db, KEY_ID, "seven_day",
                                    self.now + timedelta(days=7))
        self.assertEqual(
            await self.db.list_oauth_window_usage_pending(KEY_ID), [])
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 100)
        logs = await self.db.list_oauth_window_log(KEY_ID)
        new_row = next(r for r in logs if r["resets_at"] == new_resets)
        self.assertEqual(rows[0]["window_id"], new_row["id"])

    async def test_drain_only_moves_matching_kind(self) -> None:
        await _observe(self.db, KEY_ID, "five_hour",
                       self.now + timedelta(hours=1))
        await _observe(self.db, KEY_ID, "seven_day",
                       self.now + timedelta(hours=1))
        late = (self.now + timedelta(hours=2)).isoformat()
        await self.db.attribute_oauth_window_usage([_delta()], now=late)
        self.assertEqual(
            len(await self.db.list_oauth_window_usage_pending(KEY_ID)), 2)

        await _observe(self.db, KEY_ID, "five_hour",
                       self.now + timedelta(hours=7))
        pending = await self.db.list_oauth_window_usage_pending(KEY_ID)
        self.assertEqual([p["window_kind"] for p in pending], ["seven_day"])

    async def test_reobservation_of_same_window_does_not_duplicate(self) -> None:
        resets_dt = self.now + timedelta(hours=1)
        await _observe(self.db, KEY_ID, "seven_day", resets_dt)
        await self.db.attribute_oauth_window_usage([_delta()])
        await _observe(self.db, KEY_ID, "seven_day", resets_dt,
                       utilization=60.0)  # jittered duplicate, merged
        rows = await self.db.list_oauth_window_usage(KEY_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["input_tokens"], 100)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py -q`
Expected: `test_pending_drains_into_new_window` and `test_drain_only_moves_matching_kind` FAIL (pending rows are never drained); the reobservation test PASSES already.

- [ ] **Step 3: Implement**

3a. In `record_oauth_window_observations`, immediately after the `INSERT INTO oauth_window_log` for a new row (after its `await self.db.execute(...)` call and before the `if prev:` block), add:

```python
            await self._drain_window_pending(key_id, kind, resets_at)
```

3b. Add the method to `Database` (next to `attribute_oauth_window_usage`):

```python
    async def _drain_window_pending(
        self, key_id: str, window_kind: str, resets_at: str
    ) -> None:
        """Move pending usage for (key, kind) into the just-inserted window row."""
        cur = await self.db.execute(
            "SELECT id FROM oauth_window_log "
            "WHERE key_id = ? AND window_kind = ? AND resets_at = ?",
            (key_id, window_kind, resets_at),
        )
        row = await cur.fetchone()
        if row is None:
            return
        window_id = row["id"]
        cur = await self.db.execute(
            """SELECT model, input_tokens, output_tokens, cache_read_tokens,
                      cache_creation_tokens, cache_creation_5m_tokens,
                      cache_creation_1h_tokens, web_search_requests, requests
               FROM oauth_window_usage_pending
               WHERE key_id = ? AND window_kind = ?""",
            (key_id, window_kind),
        )
        pending = await cur.fetchall()
        if not pending:
            return
        usage_sql = build_window_usage_upsert_sql(self._backend)
        for p in pending:
            await self.db.execute(
                usage_sql,
                (window_id, p["model"],
                 *(p[c] for c in WINDOW_USAGE_COUNTERS)),
            )
        await self.db.execute(
            "DELETE FROM oauth_window_usage_pending "
            "WHERE key_id = ? AND window_kind = ?",
            (key_id, window_kind),
        )
```

(No separate commit call — `record_oauth_window_observations` commits at the end, the drain rides that transaction.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py tests/test_oauth_window_tracking_e2e.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_oauth_window_usage_db.py
git commit -m "feat(db): drain pending window usage into newly observed windows"
```

---

### Task 4: `UsageTracker.flush` returns rows; anthropic-proxy attribution wiring

**Files:**
- Modify: `src/smart_proxy/usage.py` — `UsageTracker.flush` (near line 630)
- Modify: `src/smart_proxy/anthropic_proxy.py` — `_usage_flush_loop` (line 2698), cleanup hook (`await tracker.flush(db)` near line 2975), new helpers `_window_usage_deltas` / `_flush_usage`, import `WINDOW_USAGE_COUNTERS`
- Modify: `tests/test_anthropic_cache_usage.py:401` — `self.assertEqual(flushed, 2)` → `self.assertEqual(len(flushed), 2)`
- Test: `tests/test_oauth_window_usage_db.py`

**Interfaces:**
- Consumes: Task 2 `attribute_oauth_window_usage`; `WINDOW_USAGE_COUNTERS` from `smart_proxy.db`.
- Produces:
  - `UsageTracker.flush(db) -> list[tuple]` — returns the flushed `usage_daily` row tuples `(date, proxy_key, group_name, credential_id, provider, model, via_openai_compat, <eight counters>)`. Empty list when nothing buffered. (`scheduler.py` call sites ignore the return value — no change there.)
  - `_window_usage_deltas(rows: list[tuple]) -> list[dict]` in `anthropic_proxy` — anthropic-only, aggregated by `(credential_id, model)`.
  - `async def _flush_usage(tracker: UsageTracker, db: Database) -> int` in `anthropic_proxy` — flush + attribute, returns flushed row count; attribution errors logged, never raised.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_oauth_window_usage_db.py`:

```python
from smart_proxy.anthropic_proxy import _flush_usage, _window_usage_deltas
from smart_proxy.usage import UsageTracker


class FlushAttributionTests(unittest.IsolatedAsyncioTestCase):
    def test_window_usage_deltas_aggregates_anthropic_rows(self) -> None:
        rows = [
            ("2026-07-12", "sp-a", None, "k1", "anthropic", "claude-sonnet-5",
             0, 10, 5, 0, 0, 0, 0, 0, 1),
            ("2026-07-12", "sp-b", None, "k1", "anthropic", "claude-sonnet-5",
             1, 30, 15, 2, 0, 0, 0, 0, 2),
            ("2026-07-12", "sp-a", None, "k1", "openai", "gpt-x",
             0, 99, 99, 0, 0, 0, 0, 0, 9),
        ]
        deltas = _window_usage_deltas(rows)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["key_id"], "k1")
        self.assertEqual(deltas[0]["model"], "claude-sonnet-5")
        self.assertEqual(deltas[0]["input_tokens"], 40)
        self.assertEqual(deltas[0]["output_tokens"], 20)
        self.assertEqual(deltas[0]["cache_read_tokens"], 2)
        self.assertEqual(deltas[0]["requests"], 3)

    async def test_flush_usage_lands_on_live_window(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                await _insert_key(db)
                await _observe(db, KEY_ID, "seven_day",
                               datetime.now(timezone.utc) + timedelta(days=3))
                tracker = UsageTracker()
                tracker.record("sp-a", KEY_ID, "anthropic",
                               "claude-sonnet-5", 100, 50)
                n = await _flush_usage(tracker, db)
                self.assertEqual(n, 1)
                rows = await db.list_oauth_window_usage(KEY_ID)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["input_tokens"], 100)
                self.assertEqual(rows[0]["requests"], 1)
            finally:
                await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py -q`
Expected: FAIL — `ImportError: cannot import name '_flush_usage'`.

- [ ] **Step 3: Implement**

3a. `src/smart_proxy/usage.py` — change `flush` to return the rows (docstring included):

```python
    async def flush(self, db: object) -> list[tuple]:
        """Move buffered data to the database.

        Returns the flushed row tuples in ``usage_daily`` upsert order:
        (date, proxy_key, group_name, credential_id, provider, model,
        via_openai_compat, input, output, cache_read, cache_creation,
        cache_creation_5m, cache_creation_1h, web_search_requests, requests).
        """
        from smart_proxy.db import Database
        assert isinstance(db, Database)

        async with self._lock:
            if not self._buf:
                return []
            snapshot = self._buf
            self._buf = {}

        rows = [
            (
                k[0], k[1], (k[2] or None), k[3], k[4], k[5], k[6],
                v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7],
            )
            for k, v in snapshot.items()
        ]
        await db.upsert_usage_batch(rows)
        logger.debug("Flushed %d usage rows to DB", len(rows))
        return rows
```

3b. `src/smart_proxy/anthropic_proxy.py` — extend the db import at line 45 to
`from smart_proxy.db import Database, WINDOW_USAGE_COUNTERS, build_database_from_config`,
then add above `_usage_flush_loop`:

```python
def _window_usage_deltas(rows: list[tuple]) -> list[dict]:
    """Aggregate flushed usage rows into per-(key, model) window deltas."""
    agg: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        if row[4] != "anthropic":
            continue
        acc = agg.setdefault((row[3], row[5]), [0] * len(WINDOW_USAGE_COUNTERS))
        for i in range(len(WINDOW_USAGE_COUNTERS)):
            acc[i] += int(row[7 + i])
    return [
        {"key_id": key_id, "model": model,
         **dict(zip(WINDOW_USAGE_COUNTERS, counters))}
        for (key_id, model), counters in agg.items()
    ]


async def _flush_usage(tracker: UsageTracker, db: Database) -> int:
    """Flush buffered usage; attribute the deltas to OAuth rate-limit windows."""
    rows = await tracker.flush(db)
    if rows:
        deltas = _window_usage_deltas(rows)
        if deltas:
            try:
                await db.attribute_oauth_window_usage(deltas)
            except Exception:
                logger.exception("OAuth window usage attribution failed")
    return len(rows)
```

3c. In `_usage_flush_loop`, replace `n = await tracker.flush(db)` with `n = await _flush_usage(tracker, db)`.

3d. In the cleanup hook (near line 2975), replace `await tracker.flush(db)` with `await _flush_usage(tracker, db)`.

3e. `tests/test_anthropic_cache_usage.py` line 401: `self.assertEqual(flushed, 2)` → `self.assertEqual(len(flushed), 2)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_oauth_window_usage_db.py tests/test_anthropic_cache_usage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/usage.py src/smart_proxy/anthropic_proxy.py tests/test_oauth_window_usage_db.py tests/test_anthropic_cache_usage.py
git commit -m "feat(anthropic-proxy): attribute flushed usage to oauth windows"
```

---

### Task 5: `usage` and `pending` blocks on `/_oauth_usage_history`

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — `_oauth_usage_history_handler` (line 2226) + helper `_window_usage_block`; extend the `smart_proxy.usage` import (line 47) with `build_price_lookup, estimate_cost_with_cache`
- Test: `tests/test_anthropic_proxy_oauth_usage_history.py`

**Interfaces:**
- Consumes: Task 2 `list_oauth_window_usage` / `list_oauth_window_usage_pending`; existing `db.get_all_model_prices()` (db.py:1333); `build_price_lookup` / `estimate_cost_with_cache` from `smart_proxy.usage` (returns `(cost | None, partial: bool)`; exact model==prefix matches — asserting `cost_usd == 6.0` for `claude-sonnet-5` is safe); `WINDOW_USAGE_COUNTERS` already imported into `anthropic_proxy` by Task 4.
- Produces JSON additions:
  - each window entry gains `"usage"`: `null` when no traffic, else `{"models": {<model>: {<eight counters>, "cost_usd": float|null}}, "totals": {<eight counters>, "cost_usd": float, "cost_partial": bool}}`;
  - each key entry gains `"pending"`: `{<window_kind>: {"models": {<model>: {<eight counters>}}, "updated_at": str}}` (respects `?kind=` filter).

- [ ] **Step 1: Write the failing tests**

In `tests/test_anthropic_proxy_oauth_usage_history.py`, extend `_app` so the mocked db supports the new calls (add parameters with defaults so existing tests keep working):

```python
def _app(
    rows: list[dict],
    drops: list[dict] | None = None,
    usage: list[dict] | None = None,
    pending: list[dict] | None = None,
    prices: list[dict] | None = None,
    **extra: object,
) -> dict:
    mock_db = MagicMock()
    mock_db.list_anthropic_keys = AsyncMock(
        return_value=[
            {"id": "oauth-key-1", "key_type": "oauth", "status": "active",
             "name": "pro-sp-auth"},
            {"id": "api-key-1", "key_type": "api_key", "status": "active",
             "name": "big-key"},
        ]
    )
    mock_db.list_oauth_window_log = AsyncMock(return_value=rows)
    mock_db.list_oauth_window_drops = AsyncMock(return_value=drops or [])
    mock_db.list_oauth_window_usage = AsyncMock(return_value=usage or [])
    mock_db.list_oauth_window_usage_pending = AsyncMock(
        return_value=pending or [])
    mock_db.get_all_model_prices = AsyncMock(return_value=prices or [])
    mock_pool = MagicMock()
    mock_pool.check_auth.return_value = False
    app = {"anthropic_pool": mock_pool, "db": mock_db}
    app.update(extra)
    return app
```

Add module-level helpers and a test class:

```python
def _usage_row(**overrides: object) -> dict:
    base = {
        "window_id": 1, "model": "claude-sonnet-5",
        "input_tokens": 1_000_000, "output_tokens": 200_000,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0,
        "web_search_requests": 0, "requests": 42,
    }
    base.update(overrides)
    return base


_SONNET_PRICE = {
    "model_prefix": "claude-sonnet-5", "provider": "anthropic",
    "input_price": 3.0, "output_price": 15.0, "cache_read_price": 0.3,
    "cache_write_5m_price": 3.75, "cache_write_1h_price": 6.0,
    "updated_at": "2026-07-12T00:00:00+00:00",
}


class OauthUsageHistoryUsageBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_block_with_costs(self) -> None:
        app = _app([_log_row(id=1)], usage=[_usage_row()],
                   prices=[_SONNET_PRICE])
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        win = body["keys"][0]["windows"]["seven_day"][0]
        model = win["usage"]["models"]["claude-sonnet-5"]
        self.assertEqual(model["input_tokens"], 1_000_000)
        self.assertEqual(model["cost_usd"], 6.0)  # 1M*3 + 0.2M*15 per MTok
        totals = win["usage"]["totals"]
        self.assertEqual(totals["output_tokens"], 200_000)
        self.assertEqual(totals["requests"], 42)
        self.assertEqual(totals["cost_usd"], 6.0)
        self.assertFalse(totals["cost_partial"])
        self.assertEqual(body["keys"][0]["pending"], {})

    async def test_unpriced_model_sets_cost_partial(self) -> None:
        app = _app([_log_row(id=1)], usage=[_usage_row()], prices=[])
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        win = body["keys"][0]["windows"]["seven_day"][0]
        self.assertIsNone(win["usage"]["models"]["claude-sonnet-5"]["cost_usd"])
        self.assertEqual(win["usage"]["totals"]["cost_usd"], 0.0)
        self.assertTrue(win["usage"]["totals"]["cost_partial"])

    async def test_window_without_usage_has_null_block(self) -> None:
        app = _app([_log_row(id=1)])
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        self.assertIsNone(body["keys"][0]["windows"]["seven_day"][0]["usage"])

    async def test_pending_block_grouped_by_kind(self) -> None:
        pending = [{
            "key_id": "oauth-key-1", "window_kind": "seven_day",
            "model": "claude-sonnet-5",
            "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0, "web_search_requests": 0,
            "requests": 1, "updated_at": "2026-07-12T10:00:00+00:00",
        }]
        app = _app([_log_row(id=1)], pending=pending)
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        block = body["keys"][0]["pending"]["seven_day"]
        self.assertEqual(block["models"]["claude-sonnet-5"]["input_tokens"], 7)
        self.assertEqual(block["updated_at"], "2026-07-12T10:00:00+00:00")

    async def test_pending_respects_kind_filter(self) -> None:
        pending = [{
            "key_id": "oauth-key-1", "window_kind": "five_hour",
            "model": "claude-sonnet-5",
            "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0, "web_search_requests": 0,
            "requests": 1, "updated_at": "2026-07-12T10:00:00+00:00",
        }]
        app = _app([_log_row(id=1)], pending=pending)
        resp = await _oauth_usage_history_handler(make_mocked_request(
            "GET", "/_oauth_usage_history?kind=seven_day", app=app))
        body = json.loads(resp.body)
        self.assertEqual(body["keys"][0]["pending"], {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_anthropic_proxy_oauth_usage_history.py -q`
Expected: new tests FAIL with `KeyError: 'usage'` / `KeyError: 'pending'`; pre-existing tests still PASS (the widened `_app` defaults keep them green).

- [ ] **Step 3: Implement**

3a. Extend the import at `anthropic_proxy.py:47`:

```python
from smart_proxy.usage import (
    UsageTracker, extract_usage, extract_usage_from_sse, _TAIL_BUF_MAX,
    build_price_lookup, estimate_cost_with_cache,
)
```

3b. Add a helper above `_oauth_usage_history_handler`:

```python
def _window_usage_block(models: dict[str, dict] | None, prices: dict) -> dict | None:
    """Per-model token counters + cost for one window, shaped like /_usage."""
    if not models:
        return None
    out_models: dict[str, dict] = {}
    totals: dict[str, object] = {c: 0 for c in WINDOW_USAGE_COUNTERS}
    total_cost = 0.0
    cost_partial = False
    for model in sorted(models):
        row = models[model]
        entry: dict[str, object] = {c: row[c] for c in WINDOW_USAGE_COUNTERS}
        cost, partial = estimate_cost_with_cache(
            model,
            row["input_tokens"],
            row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_creation_tokens=row["cache_creation_tokens"],
            cache_creation_5m_tokens=row["cache_creation_5m_tokens"],
            cache_creation_1h_tokens=row["cache_creation_1h_tokens"],
            web_search_requests=row["web_search_requests"],
            prices=prices,
        )
        entry["cost_usd"] = round(cost, 4) if cost is not None else None
        if cost is not None:
            total_cost += cost
        if cost is None or partial:
            cost_partial = True
        out_models[model] = entry
        for c in WINDOW_USAGE_COUNTERS:
            totals[c] += row[c]
    totals["cost_usd"] = round(total_cost, 4)
    totals["cost_partial"] = cost_partial
    return {"models": out_models, "totals": totals}
```

3c. In `_oauth_usage_history_handler`:

- After the auth check, load prices once:

```python
    prices = build_price_lookup(await db.get_all_model_prices())
```

- Inside the per-key loop, right after `log_rows = await db.list_oauth_window_log(key_id)`:

```python
        usage_rows = await db.list_oauth_window_usage(key_id)
        usage_by_window: dict[int, dict[str, dict]] = {}
        for u in usage_rows:
            usage_by_window.setdefault(u["window_id"], {})[u["model"]] = u
```

- In the `bucket.append({...})` dict, add after `"span_days_since_prev": span,`:

```python
                "usage": _window_usage_block(
                    usage_by_window.get(log_row["id"]), prices),
```

- After the drops loop (before `keys_out.append`), build the pending block:

```python
        pending_rows = await db.list_oauth_window_usage_pending(key_id)
        pending: dict[str, dict] = {}
        for p in pending_rows:
            kind = p["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            block = pending.setdefault(
                kind, {"models": {}, "updated_at": p["updated_at"]})
            block["models"][p["model"]] = {
                c: p[c] for c in WINDOW_USAGE_COUNTERS}
            if p["updated_at"] > block["updated_at"]:
                block["updated_at"] = p["updated_at"]
```

- Add `"pending": pending,` to the `keys_out.append({...})` dict.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_anthropic_proxy_oauth_usage_history.py tests/test_oauth_window_tracking_e2e.py -q`
Expected: PASS (if a pre-existing test asserted an exact window-entry dict, update it to include `"usage": None`).

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_proxy_oauth_usage_history.py
git commit -m "feat(anthropic-proxy): usage and pending blocks on /_oauth_usage_history"
```

---

### Task 6: End-to-end test + full suite

**Files:**
- Modify: `tests/test_oauth_window_tracking_e2e.py` (add `timedelta` to the datetime import; new test)

**Interfaces:**
- Consumes: everything from Tasks 1–5 (real sqlite `Database`, `_oauth_usage_history_handler`).
- Produces: regression coverage of the full lifecycle: live attribution → expiry → pending → drain → history JSON.

- [ ] **Step 1: Write the e2e test**

In `tests/test_oauth_window_tracking_e2e.py`, change the datetime import to `from datetime import datetime, timedelta, timezone` and append to `OauthWindowTrackingE2ETests`:

```python
    async def test_token_attribution_pending_drain_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                now = datetime.now(timezone.utc)
                iso_now = now.isoformat()
                await db.db.execute(
                    """INSERT INTO anthropic_keys
                       (id, key_type, status, access_token, refresh_token,
                        expires_at, name, created_at, updated_at)
                       VALUES (?, 'oauth', 'active', 'tok', 'ref',
                               9999999999999, 'e2e', ?, ?)""",
                    ("oauth-key-1", iso_now, iso_now),
                )
                await db.db.commit()

                def _minute(dt: datetime) -> str:
                    return dt.replace(second=0, microsecond=0).isoformat()

                delta = {
                    "key_id": "oauth-key-1", "model": "claude-sonnet-5",
                    "input_tokens": 100, "output_tokens": 50,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "cache_creation_5m_tokens": 0,
                    "cache_creation_1h_tokens": 0,
                    "web_search_requests": 0, "requests": 2,
                }

                # Poll observes a window; tokens then arrive after it expired.
                first = now + timedelta(hours=1)
                await db.record_oauth_window_observations("oauth-key-1", [{
                    "window_kind": "seven_day",
                    "resets_at": _minute(first),
                    "resets_at_raw": first.isoformat(),
                    "utilization": 90.0,
                }])
                late = (now + timedelta(hours=2)).isoformat()
                await db.attribute_oauth_window_usage([delta], now=late)
                self.assertEqual(
                    len(await db.list_oauth_window_usage_pending("oauth-key-1")),
                    1)

                # Next poll sees the successor window: pending drains into it.
                second = now + timedelta(days=7)
                await db.record_oauth_window_observations("oauth-key-1", [{
                    "window_kind": "seven_day",
                    "resets_at": _minute(second),
                    "resets_at_raw": second.isoformat(),
                    "utilization": 1.0,
                }])
                self.assertEqual(
                    await db.list_oauth_window_usage_pending("oauth-key-1"), [])

                # More traffic while the new window is live.
                await db.attribute_oauth_window_usage([delta], now=late)

                app = {"anthropic_pool": MagicMock(), "db": db}
                hist = await _oauth_usage_history_handler(
                    make_mocked_request("GET", "/_oauth_usage_history", app=app))
                body = json.loads(hist.body)
                seven = body["keys"][0]["windows"]["seven_day"]
                self.assertEqual(len(seven), 2)
                newest = seven[0]
                self.assertEqual(newest["usage"]["totals"]["input_tokens"], 200)
                self.assertEqual(newest["usage"]["totals"]["requests"], 4)
                self.assertIn(
                    "claude-sonnet-5", newest["usage"]["models"])
                self.assertIsNone(seven[1]["usage"])
                self.assertEqual(body["keys"][0]["pending"], {})
            finally:
                await db.close()
```

- [ ] **Step 2: Run the e2e test**

Run: `uv run python -m pytest tests/test_oauth_window_tracking_e2e.py -q`
Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run: `uv run python -m pytest tests/ -q`
Expected: all tests PASS. Fix any stragglers (most likely: a test asserting exact window/keys dict shapes now missing `usage`/`pending`).

- [ ] **Step 4: Commit**

```bash
git add tests/test_oauth_window_tracking_e2e.py
git commit -m "test: e2e per-window token attribution lifecycle"
```
