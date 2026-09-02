# OAuth Rate-Limit Window Reset Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the lifecycle of every Anthropic OAuth rate-limit window (one DB row per window instance, updated in place) so we can measure how long the "7-day" window actually lasts, and expose the history via `GET /_oauth_usage_history`.

**Architecture:** Piggyback on the existing `/api/oauth/usage` polling in `_build_oauth_usage_payload()` (src/smart_proxy/anthropic_proxy.py). A pure extraction helper flattens the usage payload into window observations; `Database.record_oauth_window_observations()` upserts one row per `(key_id, window_kind, minute-truncated resets_at)` and returns detected reset events; a new read-only aiohttp handler serves the aggregated history with computed `span_days_since_prev`.

**Tech Stack:** Python 3, aiohttp, aiosqlite (SQLite) + psycopg (PostgreSQL via `_translate_sql` `?`→`%s`), unittest-style tests run with pytest.

**Spec:** `docs/superpowers/specs/2026-07-02-oauth-window-reset-tracking-design.md`

## Global Constraints

- All SQL must work on both backends: SQLite (aiosqlite) and PostgreSQL (`PostgresDatabase` inherits `Database` and translates `?`→`%s`). No SQLite-only or PG-only syntax in shared methods — compute conditionals in Python, not in SQL `CASE WHEN <bool param>`.
- Window identity = `resets_at` truncated to minute precision (upstream jitters the fractional seconds between polls of the same window).
- Recording failures must NEVER break the `/_oauth_usage` response (broad try/except + `logger.warning`).
- Utilization values are percent floats 0–100 (top-level `utilization`) or ints (limits `percent`); store as REAL.
- Tests live in `tests/`, are `unittest.IsolatedAsyncioTestCase`/`unittest.TestCase` style with the `sys.path` preamble used by every existing test file, and run via `.venv/bin/python -m pytest`.
- Commit after every green test cycle. Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `oauth_window_log` table + `Database.record_oauth_window_observations()` / `list_oauth_window_log()`

**Files:**
- Modify: `src/smart_proxy/db.py` (SCHEMA_SQL ~line 199, MIGRATIONS ~line 304, SNAPSHOT_TABLE_SPECS ~line 449, SNAPSHOT_DELETE_ORDER ~line 452, new methods after `record_rate_limit` ~line 1647)
- Modify: `src/smart_proxy/db_migrations.py` (append postgres migration)
- Modify: `src/smart_proxy/db_postgres.py` (`_after_replace_snapshot` table list ~line 139)
- Test: `tests/test_oauth_window_log.py` (new)

**Interfaces:**
- Consumes: existing `Database` class, its `self.db` connection adapter, `datetime`/`timezone` already imported in db.py.
- Produces:
  - `async Database.record_oauth_window_observations(key_id: str, observations: list[dict], *, seen_at: str | None = None) -> list[dict]` — each observation dict has keys `window_kind: str`, `resets_at: str` (minute-truncated ISO), `resets_at_raw: str`, `utilization: float | None`. Returns a list of reset events: `{"key_id", "window_kind", "prev_resets_at", "new_resets_at", "span_days": float | None}`.
  - `async Database.list_oauth_window_log(key_id: str) -> list[dict]` — all rows for a key ordered by `window_kind ASC, resets_at ASC`, as plain dicts with every table column.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_oauth_window_log.py`:

```python
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database


def _obs(**overrides: object) -> dict:
    base = {
        "window_kind": "seven_day",
        "resets_at": "2026-07-02T11:00:00+00:00",
        "resets_at_raw": "2026-07-02T11:00:00.028998+00:00",
        "utilization": 0.0,
    }
    base.update(overrides)
    return base


class OauthWindowLogTests(unittest.TestCase):
    def _run(self, coro) -> None:
        asyncio.run(coro)

    def test_first_observation_inserts_row_without_reset_event(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    resets = await db.record_oauth_window_observations(
                        "key-1", [_obs()], seen_at="2026-07-01T05:00:00+00:00"
                    )
                    self.assertEqual(resets, [])
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertEqual(row["window_kind"], "seven_day")
                    self.assertEqual(row["resets_at"], "2026-07-02T11:00:00+00:00")
                    self.assertEqual(
                        row["resets_at_raw"], "2026-07-02T11:00:00.028998+00:00"
                    )
                    self.assertEqual(row["first_seen_at"], "2026-07-01T05:00:00+00:00")
                    self.assertEqual(row["last_seen_at"], "2026-07-01T05:00:00+00:00")
                    self.assertIsNone(row["first_active_at"])  # utilization == 0
                    self.assertEqual(row["observations"], 1)
                    self.assertEqual(row["last_utilization"], 0.0)
                    self.assertEqual(row["max_utilization"], 0.0)
                finally:
                    await db.close()

        self._run(scenario())

    def test_same_window_updates_in_place(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=0.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    # usage starts: first_active_at stamps, max/last update
                    resets = await db.record_oauth_window_observations(
                        "key-1",
                        [_obs(
                            utilization=55.0,
                            resets_at_raw="2026-07-02T11:00:00.874164+00:00",
                        )],
                        seen_at="2026-07-01T09:00:00+00:00",
                    )
                    self.assertEqual(resets, [])
                    # utilization dips: max stays, last follows
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=40.0)],
                        seen_at="2026-07-01T10:00:00+00:00",
                    )
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertEqual(row["observations"], 3)
                    self.assertEqual(row["first_seen_at"], "2026-07-01T05:00:00+00:00")
                    self.assertEqual(row["first_active_at"], "2026-07-01T09:00:00+00:00")
                    self.assertEqual(row["last_seen_at"], "2026-07-01T10:00:00+00:00")
                    self.assertEqual(row["last_utilization"], 40.0)
                    self.assertEqual(row["max_utilization"], 55.0)
                    self.assertEqual(
                        row["max_utilization_at"], "2026-07-01T09:00:00+00:00"
                    )
                    self.assertEqual(
                        row["resets_at_raw"], "2026-07-02T11:00:00.028998+00:00"
                    )
                finally:
                    await db.close()

        self._run(scenario())

    def test_changed_resets_at_creates_new_row_and_reset_event(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    await db.record_oauth_window_observations(
                        "key-1", [_obs(utilization=71.0)],
                        seen_at="2026-07-01T05:00:00+00:00",
                    )
                    resets = await db.record_oauth_window_observations(
                        "key-1",
                        [_obs(
                            resets_at="2026-07-06T16:00:00+00:00",
                            resets_at_raw="2026-07-06T16:00:00.111111+00:00",
                            utilization=1.0,
                        )],
                        seen_at="2026-07-02T12:00:00+00:00",
                    )
                    self.assertEqual(len(resets), 1)
                    event = resets[0]
                    self.assertEqual(event["key_id"], "key-1")
                    self.assertEqual(event["window_kind"], "seven_day")
                    self.assertEqual(
                        event["prev_resets_at"], "2026-07-02T11:00:00+00:00"
                    )
                    self.assertEqual(
                        event["new_resets_at"], "2026-07-06T16:00:00+00:00"
                    )
                    self.assertEqual(event["span_days"], 4.21)
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(rows[0]["resets_at"], "2026-07-02T11:00:00+00:00")
                    self.assertEqual(rows[1]["resets_at"], "2026-07-06T16:00:00+00:00")
                    self.assertEqual(
                        rows[1]["first_active_at"], "2026-07-02T12:00:00+00:00"
                    )
                finally:
                    await db.close()

        self._run(scenario())

    def test_kinds_are_independent_and_keys_isolated(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                try:
                    resets = await db.record_oauth_window_observations(
                        "key-1",
                        [
                            _obs(),
                            _obs(window_kind="five_hour",
                                 resets_at="2026-07-01T10:00:00+00:00",
                                 utilization=4.0),
                            _obs(window_kind="limit:weekly_scoped:Fable",
                                 utilization=None),
                        ],
                    )
                    self.assertEqual(resets, [])
                    await db.record_oauth_window_observations("key-2", [_obs()])
                    rows = await db.list_oauth_window_log("key-1")
                    self.assertEqual(len(rows), 3)
                    kinds = [r["window_kind"] for r in rows]
                    self.assertEqual(
                        kinds,
                        ["five_hour", "limit:weekly_scoped:Fable", "seven_day"],
                    )
                    scoped = rows[1]
                    self.assertIsNone(scoped["last_utilization"])
                    self.assertIsNone(scoped["max_utilization"])
                    self.assertIsNone(scoped["first_active_at"])
                    self.assertEqual(len(await db.list_oauth_window_log("key-2")), 1)
                finally:
                    await db.close()

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
```

Note on `span_days == 4.21`: 2026-07-02T11:00 → 2026-07-06T16:00 is 4 days 5 h = 4.2083…, rounded to 2 decimals = 4.21.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_oauth_window_log.py -v`
Expected: 4 failures/errors with `AttributeError: 'Database' object has no attribute 'record_oauth_window_observations'`.

- [ ] **Step 3: Add the table to SCHEMA_SQL, MIGRATIONS, snapshot specs, and postgres migrations**

In `src/smart_proxy/db.py`, inside `SCHEMA_SQL` right after the `rate_limit_log` block (`CREATE INDEX IF NOT EXISTS idx_rll_provider_date ...` line, before the closing `"""`), add:

```sql
CREATE TABLE IF NOT EXISTS oauth_window_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id             TEXT    NOT NULL,   -- anthropic_keys.id
    window_kind        TEXT    NOT NULL,   -- 'five_hour', 'seven_day', 'limit:weekly_scoped:Fable', ...
    resets_at          TEXT    NOT NULL,   -- minute-truncated ISO UTC; window identity
    resets_at_raw      TEXT    NOT NULL,   -- as last received from upstream
    first_seen_at      TEXT    NOT NULL,
    first_active_at    TEXT,               -- first observation with utilization > 0
    last_seen_at       TEXT    NOT NULL,
    observations       INTEGER NOT NULL DEFAULT 1,
    last_utilization   REAL,               -- percent 0-100
    max_utilization    REAL,
    max_utilization_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_owl_identity
    ON oauth_window_log(key_id, window_kind, resets_at);
```

In the `MIGRATIONS` list (end of list, after `"ALTER TABLE rate_limit_log ADD COLUMN utilization_7d REAL",`), append two entries — the same `CREATE TABLE IF NOT EXISTS oauth_window_log (...)` statement as a triple-quoted string and the same `CREATE UNIQUE INDEX IF NOT EXISTS idx_owl_identity ...` statement (this is how existing SQLite DBs created before this table get it; the try/except in `_run_migrations` makes re-runs harmless).

In `SNAPSHOT_TABLE_SPECS`, after the `rate_limit_log` entry, add:

```python
    (
        "oauth_window_log",
        (
            "id",
            "key_id",
            "window_kind",
            "resets_at",
            "resets_at_raw",
            "first_seen_at",
            "first_active_at",
            "last_seen_at",
            "observations",
            "last_utilization",
            "max_utilization",
            "max_utilization_at",
        ),
        "id",
    ),
```

In `SNAPSHOT_DELETE_ORDER`, add `"oauth_window_log",` as the first element (no FK constraints, order just needs to be present).

In `src/smart_proxy/db_postgres.py`, `_after_replace_snapshot`, add `"oauth_window_log"` to the tuple of tables whose `id` sequence is resynced (it has an identity `id` column like the others).

In `src/smart_proxy/db_migrations.py`, append to `POSTGRES_MIGRATIONS` after the `0001_initial_schema` entry:

```python
    (
        "0002_oauth_window_log",
        (
            """
            CREATE TABLE IF NOT EXISTS oauth_window_log (
                id                 INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                key_id             TEXT    NOT NULL,
                window_kind        TEXT    NOT NULL,
                resets_at          TEXT    NOT NULL,
                resets_at_raw      TEXT    NOT NULL,
                first_seen_at      TEXT    NOT NULL,
                first_active_at    TEXT,
                last_seen_at       TEXT    NOT NULL,
                observations       INTEGER NOT NULL DEFAULT 1,
                last_utilization   REAL,
                max_utilization    REAL,
                max_utilization_at TEXT
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_owl_identity
                ON oauth_window_log(key_id, window_kind, resets_at)
            """,
        ),
    ),
```

- [ ] **Step 4: Implement the two Database methods**

In `src/smart_proxy/db.py`, directly after `record_rate_limit` (ends ~line 1647), add:

```python
    async def record_oauth_window_observations(
        self,
        key_id: str,
        observations: list[dict],
        *,
        seen_at: str | None = None,
    ) -> list[dict]:
        """Upsert one row per rate-limit window instance; return reset events.

        Each observation: ``{"window_kind", "resets_at" (minute-truncated ISO,
        window identity), "resets_at_raw", "utilization" (percent or None)}``.
        A new row for a kind that already has an older row means the window
        reset; those events are returned for the caller to log.
        """
        now = seen_at or datetime.now(timezone.utc).isoformat()
        reset_events: list[dict] = []
        for obs in observations:
            kind = obs["window_kind"]
            resets_at = obs["resets_at"]
            utilization = obs.get("utilization")
            cur = await self.db.execute(
                """SELECT id, first_active_at, last_utilization,
                          max_utilization, max_utilization_at
                   FROM oauth_window_log
                   WHERE key_id = ? AND window_kind = ? AND resets_at = ?""",
                (key_id, kind, resets_at),
            )
            existing = await cur.fetchone()
            if existing:
                max_util = existing["max_utilization"]
                max_util_at = existing["max_utilization_at"]
                if utilization is not None and (
                    max_util is None or utilization > max_util
                ):
                    max_util, max_util_at = utilization, now
                first_active = existing["first_active_at"]
                if first_active is None and utilization is not None and utilization > 0:
                    first_active = now
                last_util = (
                    utilization if utilization is not None
                    else existing["last_utilization"]
                )
                await self.db.execute(
                    """UPDATE oauth_window_log
                       SET last_seen_at = ?,
                           observations = observations + 1,
                           resets_at_raw = ?,
                           first_active_at = ?,
                           last_utilization = ?,
                           max_utilization = ?,
                           max_utilization_at = ?
                       WHERE id = ?""",
                    (now, obs["resets_at_raw"], first_active, last_util,
                     max_util, max_util_at, existing["id"]),
                )
                continue
            cur = await self.db.execute(
                """SELECT resets_at FROM oauth_window_log
                   WHERE key_id = ? AND window_kind = ?
                   ORDER BY resets_at DESC LIMIT 1""",
                (key_id, kind),
            )
            prev = await cur.fetchone()
            active = utilization is not None and utilization > 0
            await self.db.execute(
                """INSERT INTO oauth_window_log
                   (key_id, window_kind, resets_at, resets_at_raw, first_seen_at,
                    first_active_at, last_seen_at, observations,
                    last_utilization, max_utilization, max_utilization_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (key_id, kind, resets_at, obs["resets_at_raw"], now,
                 now if active else None, now, utilization, utilization,
                 now if utilization is not None else None),
            )
            if prev:
                reset_events.append({
                    "key_id": key_id,
                    "window_kind": kind,
                    "prev_resets_at": prev["resets_at"],
                    "new_resets_at": resets_at,
                    "span_days": _iso_span_days(prev["resets_at"], resets_at),
                })
        await self.db.commit()
        return reset_events

    async def list_oauth_window_log(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT id, key_id, window_kind, resets_at, resets_at_raw,
                      first_seen_at, first_active_at, last_seen_at,
                      observations, last_utilization, max_utilization,
                      max_utilization_at
               FROM oauth_window_log
               WHERE key_id = ?
               ORDER BY window_kind ASC, resets_at ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]
```

And add the module-level helper near the top of `db.py` (after the imports, before `SCHEMA_SQL`):

```python
def _iso_span_days(prev_iso: str, new_iso: str) -> float | None:
    """Distance between two ISO timestamps in days, 2 decimals; None if unparseable."""
    try:
        prev_dt = datetime.fromisoformat(prev_iso)
        new_dt = datetime.fromisoformat(new_iso)
        return round((new_dt - prev_dt).total_seconds() / 86400.0, 2)
    except (ValueError, TypeError):
        return None
```

(`datetime`/`timezone` are already imported in db.py.)

- [ ] **Step 5: Run the new tests, then the full suite**

Run: `.venv/bin/python -m pytest tests/test_oauth_window_log.py -v`
Expected: 4 passed.

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass. Watch `tests/test_db_snapshot_roundtrip.py` and `tests/test_db_import_verify.py` in particular — they iterate `SNAPSHOT_TABLE_SPECS`; the new empty table must round-trip cleanly (empty list in, empty list out). If a test asserts an explicit table list, add `oauth_window_log` to it.

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/db.py src/smart_proxy/db_migrations.py src/smart_proxy/db_postgres.py tests/test_oauth_window_log.py
git commit -m "feat(db): oauth_window_log table tracking rate-limit window lifecycles

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `_extract_window_observations()` payload flattener

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` (new pure helpers near `_utc_now_iso`, ~line 1796)
- Test: `tests/test_extract_window_observations.py` (new)

**Interfaces:**
- Consumes: nothing project-specific (pure functions; `datetime`/`timezone` already imported in anthropic_proxy.py line 23).
- Produces:
  - `_extract_window_observations(usage: dict) -> list[dict]` — observation dicts with keys `window_kind`, `resets_at`, `resets_at_raw`, `utilization`, exactly the shape `Database.record_oauth_window_observations` consumes (Task 1).
  - `_truncate_resets_at_minute(raw: str) -> str | None` — internal, but tested.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extract_window_observations.py`:

```python
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import (
    _extract_window_observations,
    _truncate_resets_at_minute,
)

# Real /api/oauth/usage shape observed 2026-07-01 (trimmed).
REAL_PAYLOAD = {
    "five_hour": {
        "utilization": 4.0,
        "resets_at": "2026-07-02T02:10:00.028978+00:00",
        "limit_dollars": None,
    },
    "seven_day": {
        "utilization": 1.0,
        "resets_at": "2026-07-02T11:00:00.028998+00:00",
        "limit_dollars": None,
    },
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "extra_usage": {"is_enabled": False, "monthly_limit": None},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 4,
            "severity": "normal",
            "resets_at": "2026-07-02T02:10:00.874141+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 1,
            "severity": "normal",
            "resets_at": "2026-07-02T11:00:00.874164+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 0,
            "severity": "normal",
            "resets_at": "2026-07-02T11:00:00.874513+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ],
}


class TruncateResetsAtTests(unittest.TestCase):
    def test_truncates_to_minute_in_utc(self) -> None:
        self.assertEqual(
            _truncate_resets_at_minute("2026-07-02T02:10:00.874141+00:00"),
            "2026-07-02T02:10:00+00:00",
        )

    def test_jittered_fractions_map_to_same_identity(self) -> None:
        a = _truncate_resets_at_minute("2026-07-02T11:00:00.028998+00:00")
        b = _truncate_resets_at_minute("2026-07-02T11:00:00.874164+00:00")
        self.assertEqual(a, b)

    def test_z_suffix_and_offsets_normalize_to_utc(self) -> None:
        self.assertEqual(
            _truncate_resets_at_minute("2026-07-02T14:00:59Z"),
            "2026-07-02T14:00:00+00:00",
        )
        self.assertEqual(
            _truncate_resets_at_minute("2026-07-02T13:00:00+02:00"),
            "2026-07-02T11:00:00+00:00",
        )

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(_truncate_resets_at_minute("not-a-date"))
        self.assertIsNone(_truncate_resets_at_minute(""))


class ExtractWindowObservationsTests(unittest.TestCase):
    def test_real_payload_yields_all_windows(self) -> None:
        obs = _extract_window_observations(REAL_PAYLOAD)
        by_kind = {o["window_kind"]: o for o in obs}
        self.assertEqual(
            set(by_kind),
            {
                "five_hour",
                "seven_day",
                "limit:session",
                "limit:weekly_all",
                "limit:weekly_scoped:Fable",
            },
        )
        seven = by_kind["seven_day"]
        self.assertEqual(seven["resets_at"], "2026-07-02T11:00:00+00:00")
        self.assertEqual(
            seven["resets_at_raw"], "2026-07-02T11:00:00.028998+00:00"
        )
        self.assertEqual(seven["utilization"], 1.0)
        scoped = by_kind["limit:weekly_scoped:Fable"]
        self.assertEqual(scoped["utilization"], 0.0)  # limits use int percent
        self.assertEqual(by_kind["limit:session"]["utilization"], 4.0)

    def test_null_windows_and_extra_usage_are_skipped(self) -> None:
        kinds = {o["window_kind"] for o in _extract_window_observations(REAL_PAYLOAD)}
        self.assertNotIn("seven_day_opus", kinds)
        self.assertNotIn("extra_usage", kinds)

    def test_unscoped_limit_kind_has_no_suffix(self) -> None:
        payload = {"limits": [{"kind": "weekly_all", "percent": 5,
                               "resets_at": "2026-07-09T11:00:00+00:00"}]}
        obs = _extract_window_observations(payload)
        self.assertEqual(obs[0]["window_kind"], "limit:weekly_all")

    def test_malformed_input_is_tolerated(self) -> None:
        self.assertEqual(_extract_window_observations({}), [])
        self.assertEqual(_extract_window_observations(None), [])
        self.assertEqual(
            _extract_window_observations(
                {
                    "five_hour": {"utilization": 3.0, "resets_at": "garbage"},
                    "seven_day": "not-a-dict",
                    "limits": [None, {"kind": None, "resets_at": "2026-07-09T11:00:00Z"},
                               {"kind": "x"}],
                }
            ),
            [],
        )

    def test_missing_utilization_becomes_none(self) -> None:
        payload = {"five_hour": {"resets_at": "2026-07-02T02:10:00+00:00"}}
        obs = _extract_window_observations(payload)
        self.assertEqual(len(obs), 1)
        self.assertIsNone(obs[0]["utilization"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_extract_window_observations.py -v`
Expected: collection error — `ImportError: cannot import name '_extract_window_observations'`.

- [ ] **Step 3: Implement the helpers**

In `src/smart_proxy/anthropic_proxy.py`, directly after `_utc_now_iso()` (~line 1797), add:

```python
def _truncate_resets_at_minute(raw: str) -> str | None:
    """Normalize an upstream resets_at to minute precision in UTC.

    Upstream jitters the fractional seconds between polls of the same
    window, so minute-truncated resets_at is the window's identity.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


_WINDOW_TOP_LEVEL_SKIP = frozenset({"limits", "extra_usage"})


def _extract_window_observations(usage: dict) -> list[dict]:
    """Flatten an /api/oauth/usage payload into window observations.

    Top-level window objects keep their JSON key as ``window_kind``
    (``five_hour``, ``seven_day``, ...); ``limits[]`` entries become
    ``limit:<kind>`` with the model display name appended for scoped
    limits (``limit:weekly_scoped:Fable``).
    """
    if not isinstance(usage, dict):
        return []
    out: list[dict] = []

    def _add(kind: str, raw_resets_at: object, utilization: object) -> None:
        if not isinstance(raw_resets_at, str):
            return
        resets_at = _truncate_resets_at_minute(raw_resets_at)
        if resets_at is None:
            return
        util = (
            float(utilization)
            if isinstance(utilization, (int, float)) and not isinstance(utilization, bool)
            else None
        )
        out.append({
            "window_kind": kind,
            "resets_at": resets_at,
            "resets_at_raw": raw_resets_at,
            "utilization": util,
        })

    for key, value in usage.items():
        if key in _WINDOW_TOP_LEVEL_SKIP or not isinstance(value, dict):
            continue
        if "resets_at" in value:
            _add(key, value.get("resets_at"), value.get("utilization"))

    limits = usage.get("limits")
    if isinstance(limits, list):
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            kind = limit.get("kind")
            if not isinstance(kind, str) or not kind:
                continue
            name = f"limit:{kind}"
            scope = limit.get("scope")
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict):
                    display = model.get("display_name") or model.get("id")
                    if isinstance(display, str) and display:
                        name = f"{name}:{display}"
            _add(name, limit.get("resets_at"), limit.get("percent"))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_extract_window_observations.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_extract_window_observations.py
git commit -m "feat(anthropic-proxy): flatten oauth usage payload into window observations

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: record observations from `_build_oauth_usage_payload()`

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py:1899-1908` (the `r.status_code == 200` branch of `_build_oauth_usage_payload`)
- Test: `tests/test_anthropic_proxy_oauth_usage_endpoint.py` (extend)

**Interfaces:**
- Consumes: `_extract_window_observations(usage)` (Task 2), `Database.record_oauth_window_observations(key_id, observations)` (Task 1).
- Produces: no new API — side effect only. Existing `/_oauth_usage` behavior (payload shape, caching, error surface) must not change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_anthropic_proxy_oauth_usage_endpoint.py` (inside `OauthUsageEndpointTests`; reuses the module's existing imports — `AsyncMock`, `MagicMock`, `make_mocked_request`, `AnthropicKeyPool`, `json`):

```python
    async def test_success_records_window_observations(self) -> None:
        usage_payload = {
            "five_hour": {
                "utilization": 4.0,
                "resets_at": "2026-07-02T02:10:00.028978+00:00",
            },
            "seven_day": {
                "utilization": 1.0,
                "resets_at": "2026-07-02T11:00:00.028998+00:00",
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])
        mock_db.record_oauth_window_observations = AsyncMock(return_value=[])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "http_client": mock_client,
            "db": mock_db,
        }
        resp = await _oauth_usage_handler(
            make_mocked_request("GET", "/_oauth_usage", app=app)
        )
        self.assertEqual(resp.status, 200)
        mock_db.record_oauth_window_observations.assert_awaited_once()
        call = mock_db.record_oauth_window_observations.await_args
        self.assertEqual(call[0][0], "oauth-key-1")
        kinds = {o["window_kind"] for o in call[0][1]}
        self.assertEqual(kinds, {"five_hour", "seven_day"})

    async def test_recording_failure_does_not_break_response(self) -> None:
        usage_payload = {
            "seven_day": {
                "utilization": 1.0,
                "resets_at": "2026-07-02T11:00:00+00:00",
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])
        mock_db.record_oauth_window_observations = AsyncMock(
            side_effect=RuntimeError("db locked")
        )

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "http_client": mock_client,
            "db": mock_db,
        }
        resp = await _oauth_usage_handler(
            make_mocked_request("GET", "/_oauth_usage", app=app)
        )
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["keys"][0]["usage"], usage_payload)
        self.assertNotIn("error", body["keys"][0])

    async def test_upstream_error_does_not_record(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"error": {"type": "rate_limit_error"}}
        mock_resp.headers = {}
        mock_resp.text = "{}"

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])
        mock_db.record_oauth_window_observations = AsyncMock()

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "http_client": mock_client,
            "db": mock_db,
        }
        resp = await _oauth_usage_handler(
            make_mocked_request("GET", "/_oauth_usage", app=app)
        )
        self.assertEqual(resp.status, 200)
        mock_db.record_oauth_window_observations.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_oauth_usage_endpoint.py -v`
Expected: `test_success_records_window_observations` fails (`assert_awaited_once` — 0 awaits); the two others may pass trivially; all pre-existing tests still pass.

- [ ] **Step 3: Wire recording into the success branch**

In `src/smart_proxy/anthropic_proxy.py`, `_build_oauth_usage_payload`, replace the current 200-branch:

```python
        entry["http_status"] = r.status_code
        if r.status_code == 200:
            try:
                entry["usage"] = r.json()
            except (json.JSONDecodeError, ValueError):
                entry["error"] = "usage_invalid_json"
                entry["usage_body_preview"] = r.text[:500]
                last_failure = {
                    "seen_at": _utc_now_iso(),
                    **entry,
                }
```

with:

```python
        entry["http_status"] = r.status_code
        if r.status_code == 200:
            try:
                entry["usage"] = r.json()
            except (json.JSONDecodeError, ValueError):
                entry["error"] = "usage_invalid_json"
                entry["usage_body_preview"] = r.text[:500]
                last_failure = {
                    "seen_at": _utc_now_iso(),
                    **entry,
                }
            else:
                await _record_window_observations(db, key.key_id, entry["usage"])
```

and add this helper directly above `_build_oauth_usage_payload`:

```python
async def _record_window_observations(db: Database, key_id: str, usage: dict) -> None:
    """Persist window lifecycles from a usage payload; never raise."""
    try:
        observations = _extract_window_observations(usage)
        if not observations:
            return
        reset_events = await db.record_oauth_window_observations(key_id, observations)
        for event in reset_events:
            logger.info(
                "OAuth window reset key=%s kind=%s prev_resets_at=%s new_resets_at=%s span=%sd",
                key_id[:12],
                event["window_kind"],
                event["prev_resets_at"],
                event["new_resets_at"],
                event["span_days"],
            )
    except Exception:
        logger.warning(
            "Failed to record oauth window observations for key %s",
            key_id[:12],
            exc_info=True,
        )
```

Note: pre-existing tests pass a plain `MagicMock()` as db, so `await db.record_oauth_window_observations(...)` raises `TypeError` there — the broad except swallows it by design (recording must never break the endpoint), and those tests keep passing.

- [ ] **Step 4: Run the endpoint tests, then the full suite**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_oauth_usage_endpoint.py -v`
Expected: all pass (old and new).

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_proxy_oauth_usage_endpoint.py
git commit -m "feat(anthropic-proxy): record rate-limit window lifecycles on usage polls

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `GET /_oauth_usage_history` endpoint

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` (new handler after `_oauth_usage_handler`; route registration ~line 2734)
- Test: `tests/test_anthropic_proxy_oauth_usage_history.py` (new)

**Interfaces:**
- Consumes: `Database.list_oauth_window_log(key_id)` and `Database.list_anthropic_keys()` (Task 1 / existing), `_extract_client_token`, `_utc_now_iso` (existing).
- Produces: `GET /_oauth_usage_history?kind=<window_kind>&limit=<N>` returning `{"generated_at", "keys": [{"id", "name", "status", "windows": {kind: [window..., newest first]}}]}` where each window dict has `resets_at`, `first_seen_at`, `first_active_at`, `last_seen_at`, `observations`, `last_utilization`, `max_utilization`, `max_utilization_at`, `span_days_since_prev`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_anthropic_proxy_oauth_usage_history.py`:

```python
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request

from smart_proxy.anthropic_proxy import _oauth_usage_history_handler


def _log_row(**overrides: object) -> dict:
    base = {
        "id": 1,
        "key_id": "oauth-key-1",
        "window_kind": "seven_day",
        "resets_at": "2026-07-02T11:00:00+00:00",
        "resets_at_raw": "2026-07-02T11:00:00.028998+00:00",
        "first_seen_at": "2026-06-28T09:00:00+00:00",
        "first_active_at": "2026-06-28T09:30:00+00:00",
        "last_seen_at": "2026-07-02T10:58:00+00:00",
        "observations": 812,
        "last_utilization": 34.0,
        "max_utilization": 71.0,
        "max_utilization_at": "2026-07-01T18:00:00+00:00",
    }
    base.update(overrides)
    return base


def _app(rows: list[dict], **extra: object) -> dict:
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
    mock_pool = MagicMock()
    mock_pool.check_auth.return_value = False
    app = {"anthropic_pool": mock_pool, "db": mock_db}
    app.update(extra)
    return app


class OauthUsageHistoryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_by_kind_and_computes_spans_newest_first(self) -> None:
        # list_oauth_window_log returns window_kind ASC, resets_at ASC
        rows = [
            _log_row(id=1, window_kind="five_hour",
                     resets_at="2026-07-01T21:00:00+00:00"),
            _log_row(id=2, resets_at="2026-06-28T05:00:00+00:00"),
            _log_row(id=3, resets_at="2026-07-02T11:00:00+00:00"),
        ]
        app = _app(rows)
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app)
        )
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertIn("generated_at", body)
        self.assertEqual(len(body["keys"]), 1)  # api_key row excluded
        key = body["keys"][0]
        self.assertEqual(key["id"], "oauth-key-1")
        self.assertEqual(key["name"], "pro-sp-auth")
        self.assertEqual(set(key["windows"]), {"five_hour", "seven_day"})
        seven = key["windows"]["seven_day"]
        self.assertEqual(len(seven), 2)
        # newest first
        self.assertEqual(seven[0]["resets_at"], "2026-07-02T11:00:00+00:00")
        # 06-28T05:00 -> 07-02T11:00 = 4d6h = 4.25 days
        self.assertEqual(seven[0]["span_days_since_prev"], 4.25)
        self.assertIsNone(seven[1]["span_days_since_prev"])  # oldest has no prev
        self.assertEqual(seven[0]["observations"], 812)
        self.assertEqual(seven[0]["max_utilization"], 71.0)
        self.assertEqual(
            seven[0]["first_active_at"], "2026-06-28T09:30:00+00:00"
        )
        self.assertIsNone(
            key["windows"]["five_hour"][0]["span_days_since_prev"]
        )

    async def test_kind_filter_and_limit(self) -> None:
        rows = [
            _log_row(id=1, window_kind="five_hour",
                     resets_at="2026-07-01T21:00:00+00:00"),
            _log_row(id=2, resets_at="2026-06-20T05:00:00+00:00"),
            _log_row(id=3, resets_at="2026-06-28T05:00:00+00:00"),
            _log_row(id=4, resets_at="2026-07-02T11:00:00+00:00"),
        ]
        app = _app(rows)
        resp = await _oauth_usage_history_handler(
            make_mocked_request(
                "GET", "/_oauth_usage_history?kind=seven_day&limit=2", app=app
            )
        )
        body = json.loads(resp.body)
        windows = body["keys"][0]["windows"]
        self.assertEqual(set(windows), {"seven_day"})
        seven = windows["seven_day"]
        self.assertEqual(len(seven), 2)  # limit applied after newest-first sort
        self.assertEqual(seven[0]["resets_at"], "2026-07-02T11:00:00+00:00")
        self.assertEqual(seven[1]["resets_at"], "2026-06-28T05:00:00+00:00")
        # span still computed from the full sequence (prev = 06-20 row)
        self.assertEqual(seven[1]["span_days_since_prev"], 8.0)

    async def test_requires_auth_when_flag_enabled(self) -> None:
        app = _app([], oauth_usage_require_auth=True)
        resp = await _oauth_usage_history_handler(
            make_mocked_request(
                "GET", "/_oauth_usage_history", app=app,
                headers={"Authorization": "Bearer sp-bad"},
            )
        )
        self.assertEqual(resp.status, 401)

    async def test_route_is_registered(self) -> None:
        import inspect  # noqa: PLC0415

        import smart_proxy.anthropic_proxy as mod  # noqa: PLC0415

        source = inspect.getsource(mod)
        self.assertIn('"/_oauth_usage_history"', source)


if __name__ == "__main__":
    unittest.main()
```

Note on `test_route_is_registered`: the app factory is `create_app` (src/smart_proxy/anthropic_proxy.py:2691). If it can be constructed cheaply with fakes, prefer asserting `"/_oauth_usage_history"` appears in `[r.resource.canonical for r in app.router.routes()]`; the source-scan fallback above is acceptable if construction needs live config.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_oauth_usage_history.py -v`
Expected: collection error — `ImportError: cannot import name '_oauth_usage_history_handler'`.

- [ ] **Step 3: Implement handler + route**

In `src/smart_proxy/anthropic_proxy.py`, after the `_oauth_usage_handler` function ends (~line 2016), add:

```python
def _span_days_between(prev_resets_at: str, resets_at: str) -> float | None:
    try:
        prev_dt = datetime.fromisoformat(prev_resets_at)
        new_dt = datetime.fromisoformat(resets_at)
        return round((new_dt - prev_dt).total_seconds() / 86400.0, 2)
    except (ValueError, TypeError):
        return None


async def _oauth_usage_history_handler(request: web.Request) -> web.Response:
    """GET observed rate-limit window history for OAuth keys.

    One record per observed window instance (see ``oauth_window_log``).
    ``span_days_since_prev`` — distance to the previous window of the same
    kind, i.e. the actual window length. ``?kind=seven_day`` filters by
    window kind; ``?limit=N`` caps records per kind (default 50, newest
    first). Auth follows the same ``oauth_usage_require_auth`` flag as
    ``/_oauth_usage``.
    """
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    db: Database = request.app["db"]

    if request.app.get("oauth_usage_require_auth") and not pool.check_auth(
        _extract_client_token(request)
    ):
        return web.json_response({"error": "unauthorized"}, status=401)

    kind_filter = request.query.get("kind") or None
    try:
        per_kind_limit = max(1, int(request.query.get("limit", "50")))
    except ValueError:
        per_kind_limit = 50

    rows = await db.list_anthropic_keys()
    keys_out: list[dict] = []
    for row in rows:
        if row.get("key_type") != "oauth":
            continue
        key_id = str(row.get("id", ""))
        log_rows = await db.list_oauth_window_log(key_id)
        windows: dict[str, list[dict]] = {}
        for log_row in log_rows:  # ordered window_kind ASC, resets_at ASC
            kind = log_row["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            bucket = windows.setdefault(kind, [])
            span = None
            if bucket:
                span = _span_days_between(
                    bucket[-1]["resets_at"], log_row["resets_at"]
                )
            bucket.append({
                "resets_at": log_row["resets_at"],
                "first_seen_at": log_row["first_seen_at"],
                "first_active_at": log_row["first_active_at"],
                "last_seen_at": log_row["last_seen_at"],
                "observations": log_row["observations"],
                "last_utilization": log_row["last_utilization"],
                "max_utilization": log_row["max_utilization"],
                "max_utilization_at": log_row["max_utilization_at"],
                "span_days_since_prev": span,
            })
        for bucket in windows.values():
            bucket.reverse()  # newest first
            del bucket[per_kind_limit:]
        keys_out.append({
            "id": key_id,
            "name": row.get("name"),
            "status": row.get("status"),
            "windows": windows,
        })

    return web.json_response({
        "generated_at": _utc_now_iso(),
        "keys": keys_out,
    })
```

Register the route next to the existing one (~line 2734):

```python
    app.router.add_get("/_oauth_usage", _oauth_usage_handler)
    app.router.add_get("/_oauth_usage_history", _oauth_usage_history_handler)
```

- [ ] **Step 4: Run tests, then the full suite**

Run: `.venv/bin/python -m pytest tests/test_anthropic_proxy_oauth_usage_history.py -v`
Expected: all pass.

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_proxy_oauth_usage_history.py
git commit -m "feat(anthropic-proxy): /_oauth_usage_history endpoint with observed window spans

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: end-to-end smoke against a real SQLite DB

**Files:**
- Test: `tests/test_oauth_window_tracking_e2e.py` (new)

**Interfaces:**
- Consumes: everything from Tasks 1–4: `_build_oauth_usage_payload` (via `_oauth_usage_handler`), `_oauth_usage_history_handler`, real `Database` with tempfile SQLite.
- Produces: nothing new — proves the pieces compose: poll → rows in `oauth_window_log` → history endpoint reports the reset span.

- [ ] **Step 1: Write the test**

Create `tests/test_oauth_window_tracking_e2e.py`:

```python
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request

from smart_proxy.anthropic_proxy import (
    AnthropicKeyPool,
    _oauth_usage_handler,
    _oauth_usage_history_handler,
)
from smart_proxy.db import Database


def _usage_payload(resets_at: str, utilization: float) -> dict:
    return {
        "seven_day": {"utilization": utilization, "resets_at": resets_at},
    }


class OauthWindowTrackingE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_polls_accumulate_and_history_reports_span(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                now = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat()
                await db.db.execute(
                    """INSERT INTO anthropic_keys
                       (id, key_type, status, access_token, refresh_token,
                        expires_at, name, created_at, updated_at)
                       VALUES (?, 'oauth', 'active', 'tok', 'ref',
                               9999999999999, 'e2e', ?, ?)""",
                    ("oauth-key-1", now, now),
                )
                await db.db.commit()

                responses = [
                    _usage_payload("2026-07-02T11:00:00.028998+00:00", 71.0),
                    _usage_payload("2026-07-02T11:00:00.874164+00:00", 72.0),
                    _usage_payload("2026-07-06T16:00:00.111111+00:00", 1.0),
                ]

                def _resp(payload: dict) -> MagicMock:
                    m = MagicMock()
                    m.status_code = 200
                    m.json.return_value = payload
                    return m

                mock_client = MagicMock()
                mock_client.get = AsyncMock(
                    side_effect=[_resp(p) for p in responses]
                )

                mock_pool = MagicMock()
                mock_pool.check_auth.return_value = True
                mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
                mock_pool.ensure_valid_token = AsyncMock(return_value="tok")
                mock_pool.get_loaded_key.return_value = None

                app = {
                    "anthropic_pool": mock_pool,
                    "http_client": mock_client,
                    "db": db,
                }
                for _ in responses:
                    resp = await _oauth_usage_handler(
                        make_mocked_request("GET", "/_oauth_usage", app=app)
                    )
                    self.assertEqual(resp.status, 200)

                hist = await _oauth_usage_history_handler(
                    make_mocked_request("GET", "/_oauth_usage_history", app=app)
                )
                body = json.loads(hist.body)
                seven = body["keys"][0]["windows"]["seven_day"]
                self.assertEqual(len(seven), 2)
                self.assertEqual(seven[0]["resets_at"], "2026-07-06T16:00:00+00:00")
                self.assertEqual(seven[0]["span_days_since_prev"], 4.21)
                self.assertEqual(seven[1]["observations"], 2)  # jittered dup merged
                self.assertEqual(seven[1]["max_utilization"], 72.0)
                self.assertEqual(seven[1]["last_utilization"], 72.0)
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
```

Note: the jittered duplicate (`.028998` vs `.874164`) landing in one row with `observations == 2` is the whole point of minute truncation — this test locks it in end-to-end. If `Database` has no `close()` method, check how other real-DB tests clean up (`tests/test_db_snapshot_roundtrip.py`) and match that.

- [ ] **Step 2: Run the test**

Run: `.venv/bin/python -m pytest tests/test_oauth_window_tracking_e2e.py -v`
Expected: PASS (all production code already exists after Tasks 1–4; this is a composition check, not TDD).

If it fails, debug the composition — most likely candidates: `_anthropic_key_from_row` requiring more columns in the INSERT (add them), or `Database` cleanup method name.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_oauth_window_tracking_e2e.py
git commit -m "test: e2e oauth window tracking from poll to history span

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
