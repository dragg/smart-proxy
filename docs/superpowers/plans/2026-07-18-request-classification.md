# Request Classification (helper / main / subagent) + Per-Session Attribution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Classify each Anthropic-proxy request as `helper` / `main` / `subagent` and attribute token usage to the originating Claude Code `session_id` (behind a proxy key), so the dashboard can show how much traffic/cost is automated vs user-driven and *whose* key owns expensive/never-cleared sessions.

**Architecture:** A pure classifier inspects the already-parsed request body (toolset + system-prompt size) and metadata (`session_id`). The proxy computes the classification once per request and passes it to the existing `UsageTracker`, which buffers two *new* aggregates (per-kind-daily, per-session) alongside the existing `usage_daily` buffer and flushes all three together. Two new **additive** tables (`usage_kind_daily`, `usage_session`) avoid any migration to the hot `usage_daily` table. New read APIs + a "Traffic" dashboard tab surface the breakdown.

**Tech Stack:** aiohttp proxy, aiosqlite base `Database` + `PostgresDatabase` (psycopg) via a `?`-placeholder adapter, Svelte 5 + Vite dashboard, Chart.js.

## Global Constraints

- **Do NOT modify `usage_daily` or its PRIMARY KEY.** All new storage is additive tables added to `SCHEMA_SQL` (fresh DBs) and to the `MIGRATIONS` list (existing DBs) using `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`.
- SQL uses `?` placeholders only (the Postgres adapter translates them). No backend-specific SQL except via the existing `self._backend`-switched builders.
- Classification is a **heuristic**, not a guaranteed flag. The main-vs-subagent split uses a system-prompt-size threshold `MAIN_SYSTEM_MIN_CHARS = 15000` (observed: main ≈ 27 KB, subagent ≈ 3–4 KB). Keep it a single named constant with a comment; it is expected to need occasional re-tuning across Claude Code versions.
- `request_kind` values are exactly the three strings `"helper"`, `"main"`, `"subagent"`, plus `"unknown"` as the column default for rows written before/without classification.
- Session rollup must carry `proxy_key` so a session is attributable to a key (owner name via `proxy_api_keys`).
- Run tests with `.venv/bin/python -m pytest` (system `python` lacks aiohttp). Force local DB in any manual check with `DATABASE_URL= DB_PATH=...`.
- Frontend: Svelte 5 runes (`$state`/`$derived`), event attrs `onclick=`; build with `npm run build` in `web/` (output is gitignored `src/smart_proxy/static/app/`).
- The new tables are created by the normal migration path on startup. No `usage_daily`-locking migration is involved, so the "stop both services" dance is **not** required — but both proxy processes must run the new code before the dashboard reads the new tables.

---

### Task 1: Pure request classifier

**Files:**
- Create: `src/smart_proxy/request_classify.py`
- Test: `tests/test_request_classify.py`

**Interfaces:**
- Produces: `classify_request(body: dict, *, user_agent: str = "", x_app: str = "") -> RequestClass` where `RequestClass` is a frozen dataclass with `kind: str` (`"helper"|"main"|"subagent"`), `session_id: str` (`""` if unknown), `entrypoint: str` (`""` if unknown). Also exports the constant `MAIN_SYSTEM_MIN_CHARS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_request_classify.py
from __future__ import annotations
import sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.request_classify import classify_request, RequestClass, MAIN_SYSTEM_MIN_CHARS


def _big(n): return "x" * n


class TestClassify:
    def test_no_tools_is_helper(self):
        rc = classify_request({"tools": [], "system": _big(50000)})
        assert rc.kind == "helper"

    def test_tools_plus_big_system_is_main(self):
        body = {"tools": [{"name": "Bash"}], "system": _big(MAIN_SYSTEM_MIN_CHARS + 1)}
        assert classify_request(body).kind == "main"

    def test_tools_plus_small_system_is_subagent(self):
        body = {"tools": [{"name": "Read"}], "system": _big(3000)}
        assert classify_request(body).kind == "subagent"

    def test_billing_block_excluded_from_system_size(self):
        # A tiny task system + a billing block must still classify as subagent.
        system = [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=1; " + _big(40000)},
            {"type": "text", "text": _big(3000)},
        ]
        body = {"tools": [{"name": "Read"}], "system": system}
        assert classify_request(body).kind == "subagent"

    def test_session_id_parsed_from_user_id_json(self):
        meta = {"user_id": '{"device_id":"d","account_uuid":"","session_id":"sess-123"}'}
        rc = classify_request({"tools": [{"name": "Bash"}], "system": _big(30000), "metadata": meta})
        assert rc.session_id == "sess-123"

    def test_session_id_absent_is_empty(self):
        assert classify_request({"tools": [], "metadata": {}}).session_id == ""
        assert classify_request({"tools": []}).session_id == ""

    def test_entrypoint_from_x_app_then_user_agent(self):
        assert classify_request({}, x_app="cli").entrypoint == "cli"
        assert classify_request({}, user_agent="claude-cli/2.1.92 (external, cli)").entrypoint == "cli"
        assert classify_request({}).entrypoint == ""

    def test_non_dict_body_is_helper(self):
        assert classify_request(None).kind == "helper"  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_request_classify.py -q`
Expected: FAIL (ModuleNotFoundError: smart_proxy.request_classify)

- [ ] **Step 3: Write minimal implementation**

```python
# src/smart_proxy/request_classify.py
"""Heuristic classification of Anthropic proxy requests.

Empirically (captured Claude Code traffic): the main agent loop sends a
~27 KB system prompt with the full toolset; a subagent (Task/Agent) shares
the same session_id but sends a stripped ~3-4 KB system prompt; background
helper calls (titles/summaries/quota) carry no tools. This is a heuristic,
not a guaranteed API flag, and the threshold may need re-tuning across
Claude Code versions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# Main-loop system prompt ≈ 27 KB; subagent ≈ 3-4 KB. Threshold sits well between.
MAIN_SYSTEM_MIN_CHARS = 15000

_BILLING_PREFIX = "x-anthropic-billing"


@dataclass(frozen=True)
class RequestClass:
    kind: str          # "helper" | "main" | "subagent"
    session_id: str    # "" if unknown
    entrypoint: str     # "cli" | "sdk-cli" | "" ...


def _system_text_len(system: object) -> int:
    """Total length of the request's system prompt, excluding the injected
    billing header block (which is not part of the agent instructions)."""
    if isinstance(system, str):
        return len(system)
    if isinstance(system, list):
        total = 0
        for block in system:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str) and not text.startswith(_BILLING_PREFIX):
                    total += len(text)
        return total
    return 0


def _session_id(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    uid = metadata.get("user_id")
    if not isinstance(uid, str):
        return ""
    try:
        obj = json.loads(uid)
    except Exception:
        return ""
    if isinstance(obj, dict):
        sid = obj.get("session_id")
        if isinstance(sid, str):
            return sid
    return ""


def _entrypoint(user_agent: str, x_app: str) -> str:
    if x_app.strip():
        return x_app.strip()
    ua = (user_agent or "").lower()
    if "claude-cli" in ua:
        return "cli"
    return ""


def classify_request(body: dict, *, user_agent: str = "", x_app: str = "") -> RequestClass:
    entrypoint = _entrypoint(user_agent, x_app)
    if not isinstance(body, dict):
        return RequestClass("helper", "", entrypoint)
    tools = body.get("tools")
    has_tools = isinstance(tools, list) and len(tools) > 0
    if not has_tools:
        kind = "helper"
    elif _system_text_len(body.get("system")) >= MAIN_SYSTEM_MIN_CHARS:
        kind = "main"
    else:
        kind = "subagent"
    return RequestClass(kind, _session_id(body.get("metadata")), entrypoint)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_request_classify.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/request_classify.py tests/test_request_classify.py
git commit -m "feat(classify): pure request classifier (helper/main/subagent + session_id)"
```

---

### Task 2: New additive tables + upsert builders + batch methods

**Files:**
- Modify: `src/smart_proxy/db.py` (add to `SCHEMA_SQL`, `MIGRATIONS`; add `build_usage_kind_upsert_sql`, `build_usage_session_upsert_sql`; add `upsert_usage_kind_batch`, `upsert_usage_session_batch`)
- Test: `tests/test_usage_kind_session_db.py`

**Interfaces:**
- Consumes: `Database` base class, `self._backend`, existing `_window_counter_updates`-style additive upsert pattern.
- Produces:
  - `upsert_usage_kind_batch(rows: list[tuple])` — tuple order: `(date, proxy_key, request_kind, provider, model, input, output, cache_read, cache_creation, cache_creation_5m, cache_creation_1h, web_search_requests, requests)`.
  - `upsert_usage_session_batch(rows: list[tuple])` — tuple order: `(session_id, proxy_key, provider, model, first_date, last_date, input, output, cache_read, cache_creation, cache_creation_5m, cache_creation_1h, web_search_requests, requests)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_kind_session_db.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import build_database_from_config


class KindSessionTables(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        return db

    async def asyncTearDown(self):
        t = getattr(self, "_tmp", None)
        if t: t.cleanup()

    async def test_kind_upsert_accumulates(self):
        db = await self._db()
        row = ("2026-07-18", "sp-a", "subagent", "anthropic", "m", 10, 5, 0, 0, 0, 0, 0, 1)
        await db.upsert_usage_kind_batch([row])
        await db.upsert_usage_kind_batch([row])
        cur = await db.db.execute(
            "SELECT input_tokens, requests FROM usage_kind_daily WHERE request_kind='subagent'")
        r = await cur.fetchone()
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (20, 2))

    async def test_session_upsert_tracks_first_last_and_sums(self):
        db = await self._db()
        await db.upsert_usage_session_batch(
            [("sess-1", "sp-a", "anthropic", "m", "2026-07-10", "2026-07-10", 10, 0, 0, 0, 0, 0, 0, 1)])
        await db.upsert_usage_session_batch(
            [("sess-1", "sp-a", "anthropic", "m", "2026-07-12", "2026-07-12", 5, 0, 0, 0, 0, 0, 0, 1)])
        cur = await db.db.execute(
            "SELECT first_date, last_date, input_tokens, requests FROM usage_session WHERE session_id='sess-1'")
        r = await cur.fetchone()
        self.assertEqual(r["first_date"], "2026-07-10")
        self.assertEqual(r["last_date"], "2026-07-12")
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (15, 2))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_db.py -q`
Expected: FAIL (`no such table: usage_kind_daily` / `AttributeError: upsert_usage_kind_batch`)

- [ ] **Step 3: Add schema to `SCHEMA_SQL` and `MIGRATIONS`**

Append to the `SCHEMA_SQL` string (near the other `CREATE TABLE` blocks, e.g. after the `usage_daily` block around `db.py:87`):

```sql
CREATE TABLE IF NOT EXISTS usage_kind_daily (
    date         TEXT    NOT NULL,
    proxy_key    TEXT    NOT NULL DEFAULT '',
    request_kind TEXT    NOT NULL DEFAULT 'unknown',
    provider     TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, proxy_key, request_kind, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_kind_daily_date ON usage_kind_daily(date);
CREATE TABLE IF NOT EXISTS usage_session (
    session_id   TEXT    NOT NULL,
    proxy_key    TEXT    NOT NULL DEFAULT '',
    provider     TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    first_date   TEXT    NOT NULL,
    last_date    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, proxy_key, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_session_key ON usage_session(proxy_key);
```

Add the same `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` statements as individual entries in the `MIGRATIONS` list (so existing DBs get them). Each statement is its own list element (the runner executes them one-by-one, ignoring "already exists").

- [ ] **Step 4: Add upsert builders (near `build_usage_upsert_sql`, ~db.py:620)**

```python
_KIND_COUNTERS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
    "cache_creation_5m_tokens", "cache_creation_1h_tokens", "web_search_requests", "requests",
)


def build_usage_kind_upsert_sql(backend: str) -> str:
    q = "usage_kind_daily." if backend == "postgres" else ""
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_kind_daily\n"
        "    (date, proxy_key, request_kind, provider, model,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(date, proxy_key, request_kind, provider, model) DO UPDATE SET\n"
        f"                   {sets}"
    )


def build_usage_session_upsert_sql(backend: str) -> str:
    q = "usage_session." if backend == "postgres" else ""
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_session\n"
        "    (session_id, proxy_key, provider, model, first_date, last_date,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(session_id, proxy_key, provider, model) DO UPDATE SET\n"
        f"                   first_date = MIN({q}first_date, excluded.first_date),\n"
        f"                   last_date  = MAX({q}last_date, excluded.last_date),\n"
        f"                   {sets}"
    )
```

- [ ] **Step 5: Add batch methods (near `upsert_usage_batch`, ~db.py:903)**

```python
    async def upsert_usage_kind_batch(self, rows: list[tuple]) -> None:
        if not rows:
            return
        await self.db.executemany(build_usage_kind_upsert_sql(self._backend), rows)
        await self.db.commit()

    async def upsert_usage_session_batch(self, rows: list[tuple]) -> None:
        if not rows:
            return
        await self.db.executemany(build_usage_session_upsert_sql(self._backend), rows)
        await self.db.commit()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_db.py -q`
Expected: PASS (2 passed). If MIN/MAX over TEXT dates behaves unexpectedly on Postgres, note dates are ISO `YYYY-MM-DD` so lexical MIN/MAX == chronological.

- [ ] **Step 7: Commit**

```bash
git add src/smart_proxy/db.py tests/test_usage_kind_session_db.py
git commit -m "feat(db): usage_kind_daily + usage_session additive tables and upserts"
```

---

### Task 3: Extend UsageTracker to buffer kind + session

**Files:**
- Modify: `src/smart_proxy/usage.py` (`UsageTracker.record` + `UsageTracker.flush`)
- Test: `tests/test_usage_tracker_kind_session.py`

**Interfaces:**
- Consumes: `Database.upsert_usage_kind_batch`, `Database.upsert_usage_session_batch` (Task 2).
- Produces: `record(...)` gains keyword args `request_kind: str = "unknown"`, `session_id: str = ""`. `flush` still returns the `usage_daily` rows (unchanged contract for window attribution) but ALSO writes the kind + session batches.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_tracker_kind_session.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import build_database_from_config
from smart_proxy.usage import UsageTracker


class TrackerKindSession(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await self.db.connect()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_flush_writes_kind_and_session(self):
        t = UsageTracker()
        t.record("sp-a", "cred", "anthropic", "m", 10, 5,
                 request_kind="subagent", session_id="sess-1")
        t.record("sp-a", "cred", "anthropic", "m", 4, 2,
                 request_kind="subagent", session_id="sess-1")
        rows = await t.flush(self.db)
        self.assertEqual(len(rows), 1)  # usage_daily contract unchanged
        cur = await self.db.db.execute(
            "SELECT input_tokens, requests FROM usage_kind_daily WHERE request_kind='subagent'")
        r = await cur.fetchone()
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (14, 2))
        cur = await self.db.db.execute(
            "SELECT input_tokens, requests FROM usage_session WHERE session_id='sess-1' AND proxy_key='sp-a'")
        r = await cur.fetchone()
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (14, 2))

    async def test_empty_session_id_skips_session_table(self):
        t = UsageTracker()
        t.record("sp-a", "cred", "anthropic", "m", 1, 1, request_kind="helper", session_id="")
        await t.flush(self.db)
        cur = await self.db.db.execute("SELECT COUNT(*) AS n FROM usage_session")
        self.assertEqual(int((await cur.fetchone())["n"]), 0)
        cur = await self.db.db.execute(
            "SELECT COUNT(*) AS n FROM usage_kind_daily WHERE request_kind='helper'")
        self.assertEqual(int((await cur.fetchone())["n"]), 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_tracker_kind_session.py -q`
Expected: FAIL (`record() got an unexpected keyword argument 'request_kind'`)

- [ ] **Step 3: Extend `record` and `flush`**

In `UsageTracker.__init__`, add two buffers:
```python
        # kind key: (date, proxy_key, request_kind, provider, model) -> 8 counters
        self._kind_buf: dict[tuple, list[int]] = {}
        # session key: (session_id, proxy_key, provider, model) -> [first_date, last_date, 8 counters]
        self._session_buf: dict[tuple, list] = {}
```

Add params to `record(...)` signature: `request_kind: str = "unknown", session_id: str = ""` (append after `via_openai_compat`). At the end of `record`, after updating `acc`:
```python
        counters = (input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                    cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, 1)
        kkey = (date, proxy_key, request_kind or "unknown", provider, model)
        kacc = self._kind_buf.get(kkey)
        if kacc is None:
            kacc = [0] * 8
            self._kind_buf[kkey] = kacc
        for i, c in enumerate(counters):
            kacc[i] += c
        if session_id:
            skey = (session_id, proxy_key, provider, model)
            sacc = self._session_buf.get(skey)
            if sacc is None:
                sacc = [date, date] + [0] * 8
                self._session_buf[skey] = sacc
            sacc[0] = min(sacc[0], date)
            sacc[1] = max(sacc[1], date)
            for i, c in enumerate(counters):
                sacc[2 + i] += c
```

In `flush`, under the same lock snapshot the two new buffers and reset them, then after `db.upsert_usage_batch(rows)` write:
```python
        kind_rows = [(k[0], k[1], k[2], k[3], k[4], *v) for k, v in kind_snapshot.items()]
        session_rows = [(k[0], k[1], k[2], k[3], v[0], v[1], *v[2:]) for k, v in session_snapshot.items()]
        await db.upsert_usage_kind_batch(kind_rows)
        await db.upsert_usage_session_batch(session_rows)
```
(Snapshot `self._kind_buf`/`self._session_buf` inside the `async with self._lock` block alongside the existing `snapshot = self._buf` reset.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_tracker_kind_session.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the existing usage-tracker tests (regression)**

Run: `.venv/bin/python -m pytest tests/ -k "usage" -q`
Expected: PASS (existing usage_daily behavior unchanged)

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/usage.py tests/test_usage_tracker_kind_session.py
git commit -m "feat(usage): buffer per-kind and per-session aggregates in UsageTracker"
```

---

### Task 4: Wire classification into the proxy request path

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` (compute `classify_request` once from the parsed body + headers; pass `request_kind`/`session_id` to `tracker.record` at ~line 1855)
- Test: `tests/test_proxy_classify_wiring.py` (unit-level: a helper that builds the record kwargs from a body)

**Interfaces:**
- Consumes: `classify_request` (Task 1), extended `tracker.record` (Task 3).

**Implementation notes:** The parsed request body is available in the handler as `data` (e.g. `data = json.loads(body)` around `anthropic_proxy.py:964`). Compute the classification once, near where `data` is first available, guarded so a malformed body never breaks forwarding:

```python
from smart_proxy.request_classify import classify_request
# ... after `data` (parsed request body) is available:
_rc = classify_request(
    data if isinstance(data, dict) else {},
    user_agent=request.headers.get("user-agent", ""),
    x_app=request.headers.get("x-app", ""),
)
```
Then extend the `tracker.record(...)` call (~1855) with:
```python
                    request_kind=_rc.kind,
                    session_id=_rc.session_id,
```

- [ ] **Step 1: Write the failing test** — extract a tiny helper `record_kwargs_for(data, headers)` in `anthropic_proxy.py` that returns `{"request_kind":..., "session_id":...}` so the wiring is unit-testable without a live request:

```python
# tests/test_proxy_classify_wiring.py
from __future__ import annotations
import sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.anthropic_proxy import record_kwargs_for


def test_subagent_wiring():
    body = {"tools": [{"name": "Read"}], "system": "x" * 3000,
            "metadata": {"user_id": '{"session_id":"s9"}'}}
    kw = record_kwargs_for(body, {"user-agent": "claude-cli/2 (external, cli)"})
    assert kw == {"request_kind": "subagent", "session_id": "s9"}


def test_malformed_body_defaults_helper():
    assert record_kwargs_for("nope", {})["request_kind"] == "helper"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_proxy_classify_wiring.py -q`
Expected: FAIL (`ImportError: cannot import name 'record_kwargs_for'`)

- [ ] **Step 3: Implement the helper + wire it in**

Add near the top-level helpers of `anthropic_proxy.py`:
```python
def record_kwargs_for(data: object, headers) -> dict:
    """Classification kwargs for UsageTracker.record; never raises."""
    from smart_proxy.request_classify import classify_request
    try:
        rc = classify_request(
            data if isinstance(data, dict) else {},
            user_agent=headers.get("user-agent", "") if headers else "",
            x_app=headers.get("x-app", "") if headers else "",
        )
        return {"request_kind": rc.kind, "session_id": rc.session_id}
    except Exception:
        return {"request_kind": "unknown", "session_id": ""}
```
At the `tracker.record(...)` call site (~1855), spread the kwargs:
```python
                tracker.record(
                    usage_proxy_key, key.key_id, "anthropic", model,
                    usage[0], usage[1],
                    usage[2] if len(usage) > 2 else 0,
                    usage[3] if len(usage) > 3 else 0,
                    usage[4] if len(usage) > 4 else 0,
                    usage[5] if len(usage) > 5 else 0,
                    web_search_requests=usage[6] if len(usage) > 6 else 0,
                    group_name=(key.name or "").strip() or None,
                    via_openai_compat=request.headers.get("x-smart-proxy-openai-compat") == "1",
                    **record_kwargs_for(data, request.headers),
                )
```
Confirm `data` is in scope at this point in the handler; if the streaming branch uses a differently-named variable for the parsed request body, thread the already-parsed body (compute `record_kwargs_for` once where the body is parsed and reuse the dict).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_proxy_classify_wiring.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Full backend regression**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_proxy_classify_wiring.py
git commit -m "feat(proxy): classify each request and attribute kind + session to usage"
```

---

### Task 5: Read queries for kind breakdown + top sessions

**Files:**
- Modify: `src/smart_proxy/db.py` (add `query_usage_by_kind`, `query_top_sessions`)
- Test: `tests/test_usage_kind_session_queries.py`

**Interfaces:**
- Produces:
  - `query_usage_by_kind(start_date, end_date) -> list[dict]` rows: `{proxy_key, key_name, request_kind, provider, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests}` (grouped, joined to `proxy_api_keys` for `key_name`).
  - `query_top_sessions(limit: int = 50) -> list[dict]` rows: `{session_id, proxy_key, key_name, provider, model, first_date, last_date, input_tokens, ..., requests}` ordered by total tokens desc.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_usage_kind_session_queries.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import build_database_from_config


class Queries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await self.db.connect()
        await self.db.add_proxy_key("sp-alpha-key", "Petya")
        await self.db.upsert_usage_kind_batch([
            ("2026-07-18", "sp-alpha-key", "subagent", "anthropic", "m", 100, 0, 0, 0, 0, 0, 0, 3),
            ("2026-07-18", "sp-alpha-key", "main", "anthropic", "m", 50, 0, 0, 0, 0, 0, 0, 2),
        ])
        await self.db.upsert_usage_session_batch([
            ("sess-big", "sp-alpha-key", "anthropic", "m", "2026-07-10", "2026-07-18", 999, 0, 0, 0, 0, 0, 0, 40),
            ("sess-small", "sp-alpha-key", "anthropic", "m", "2026-07-18", "2026-07-18", 5, 0, 0, 0, 0, 0, 0, 1),
        ])

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_by_kind_joins_key_name(self):
        rows = await self.db.query_usage_by_kind("2026-07-01", "2026-07-31")
        kinds = {r["request_kind"]: r for r in rows}
        self.assertEqual(kinds["subagent"]["key_name"], "Petya")
        self.assertEqual(int(kinds["subagent"]["input_tokens"]), 100)

    async def test_top_sessions_ordered_and_named(self):
        rows = await self.db.query_top_sessions(limit=10)
        self.assertEqual(rows[0]["session_id"], "sess-big")
        self.assertEqual(rows[0]["key_name"], "Petya")
        self.assertGreater(int(rows[0]["input_tokens"]), int(rows[1]["input_tokens"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_queries.py -q`
Expected: FAIL (`AttributeError: query_usage_by_kind`)

- [ ] **Step 3: Implement the queries** (mirror the `query_usage` join style, ~db.py:927)

```python
    async def query_usage_by_kind(self, start_date: str, end_date: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT u.proxy_key, COALESCE(pk.name,'') AS key_name, u.request_kind,
                      u.provider, u.model,
                      SUM(u.input_tokens) AS input_tokens,
                      SUM(u.output_tokens) AS output_tokens,
                      SUM(u.cache_read_tokens) AS cache_read_tokens,
                      SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                      SUM(u.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                      SUM(u.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                      SUM(u.web_search_requests) AS web_search_requests,
                      SUM(u.requests) AS requests
               FROM usage_kind_daily u
               LEFT JOIN proxy_api_keys pk ON pk.key = u.proxy_key
               WHERE u.date >= ? AND u.date <= ?
               GROUP BY u.proxy_key, key_name, u.request_kind, u.provider, u.model""",
            (start_date, end_date),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def query_top_sessions(self, limit: int = 50) -> list[dict]:
        cur = await self.db.execute(
            """SELECT s.session_id, s.proxy_key, COALESCE(pk.name,'') AS key_name,
                      s.provider, s.model, MIN(s.first_date) AS first_date,
                      MAX(s.last_date) AS last_date,
                      SUM(s.input_tokens) AS input_tokens,
                      SUM(s.output_tokens) AS output_tokens,
                      SUM(s.cache_read_tokens) AS cache_read_tokens,
                      SUM(s.cache_creation_tokens) AS cache_creation_tokens,
                      SUM(s.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                      SUM(s.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                      SUM(s.web_search_requests) AS web_search_requests,
                      SUM(s.requests) AS requests
               FROM usage_session s
               LEFT JOIN proxy_api_keys pk ON pk.key = s.proxy_key
               GROUP BY s.session_id, s.proxy_key, key_name, s.provider, s.model
               ORDER BY (SUM(s.input_tokens) + SUM(s.output_tokens)
                         + SUM(s.cache_read_tokens) + SUM(s.cache_creation_tokens)) DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_queries.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_usage_kind_session_queries.py
git commit -m "feat(db): query_usage_by_kind + query_top_sessions"
```

---

### Task 6: Dashboard API endpoints

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (add `_api_usage_kinds`, `_api_sessions`; register routes)
- Modify: `src/smart_proxy/usage_dashboard.py` (add `build_usage_kind_json(rows, prices)` and `build_sessions_json(rows, prices)` that attach per-row cost via `calculate_cost`)
- Test: `tests/test_dashboard_api.py` (add cases mirroring the existing read-endpoint tests)

**Interfaces:**
- Consumes: `query_usage_by_kind`, `query_top_sessions` (Task 5); `calculate_cost` / `build_price_lookup` (existing).
- Produces:
  - `GET /api/usage/kinds?start&end` → `{start, end, kinds: [{request_kind, key_name, proxy_key, requests, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, web_search_requests, base_cost, cache_cost, cost}]}` (aggregated per `(request_kind, proxy_key)` with cost summed across models).
  - `GET /api/sessions?limit` → `{sessions: [{session_id, key_name, proxy_key, first_date, last_date, requests, input_tokens, ..., cost}]}` (cost summed across models per session).

- [ ] **Step 1: Write the failing test** (follow the existing `_pool()` + `make_mocked_request` pattern in `tests/test_dashboard_api.py`; assert 401 without auth and correct shape with a mocked `db.query_usage_by_kind`/`query_top_sessions` and `db.get_all_model_prices`).

```python
class ApiKindsSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_kinds_unauthorized(self):
        req = make_mocked_request("GET", "/api/usage/kinds",
                                  app={"anthropic_pool": _pool(), "db": MagicMock()})
        self.assertEqual((await dashboard_api._api_usage_kinds(req)).status, 401)

    async def test_kinds_shape(self):
        db = MagicMock()
        db.query_usage_by_kind = AsyncMock(return_value=[
            {"proxy_key": "sp-a", "key_name": "Petya", "request_kind": "subagent",
             "provider": "anthropic", "model": "m", "input_tokens": 100, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 3}])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request("GET", "/api/usage/kinds?start=2026-07-01&end=2026-07-31",
                                  app={"anthropic_pool": _pool(), "db": db},
                                  headers={"Authorization": "Bearer sp-team"})
        import json
        body = json.loads((await dashboard_api._api_usage_kinds(req)).body)
        self.assertEqual(body["kinds"][0]["request_kind"], "subagent")
        self.assertEqual(body["kinds"][0]["key_name"], "Petya")
```

- [ ] **Step 2: Run test to verify it fails** — `.venv/bin/python -m pytest tests/test_dashboard_api.py -k KindsSessions -q` → FAIL.

- [ ] **Step 3: Implement builders + handlers + routes.** In `usage_dashboard.py`, `build_usage_kind_json(rows, prices)` groups rows by `(request_kind, proxy_key)`, computes `base_cost`/`cache_cost`/`cost` per model via `calculate_cost` (reuse the split from `_build_usage_cost_groups`), and returns the aggregated list. `build_sessions_json(rows, prices)` sums cost per `(session_id, proxy_key)`. In `dashboard_api.py`:

```python
async def _api_usage_kinds(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    db = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    start = request.query.get("start", ""); end = request.query.get("end", "")
    rows = await db.query_usage_by_kind(start, end)
    prices = build_price_lookup(await db.get_all_model_prices())
    return web.json_response({"start": start, "end": end,
                             "kinds": build_usage_kind_json(rows, prices)})

async def _api_sessions(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    db = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    try:
        limit = max(1, min(500, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    rows = await db.query_top_sessions(limit)
    prices = build_price_lookup(await db.get_all_model_prices())
    return web.json_response({"sessions": build_sessions_json(rows, prices)})
```
Register in `register_dashboard_api`: `app.router.add_get("/api/usage/kinds", _api_usage_kinds)` and `app.router.add_get("/api/sessions", _api_sessions)` (before the SPA catch-all).

- [ ] **Step 4: Run test to verify it passes** — `.venv/bin/python -m pytest tests/test_dashboard_api.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py src/smart_proxy/usage_dashboard.py tests/test_dashboard_api.py
git commit -m "feat(api): /api/usage/kinds and /api/sessions endpoints"
```

---

### Task 7: Dashboard "Traffic" tab (kind breakdown + top sessions by key)

**Files:**
- Create: `web/src/views/TrafficView.svelte`
- Modify: `web/src/App.svelte` (add `traffic` tab), `web/src/app.css` (minor)
- Verify: `npm run build`

**Interfaces:**
- Consumes: `GET /api/usage/kinds?start&end`, `GET /api/sessions?limit`.

- [ ] **Step 1: Add the tab** to `App.svelte`: extend `Tab` to include `'traffic'`, add to `TABS`, add nav `<button ... >Traffic</button>`, import `TrafficView`, and `{:else if tab === 'traffic'}<TrafficView />`.

- [ ] **Step 2: Implement `TrafficView.svelte`** — two sections:
  1. **By kind**: fetch `/api/usage/kinds` for a default range (last 7 days, reuse the UTC preset helpers from `UsageView`); render a table `Kind · Key · Requests · Tokens · Cost` and a one-line summary "automated (helper+subagent) vs user (main)" % of cost.
  2. **Top sessions**: fetch `/api/sessions?limit=50`; render `Key · Session · First → Last · Requests · Tokens · Cost`, sorted by cost desc, so an owner (e.g. "Petya") with a huge session is obvious. Truncate `session_id` for display (`sid.slice(0,8)…`).

```svelte
<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet } from '../lib/api'
  type Kind = { request_kind: string; key_name: string; proxy_key: string; requests: number
    input_tokens: number; output_tokens: number; cache_read_tokens: number
    cache_creation_tokens: number; base_cost: number|null; cache_cost: number|null; cost: number|null }
  type Session = { session_id: string; key_name: string; proxy_key: string; first_date: string
    last_date: string; requests: number; input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number; cost: number|null }
  let kinds = $state<Kind[]>([]); let sessions = $state<Session[]>([]); let error = $state('')
  const n = (v: number) => v.toLocaleString()
  const utcYMD = (d: Date) => d.toISOString().slice(0,10)
  async function load() {
    try {
      const end = utcYMD(new Date())
      const start = utcYMD(new Date(Date.now() - 6*864e5))
      kinds = (await apiGet<{kinds: Kind[]}>(`/api/usage/kinds?start=${start}&end=${end}`)).kinds
      sessions = (await apiGet<{sessions: Session[]}>('/api/sessions?limit=50')).sessions
    } catch (e) { error = String(e) }
  }
  onMount(load)
</script>
{#if error}<p class="err">{error}</p>{/if}
<h2>By request kind (last 7 days)</h2>
<table><thead><tr><th>Kind</th><th>Key</th><th>Requests</th><th>Input</th><th>Cost</th></tr></thead>
<tbody>
  {#each kinds as k (k.request_kind + k.proxy_key)}
    <tr><td>{k.request_kind}</td><td>{k.key_name || k.proxy_key.slice(0,10)}</td>
      <td>{n(k.requests)}</td><td>{n(k.input_tokens)}</td>
      <td>{k.cost != null ? '$' + k.cost.toFixed(2) : '—'}</td></tr>
  {/each}
</tbody></table>
<h2>Top sessions by cost</h2>
<table><thead><tr><th>Key</th><th>Session</th><th>First → Last</th><th>Requests</th><th>Cost</th></tr></thead>
<tbody>
  {#each sessions as s (s.session_id + s.proxy_key)}
    <tr><td>{s.key_name || s.proxy_key.slice(0,10)}</td><td>{s.session_id.slice(0,8)}…</td>
      <td>{s.first_date} → {s.last_date}</td><td>{n(s.requests)}</td>
      <td>{s.cost != null ? '$' + s.cost.toFixed(2) : '—'}</td></tr>
  {/each}
</tbody></table>
```

- [ ] **Step 3: Build** — `cd web && npm run build` → clean.

- [ ] **Step 4: Commit**

```bash
git add web/src/views/TrafficView.svelte web/src/App.svelte web/src/app.css
git commit -m "feat(dashboard): Traffic tab — kind breakdown + top sessions by key"
```

---

### Task 8: Docs — classification caveats + deploy note

**Files:**
- Modify: `README.md` (short "Request classification" subsection) and the deployment runbook (note the new tables are created automatically by the migration path; no `usage_daily`-locking migration, but both proxy processes must run the new code before the dashboard reads the new tables).

- [ ] **Step 1** Write the docs paragraph: what `helper`/`main`/`subagent` mean, that it is a heuristic (system-size threshold, version-dependent), and that `session_id` groups a Claude Code conversation attributed to a proxy key.
- [ ] **Step 2** Commit: `git commit -m "docs: request classification + deploy note"`.

---

## Self-Review Notes

- **Spec coverage:** classify (T1) → store (T2) → accumulate (T3) → wire (T4) → query (T5) → API (T6) → UI (T7) → docs (T8). Covers the two locked decisions (separate tables; 3 kinds + per-session) and the mid-flight requirement (session attributed to `proxy_key`).
- **No `usage_daily` PK change** anywhere — only additive tables.
- **Type consistency:** counter tuple order is identical across `_KIND_COUNTERS`, the upsert builders, `UsageTracker.flush` row assembly, and the query column lists.
- **Heuristic honesty:** single tunable `MAIN_SYSTEM_MIN_CHARS`; docs state the limitation. `unknown` default covers unclassified/legacy rows.
- **Open follow-ups (not in scope):** initiating-vs-continuation dimension; `cc_entrypoint` persistence; openai-compat path classification (currently records `unknown`/no session).
