# Session Title + Per-Session Request-Kind Breakdown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In the Traffic tab's "Top sessions" table, show each session's per-request-kind breakdown (main/subagent/helper) as expandable sub-rows, plus a human-readable label = project folder (from the main request's system prompt) + a best-effort snippet of the first human-typed message.

**Architecture:** Follow the existing data flow: classify at request time (`request_classify.py`) → capture into the per-session buffer (`usage.py`) → upsert into a widened `usage_session` table (`db.py`) → query per (session, kind, model) → aggregate `kinds[]` in Python (`usage_dashboard.py`) → render expandable rows (`TrafficView.svelte`). The label is derived only from `main` requests and stored last-write-wins-non-empty.

**Tech Stack:** Python 3 (aiohttp, aiosqlite, psycopg), Svelte 5 runes + Vite, Chart.js. Tests: `unittest` via `.venv/bin/python -m pytest`.

**Spec:** `docs/superpowers/specs/2026-07-19-session-title-kind-design.md`

## Global Constraints

- **Branch first.** This plan runs on a feature branch `feat/session-title-kind` (never commit feature work directly to `main`). Never `git add -A` — the working tree holds secrets (`oauth*.json`, `smart-proxy.db.bak`, `anthropic-proxy-test/` captures). Stage only the explicit files each commit lists.
- **Canonical 17-column `usage_session` row order** (INSERT list, `?` count, and the `flush()` tuple MUST all match this exactly — sqlite silently misaligns without erroring):
  ```
  session_id, proxy_key, request_kind, provider, model,
  first_date, last_date, project, title,
  input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
  cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests
  ```
- **Backend portability:** every SQL change must work on sqlite AND Postgres. Postgres has no 2-arg `MIN/MAX` (use `LEAST/GREATEST`, already switched); the `?`→`$n` adapter is a blind `?`→`%s` replace, so avoid literal `?`/`%` inside SQL text.
- **`request_kind` values:** `"main" | "subagent" | "helper" | "unknown"`. Label (`project`, `title`) captured on `main` only; other kinds pass `""` and must never clobber a stored label.
- **Never point local mutating test runs at the prod DB.** Pure-sqlite unit tests use `smart_proxy.db.Database` (the base class is sqlite). Force sqlite in the suite with `DATABASE_URL=`.
- **Deployment (post-implementation, owner's action):** the `usage_session` PK swap breaks running old-code writers on both backends. Procedure: **stop both proxy services → `smart_proxy db migrate` → deploy new code → restart both** (same hazard as a `usage_daily`-locking migration).

---

## Task 1: Classification — project folder + prompt snippet

**Files:**
- Modify: `src/smart_proxy/request_classify.py`
- Test: `tests/test_request_classify.py`

**Interfaces:**
- Produces: `RequestClass(kind, session_id, entrypoint, project="", title="")` (two new trailing fields, defaulted for back-compat). Helpers `_project(system) -> str`, `_title_snippet(messages) -> str`. `classify_request` fills `project`/`title` only when `kind == "main"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_request_classify.py`:

```python
class ProjectAndTitleTests(unittest.TestCase):
    def _main_body(self, system, messages):
        # ≥15000-char system + tools ⇒ classified as "main"
        return {"system": system, "messages": messages, "tools": [{"name": "x"}]}

    def test_project_basenames_last_two_path_components(self):
        from smart_proxy.request_classify import _project
        sys_text = "x" * 100 + "\n# Environment\n - Primary working directory: /Users/n/Projects/Acme/api\n - more"
        self.assertEqual(_project(sys_text), "Acme/api")

    def test_project_handles_paths_with_spaces_and_list_system(self):
        from smart_proxy.request_classify import _project
        system = [{"type": "text", "text": "Primary working directory: /Users/n/My Projects/smart-proxy"}]
        self.assertEqual(_project(system), "My Projects/smart-proxy")

    def test_project_absent_returns_empty(self):
        from smart_proxy.request_classify import _project
        self.assertEqual(_project("no directory here"), "")
        self.assertEqual(_project(None), "")

    def test_snippet_skips_wrappers_and_finds_human_text(self):
        from smart_proxy.request_classify import _title_snippet
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nboot hook</system-reminder>"}]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "починить баг в логине"},
        ]
        self.assertEqual(_title_snippet(messages), "починить баг в логине")

    def test_snippet_skips_compaction_and_interrupt_preambles(self):
        from smart_proxy.request_classify import _title_snippet
        messages = [
            {"role": "user", "content": "This session is being continued from a previous conversation..."},
            {"role": "user", "content": "[Request interrupted by user]"},
            {"role": "user", "content": "add a logout button"},
        ]
        self.assertEqual(_title_snippet(messages), "add a logout button")

    def test_snippet_ignores_tool_result_blocks(self):
        from smart_proxy.request_classify import _title_snippet
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "content": "SECRET OUTPUT"}]},
            {"role": "user", "content": [{"type": "text", "text": "real ask"}]},
        ]
        self.assertEqual(_title_snippet(messages), "real ask")

    def test_snippet_truncates_to_140(self):
        from smart_proxy.request_classify import _title_snippet
        long = "a" * 300
        self.assertEqual(len(_title_snippet([{"role": "user", "content": long}])), 140)

    def test_snippet_defensive_on_garbage(self):
        from smart_proxy.request_classify import _title_snippet
        self.assertEqual(_title_snippet(None), "")
        self.assertEqual(_title_snippet([{"role": "user"}]), "")
        self.assertEqual(_title_snippet("not a list"), "")

    def test_classify_fills_label_only_for_main(self):
        from smart_proxy.request_classify import classify_request
        big = "Primary working directory: /a/smart-proxy\n" + ("z" * 20000)
        main = classify_request(self._main_body(big, [{"role": "user", "content": "hello there"}]))
        self.assertEqual(main.kind, "main")
        self.assertEqual(main.project, "a/smart-proxy")
        self.assertEqual(main.title, "hello there")
        # subagent: tools present but small system ⇒ no label
        sub = classify_request({"system": "Primary working directory: /a/smart-proxy", "tools": [{"name": "x"}],
                                "messages": [{"role": "user", "content": "do a subtask"}]})
        self.assertEqual(sub.kind, "subagent")
        self.assertEqual((sub.project, sub.title), ("", ""))
        # helper: no tools ⇒ no label
        helper = classify_request({"system": "s", "messages": [{"role": "user", "content": "ping"}]})
        self.assertEqual(helper.kind, "helper")
        self.assertEqual((helper.project, helper.title), ("", ""))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_request_classify.py::ProjectAndTitleTests -v`
Expected: FAIL (`_project`/`_title_snippet` not defined; `RequestClass` has no `project`).

- [ ] **Step 3: Implement**

In `src/smart_proxy/request_classify.py`, add `import os` and `import re` at the top (after `import json`), extend `RequestClass`, add the helpers, and fill the label in `classify_request`:

```python
import os
import re

_WORKDIR_RE = re.compile(r"Primary working directory:\s*(.+)")

# Prefixes that mark harness/context wrappers (not human-typed text). Includes
# the post-compaction and interrupt preambles, which are plain text — without
# them a compacted session's real title gets overwritten under last-write-wins.
_SNIPPET_SKIP_PREFIXES = (
    "<system-reminder", "<command-", "<local-command", "<persisted-output",
    "<user-", "Caveat:", "This session is being continued", "[Request interrupted",
)
```

Change the dataclass to:

```python
@dataclass(frozen=True)
class RequestClass:
    kind: str          # "helper" | "main" | "subagent"
    session_id: str    # "" if unknown
    entrypoint: str    # "cli" | "sdk-cli" | "" ...
    project: str = ""  # folder label from the main system prompt; "" otherwise
    title: str = ""    # best-effort human-prompt snippet (≤140 chars); "" otherwise
```

Add these helpers (near `_system_text_len`; kept separate so the classification threshold is unchanged):

```python
def _system_text(system: object) -> str:
    """System prompt text (str or list of blocks), excluding the billing block."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str) and not text.startswith(_BILLING_PREFIX):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _project(system: object) -> str:
    """Last one or two path components of 'Primary working directory: <path>'
    in the (main) system prompt. '' when absent. Never raises."""
    m = _WORKDIR_RE.search(_system_text(system))
    if not m:
        return ""
    path = m.group(1).strip().rstrip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    return "/".join(parts[-2:])


def _title_snippet(messages: object) -> str:
    """First genuine human-typed user text (wrappers/tool_results skipped),
    whitespace-collapsed, truncated to 140 chars. '' otherwise. Never raises."""
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        texts.append(t)
        for t in texts:
            stripped = t.strip()
            if not stripped or any(stripped.startswith(p) for p in _SNIPPET_SKIP_PREFIXES):
                continue
            return " ".join(stripped.split())[:140]
    return ""
```

Update `classify_request`'s return path:

```python
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
    project = title = ""
    if kind == "main":
        project = _project(body.get("system"))
        title = _title_snippet(body.get("messages"))
    return RequestClass(kind, _session_id(body.get("metadata")), entrypoint, project, title)
```

(`import os` is imported for symmetry with the spec but `_project` uses string split; leave `os` out if the linter flags it unused — remove the `import os` line in that case.)

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_request_classify.py -v`
Expected: PASS (all, including the pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/request_classify.py tests/test_request_classify.py
git commit -m "feat(classify): extract project folder + prompt snippet for main requests"
```

---

## Task 2: usage_session schema + migrations (sqlite guarded rebuild + Postgres 0008)

**Files:**
- Modify: `src/smart_proxy/db.py` — `SCHEMA_SQL` usage_session (~line 106), `MIGRATIONS` usage_session create (~line 446), `_run_migrations` (~808), add `_migrate_usage_session_add_kind_title`
- Modify: `src/smart_proxy/db_migrations.py` — append `0008_usage_session_kind_title` to `POSTGRES_MIGRATIONS`
- Test: `tests/test_usage_session_migration.py` (new)

**Interfaces:**
- Produces: `usage_session` with columns `(session_id, proxy_key, request_kind, provider, model, first_date, last_date, project, title, <8 counters>)`, PK `(session_id, proxy_key, request_kind, provider, model)`, index `idx_usage_session_key`. Existing rows preserved as `request_kind='unknown'`, `project=''`, `title=''`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_usage_session_migration.py`:

```python
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database

OLD_DDL = """
CREATE TABLE usage_session (
    session_id TEXT NOT NULL, proxy_key TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL, model TEXT NOT NULL,
    first_date TEXT NOT NULL, last_date TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0, cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, proxy_key, provider, model)
);
"""


class UsageSessionMigrationTests(unittest.TestCase):
    def test_fresh_db_has_new_shape(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "fresh.db"))
                await db.connect()
                cur = await db.db.execute("PRAGMA table_info(usage_session)")
                cols = {r["name"] for r in await cur.fetchall()}
                self.assertIn("request_kind", cols)
                self.assertIn("project", cols)
                self.assertIn("title", cols)
                await db.close()
        asyncio.run(run())

    def test_old_shape_rebuilds_once_preserving_rows(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                path = str(Path(td) / "old.db")
                # Seed an OLD-shape table with one row, before Database touches it.
                con = sqlite3.connect(path)
                con.executescript(OLD_DDL)
                con.execute(
                    "INSERT INTO usage_session VALUES "
                    "('s1','sp-k','anthropic','m','2026-07-01','2026-07-02',1,2,3,4,5,6,7,8)"
                )
                con.commit(); con.close()

                db = Database(path)
                await db.connect()  # runs migrations → rebuild
                cur = await db.db.execute("PRAGMA table_info(usage_session)")
                cols = {r["name"] for r in await cur.fetchall()}
                self.assertEqual({"request_kind", "project", "title"} & cols,
                                 {"request_kind", "project", "title"})
                cur = await db.db.execute(
                    "SELECT request_kind, project, title, input_tokens, requests "
                    "FROM usage_session WHERE session_id='s1'")
                row = await cur.fetchone()
                self.assertEqual(row["request_kind"], "unknown")
                self.assertEqual(row["project"], "")
                self.assertEqual(row["title"], "")
                self.assertEqual(int(row["input_tokens"]), 1)
                self.assertEqual(int(row["requests"]), 8)
                # Idempotent: second connect is a no-op (row still there).
                await db.close()
                db2 = Database(path)
                await db2.connect()
                cur = await db2.db.execute("SELECT COUNT(*) AS c FROM usage_session")
                self.assertEqual(int((await cur.fetchone())["c"]), 1)
                await db2.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_session_migration.py -v`
Expected: FAIL — `test_fresh_db_has_new_shape` fails (no `request_kind` column yet).

- [ ] **Step 3: Implement**

**(a)** In `src/smart_proxy/db.py`, replace the `usage_session` block inside `SCHEMA_SQL` (~line 106) AND the `usage_session` `CREATE TABLE IF NOT EXISTS` inside `MIGRATIONS` (~line 446) with the new shape (both must match):

```sql
CREATE TABLE IF NOT EXISTS usage_session (
    session_id   TEXT    NOT NULL,
    proxy_key    TEXT    NOT NULL DEFAULT '',
    request_kind TEXT    NOT NULL DEFAULT 'unknown',
    provider     TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    first_date   TEXT    NOT NULL,
    last_date    TEXT    NOT NULL,
    project      TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, proxy_key, request_kind, provider, model)
)
```

(In `SCHEMA_SQL` keep the trailing `;` and the `CREATE INDEX ... idx_usage_session_key` line that follows; in `MIGRATIONS` keep it as a list entry followed by the existing index-create string.)

**(b)** In `_run_migrations` (~line 808), add a call after `_add_usage_via_openai_compat_column`:

```python
    async def _run_migrations(self) -> None:
        for sql in MIGRATIONS:
            try:
                await self.db.execute(sql)
            except Exception:
                pass
        await self._add_usage_via_openai_compat_column()
        await self._migrate_usage_session_add_kind_title()
```

**(c)** Add the guarded rebuild method (mirror `_add_usage_via_openai_compat_column`, right after it):

```python
    async def _migrate_usage_session_add_kind_title(self) -> None:
        """Add request_kind (new PK member) + project/title to usage_session.

        SQLite cannot ALTER a PRIMARY KEY in place, so an existing table is
        rebuilt once; historical rows become request_kind='unknown',
        project='', title=''. Fresh DBs already carry the columns (SCHEMA_SQL)
        and skip. Idempotent — a no-op once request_kind exists.
        """
        cur = await self.db.execute("PRAGMA table_info(usage_session)")
        old_columns = [row["name"] for row in await cur.fetchall()]
        if not old_columns or "request_kind" in old_columns:
            return

        savepoint = "migrate_usage_session_kind_title"
        await self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            await self.db.execute("DROP TABLE IF EXISTS usage_session_new")
            await self.db.execute(
                """CREATE TABLE usage_session_new (
                    session_id   TEXT    NOT NULL,
                    proxy_key    TEXT    NOT NULL DEFAULT '',
                    request_kind TEXT    NOT NULL DEFAULT 'unknown',
                    provider     TEXT    NOT NULL,
                    model        TEXT    NOT NULL,
                    first_date   TEXT    NOT NULL,
                    last_date    TEXT    NOT NULL,
                    project      TEXT    NOT NULL DEFAULT '',
                    title        TEXT    NOT NULL DEFAULT '',
                    input_tokens             INTEGER NOT NULL DEFAULT 0,
                    output_tokens            INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
                    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
                    web_search_requests      INTEGER NOT NULL DEFAULT 0,
                    requests                 INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, proxy_key, request_kind, provider, model)
                )"""
            )
            await self.db.execute(
                "INSERT INTO usage_session_new "
                "(session_id, proxy_key, request_kind, provider, model, first_date, last_date, "
                " project, title, input_tokens, output_tokens, cache_read_tokens, "
                " cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, "
                " web_search_requests, requests) "
                "SELECT session_id, proxy_key, 'unknown', provider, model, first_date, last_date, "
                " '', '', input_tokens, output_tokens, cache_read_tokens, "
                " cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, "
                " web_search_requests, requests FROM usage_session"
            )
            await self.db.execute("DROP TABLE usage_session")
            await self.db.execute("ALTER TABLE usage_session_new RENAME TO usage_session")
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_session_key ON usage_session(proxy_key)"
            )
        except Exception:
            await self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            await self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            await self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        logger.info("Migrated usage_session: added request_kind/project/title")
```

**(d)** In `src/smart_proxy/db_migrations.py`, append to `POSTGRES_MIGRATIONS` (after `0007_usage_kind_session`, ~line 372):

```python
    (
        # usage_session gains request_kind (new PK member) + project/title.
        # Existing rows keep request_kind='unknown'; the constant PK member
        # can't collide (new key is a superset of the old unique key).
        "0008_usage_session_kind_title",
        (
            "ALTER TABLE usage_session ADD COLUMN IF NOT EXISTS request_kind TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE usage_session ADD COLUMN IF NOT EXISTS project TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE usage_session ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE usage_session DROP CONSTRAINT IF EXISTS usage_session_pkey",
            "ALTER TABLE usage_session ADD PRIMARY KEY (session_id, proxy_key, request_kind, provider, model)",
        ),
    ),
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_session_migration.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py src/smart_proxy/db_migrations.py tests/test_usage_session_migration.py
git commit -m "feat(db): widen usage_session with request_kind+project+title (sqlite rebuild + PG 0008)"
```

---

## Task 3: Session upsert SQL — request_kind key + project/title columns

**Files:**
- Modify: `src/smart_proxy/db.py` — `build_usage_session_upsert_sql` (~line 770), `upsert_usage_session_batch` docstring (~1069)
- Test: `tests/test_usage_kind_session_db.py` (extend)

**Interfaces:**
- Consumes: new `usage_session` shape (Task 2).
- Produces: `build_usage_session_upsert_sql(backend)` accepting the canonical 17-column tuple; `ON CONFLICT(session_id, proxy_key, request_kind, provider, model)`; counters additive, dates MIN/MAX (LEAST/GREATEST on PG), `project`/`title` non-empty last-write-wins. `upsert_usage_session_batch(rows)` consumes 17-tuples.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_usage_kind_session_db.py` (mirror the file's existing sqlite fixture — a `smart_proxy.db.Database` on a temp path):

```python
class SessionUpsertKindLabelTests(unittest.TestCase):
    def _row(self, kind, first, last, project, title, inp, req):
        # canonical 17-col order
        return ("s1", "sp-k", kind, "anthropic", "m", first, last, project, title,
                inp, 0, 0, 0, 0, 0, 0, req)

    def test_kind_in_key_and_label_last_write_wins_nonempty(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                # main row establishes the label; a later main row updates it;
                # a subagent row for the same session must NOT clobber it and
                # is a distinct PK row.
                await db.upsert_usage_session_batch([self._row("main", "2026-07-05", "2026-07-05", "smart-proxy", "first ask", 10, 1)])
                await db.upsert_usage_session_batch([self._row("main", "2026-07-02", "2026-07-06", "smart-proxy", "second ask", 5, 1)])
                await db.upsert_usage_session_batch([self._row("subagent", "2026-07-05", "2026-07-05", "", "", 100, 3)])

                cur = await db.db.execute(
                    "SELECT request_kind, first_date, last_date, project, title, input_tokens, requests "
                    "FROM usage_session WHERE session_id='s1' ORDER BY request_kind")
                rows = [dict(r) for r in await cur.fetchall()]
                self.assertEqual([r["request_kind"] for r in rows], ["main", "subagent"])
                main = rows[0]
                self.assertEqual(main["project"], "smart-proxy")
                self.assertEqual(main["title"], "second ask")       # last non-empty wins
                self.assertEqual(main["first_date"], "2026-07-02")  # MIN
                self.assertEqual(main["last_date"], "2026-07-06")   # MAX
                self.assertEqual(int(main["input_tokens"]), 15)     # additive
                self.assertEqual(int(main["requests"]), 2)
                sub = rows[1]
                self.assertEqual((sub["project"], sub["title"]), ("", ""))
                self.assertEqual(int(sub["input_tokens"]), 100)
                await db.close()
        asyncio.run(run())
```

(If `tempfile`/`Path`/`Database`/`asyncio` aren't already imported in this file, add them at the top matching the sibling test files.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_db.py::SessionUpsertKindLabelTests -v`
Expected: FAIL — INSERT has 14 columns / the 17-tuple raises a binding error.

- [ ] **Step 3: Implement**

Replace `build_usage_session_upsert_sql` (~line 770) with:

```python
def build_usage_session_upsert_sql(backend: str) -> str:
    q = "usage_session." if backend == "postgres" else ""
    # SQLite MIN/MAX take 2+ scalar args; Postgres needs LEAST/GREATEST.
    least_fn = "LEAST" if backend == "postgres" else "MIN"
    greatest_fn = "GREATEST" if backend == "postgres" else "MAX"
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_session\n"
        "    (session_id, proxy_key, request_kind, provider, model, first_date, last_date,\n"
        "     project, title,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(session_id, proxy_key, request_kind, provider, model) DO UPDATE SET\n"
        f"                   first_date = {least_fn}({q}first_date, excluded.first_date),\n"
        f"                   last_date  = {greatest_fn}({q}last_date, excluded.last_date),\n"
        f"                   project = CASE WHEN excluded.project <> '' THEN excluded.project ELSE {q}project END,\n"
        f"                   title   = CASE WHEN excluded.title   <> '' THEN excluded.title   ELSE {q}title END,\n"
        f"                   {sets}"
    )
```

Update the `upsert_usage_session_batch` docstring (~line 1069) tuple description to the canonical 17-column order:

```python
        """Batch-upsert per-session usage rows.

        Each tuple (canonical 17-col order):
          (session_id, proxy_key, request_kind, provider, model,
           first_date, last_date, project, title,
           input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)
        """
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_usage_kind_session_db.py
git commit -m "feat(db): session upsert keys by request_kind, merges project/title non-empty"
```

---

## Task 4: UsageTracker — capture project/title, widen session buffer

**Files:**
- Modify: `src/smart_proxy/usage.py` — `record` (~602), `_session_buf` init comment (~598), `flush` (~656)
- Test: `tests/test_usage_tracker_kind_session.py` (update existing)

**Interfaces:**
- Consumes: `upsert_usage_session_batch` 17-tuples (Task 3).
- Produces: `record(..., request_kind="unknown", session_id="", project="", title="")`. `_session_buf` key `(session_id, proxy_key, request_kind, provider, model)`, value `[first_date, last_date, project, title, <8 counters>]`. `flush` emits canonical 17-tuples.

- [ ] **Step 1: Update the test**

Open `tests/test_usage_tracker_kind_session.py`. Update the existing session-buffer assertions to the new key/value shape and add a label case. The key case:

```python
def test_record_session_buffers_kind_and_label(self):
    from smart_proxy.usage import UsageTracker
    t = UsageTracker(flush_interval=999)
    t.record("sp-k", "cred", "anthropic", "m", 10, 2,
             request_kind="main", session_id="s1", project="smart-proxy", title="hello")
    t.record("sp-k", "cred", "anthropic", "m", 5, 1,
             request_kind="subagent", session_id="s1")  # no label
    # two buffer entries: one per (session, kind)
    keys = sorted(t._session_buf.keys())
    assert keys == [("s1", "sp-k", "main", "anthropic", "m"),
                    ("s1", "sp-k", "subagent", "anthropic", "m")]
    main = t._session_buf[("s1", "sp-k", "main", "anthropic", "m")]
    # [first_date, last_date, project, title, 8 counters]
    assert main[2] == "smart-proxy"
    assert main[3] == "hello"
    assert main[4] == 10   # input_tokens
    sub = t._session_buf[("s1", "sp-k", "subagent", "anthropic", "m")]
    assert (sub[2], sub[3]) == ("", "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_tracker_kind_session.py -v`
Expected: FAIL — `record` has no `project`/`title`; buffer key lacks `request_kind`.

- [ ] **Step 3: Implement**

In `src/smart_proxy/usage.py`:

Update the `_session_buf` comment (~598):

```python
        # session key: (session_id, proxy_key, request_kind, provider, model)
        #   -> [first_date, last_date, project, title, 8 counters]
        self._session_buf: dict[tuple, list] = {}
```

Add `project`/`title` params to `record` (after `session_id`):

```python
        request_kind: str = "unknown",
        session_id: str = "",
        project: str = "",
        title: str = "",
    ) -> None:
```

Replace the `if session_id:` block (~645) with:

```python
        if session_id:
            skey = (session_id, proxy_key, request_kind or "unknown", provider, model)
            sacc = self._session_buf.get(skey)
            if sacc is None:
                sacc = [date, date, "", "", 0, 0, 0, 0, 0, 0, 0, 0]
                self._session_buf[skey] = sacc
            sacc[0] = min(sacc[0], date)
            sacc[1] = max(sacc[1], date)
            if project:
                sacc[2] = project
            if title:
                sacc[3] = title
            for i, c in enumerate(counters):
                sacc[4 + i] += c
```

In `flush` (~688), replace the `session_rows` comprehension:

```python
        session_rows = [
            (k[0], k[1], k[2], k[3], k[4],   # session_id, proxy_key, request_kind, provider, model
             v[0], v[1], v[2], v[3],          # first_date, last_date, project, title
             *v[4:])                          # 8 counters
            for k, v in session_snapshot.items()
        ]
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_tracker_kind_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/usage.py tests/test_usage_tracker_kind_session.py
git commit -m "feat(usage): buffer per-session request_kind + project/title"
```

---

## Task 5: Proxy wiring — record_kwargs_for returns project/title

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — `record_kwargs_for` (~856)
- Test: `tests/test_proxy_classify_wiring.py` (update existing)

**Interfaces:**
- Consumes: `classify_request` → `RequestClass.project/title` (Task 1); `record(..., project=, title=)` (Task 4).
- Produces: `record_kwargs_for(data, headers) -> {"request_kind", "session_id", "project", "title"}`.

- [ ] **Step 1: Update the test**

In `tests/test_proxy_classify_wiring.py`, the exact-dict assertion (~line 14) now includes the two new keys. Replace it with a main-request case:

```python
def test_record_kwargs_for_main_includes_project_and_title(self):
    from smart_proxy.anthropic_proxy import record_kwargs_for
    body = {
        "tools": [{"name": "x"}],
        "system": "Primary working directory: /a/smart-proxy\n" + ("z" * 20000),
        "messages": [{"role": "user", "content": "fix the login bug"}],
        "metadata": {"user_id": '{"session_id": "sess-9"}'},
    }
    kw = record_kwargs_for(body, {})
    assert kw == {"request_kind": "main", "session_id": "sess-9",
                  "project": "a/smart-proxy", "title": "fix the login bug"}

def test_record_kwargs_for_garbage_is_safe(self):
    from smart_proxy.anthropic_proxy import record_kwargs_for
    assert record_kwargs_for(None, {}) == {
        "request_kind": "unknown", "session_id": "", "project": "", "title": ""}
```

(Adjust the `headers` argument to whatever mapping type the existing test uses — an empty `dict` works since `record_kwargs_for` uses `.get`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_proxy_classify_wiring.py -v`
Expected: FAIL — returned dict lacks `project`/`title`.

- [ ] **Step 3: Implement**

In `src/smart_proxy/anthropic_proxy.py`, update `record_kwargs_for` (~856):

```python
def record_kwargs_for(data: object, headers) -> dict:
    try:
        rc = classify_request(
            data,
            user_agent=headers.get("User-Agent", ""),
            x_app=headers.get("x-app", ""),
        )
        return {
            "request_kind": rc.kind,
            "session_id": rc.session_id,
            "project": rc.project,
            "title": rc.title,
        }
    except Exception:
        return {"request_kind": "unknown", "session_id": "", "project": "", "title": ""}
```

(Preserve the existing header-key casing the function already used — match the current `headers.get(...)` keys verbatim; only the returned dict grows.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_proxy_classify_wiring.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_proxy_classify_wiring.py
git commit -m "feat(proxy): thread project/title from classification into record()"
```

---

## Task 6: query_top_sessions — per-kind rows + project/title

**Files:**
- Modify: `src/smart_proxy/db.py` — `query_top_sessions` (~1184)
- Test: `tests/test_usage_kind_session_queries.py` (extend + fix 17-col seeds)

**Interfaces:**
- Consumes: new `usage_session` shape.
- Produces: rows one per `(session_id, proxy_key, request_kind, provider, model)`, each with `key_name`, `request_kind`, `project`, `title`, `first_date`, `last_date`, all counters. Session ranking (by token volume, `LIMIT`) unchanged.

- [ ] **Step 1: Update/extend the test**

First fix the existing 14-col session seed tuples in this file to the new 17-col order (insert `request_kind` after `proxy_key`, `project`/`title` after `last_date`). Then add:

```python
def test_top_sessions_returns_per_kind_rows_with_label(self):
    async def run():
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            await db.upsert_usage_session_batch([
                ("s1","sp-k","main","anthropic","m","2026-07-01","2026-07-02","smart-proxy","opening ask",10,0,0,0,0,0,0,4),
                ("s1","sp-k","subagent","anthropic","m","2026-07-01","2026-07-01","","",100,0,0,0,0,0,0,20),
            ])
            rows = await db.query_top_sessions(50)
            kinds = sorted(r["request_kind"] for r in rows)
            assert kinds == ["main", "subagent"]
            main = next(r for r in rows if r["request_kind"] == "main")
            assert main["project"] == "smart-proxy"
            assert main["title"] == "opening ask"
            await db.close()
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_queries.py -v`
Expected: FAIL — `request_kind`/`project` not selected.

- [ ] **Step 3: Implement**

Replace the SELECT in `query_top_sessions` (~1202). Add `s.request_kind`, `MAX(s.project) AS project`, `MAX(s.title) AS title`, and add `s.request_kind` to `GROUP BY`. Keep per-model rows (do NOT collapse model):

```python
        cur = await self.db.execute(
            """SELECT s.session_id, s.proxy_key, COALESCE(pk.name, '') AS key_name,
                      s.request_kind,
                      s.provider, s.model,
                      MAX(s.project) AS project, MAX(s.title) AS title,
                      MIN(s.first_date) AS first_date,
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
               JOIN (
                   SELECT session_id, proxy_key,
                          SUM(input_tokens + output_tokens + cache_read_tokens
                              + cache_creation_tokens) AS total_tokens
                   FROM usage_session
                   GROUP BY session_id, proxy_key
                   ORDER BY total_tokens DESC
                   LIMIT ?
               ) top ON top.session_id = s.session_id AND top.proxy_key = s.proxy_key
               LEFT JOIN proxy_api_keys pk ON pk.key = s.proxy_key
               GROUP BY s.session_id, s.proxy_key, pk.name, s.request_kind,
                        s.provider, s.model, top.total_tokens
               ORDER BY top.total_tokens DESC""",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_queries.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_usage_kind_session_queries.py
git commit -m "feat(db): query_top_sessions returns per-kind rows with project/title"
```

---

## Task 7: build_sessions_json — kinds[] + project/title + API shape

**Files:**
- Modify: `src/smart_proxy/usage_dashboard.py` — `build_sessions_json` (~275)
- Test: `tests/test_usage_kind_session_queries.py` (build_sessions_json cases) + `tests/test_dashboard_api.py` (`ApiKindsSessionsTests`)

**Interfaces:**
- Consumes: `query_top_sessions` rows (Task 6); `_row_cost_split` (per-model pricing).
- Produces: one entry per `(session_id, proxy_key)` with existing fields plus `project`, `title`, and `kinds: [{request_kind, requests, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, web_search_requests, base_cost, cache_cost, cost, partial, unknown}]` (sorted by cost desc). `/api/sessions` returns this superset.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_usage_kind_session_queries.py` (or a `build_sessions_json` test module it already uses):

```python
def test_build_sessions_json_groups_kinds_and_label(self):
    from smart_proxy.usage_dashboard import build_sessions_json
    rows = [
        {"session_id": "s1", "proxy_key": "sp-k", "key_name": "nikolai",
         "request_kind": "main", "provider": "anthropic", "model": "m",
         "project": "smart-proxy", "title": "opening ask",
         "first_date": "2026-07-01", "last_date": "2026-07-02",
         "input_tokens": 10, "output_tokens": 0, "cache_read_tokens": 0,
         "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
         "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 4},
        {"session_id": "s1", "proxy_key": "sp-k", "key_name": "nikolai",
         "request_kind": "subagent", "provider": "anthropic", "model": "m",
         "project": "", "title": "",
         "first_date": "2026-07-01", "last_date": "2026-07-01",
         "input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 0,
         "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
         "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 20},
    ]
    prices = {}  # unpriced ⇒ cost unknown; we only assert structure here
    out = build_sessions_json(rows, prices)
    assert len(out) == 1
    s = out[0]
    assert s["project"] == "smart-proxy"
    assert s["title"] == "opening ask"
    assert s["requests"] == 24
    kinds = {k["request_kind"]: k for k in s["kinds"]}
    assert set(kinds) == {"main", "subagent"}
    assert kinds["main"]["requests"] == 4
    assert kinds["subagent"]["input_tokens"] == 100

def test_build_sessions_json_lenient_missing_kind(self):
    from smart_proxy.usage_dashboard import build_sessions_json
    rows = [{"session_id": "s2", "proxy_key": "k", "model": "m",
             "input_tokens": 1, "requests": 1}]  # no request_kind
    out = build_sessions_json(rows, {})
    assert out[0]["kinds"][0]["request_kind"] == "unknown"
```

Also, in `tests/test_dashboard_api.py::ApiKindsSessionsTests`, extend the sessions assertion to check an item has `project`, `title`, and a `kinds` list (mock rows may omit `request_kind` — allowed by lenient read).

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_queries.py -k build_sessions_json tests/test_dashboard_api.py::ApiKindsSessionsTests -v`
Expected: FAIL — no `kinds`/`project`/`title` in output.

- [ ] **Step 3: Implement**

Replace `build_sessions_json` (~275) with (keeps existing top-level aggregation; adds label + per-kind buckets, finalizes kinds dict → sorted list):

```python
def build_sessions_json(rows: list[dict], prices: dict) -> list[dict]:
    """Aggregate ``query_top_sessions`` rows (one per session_id/proxy_key/
    request_kind/provider/model) into one entry per ``(session_id, proxy_key)``,
    summing tokens/cost across models, merging first/last dates, carrying the
    project/title label, and bucketing per request_kind into ``kinds``. Re-sort
    by total cost descending (for ``/api/sessions``). Per-model rows are kept so
    ``_row_cost_split`` can price each model correctly."""
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        session_id = str(row.get("session_id") or "")
        proxy_key = str(row.get("proxy_key") or "")
        request_kind = str(row.get("request_kind") or "unknown")
        group_id = (session_id, proxy_key)
        first_date = row.get("first_date")
        last_date = row.get("last_date")

        group = groups.get(group_id)
        if group is None:
            group = {
                "session_id": session_id,
                "proxy_key": proxy_key,
                "key_name": str(row.get("key_name") or ""),
                "project": "",
                "title": "",
                "first_date": first_date,
                "last_date": last_date,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "web_search_requests": 0,
                "base_cost": 0.0,
                "cache_cost": 0.0,
                "cost": 0.0,
                "partial": False,
                "unknown": False,
                "kinds": {},
            }
            groups[group_id] = group
        else:
            if first_date is not None and (
                group["first_date"] is None or first_date < group["first_date"]
            ):
                group["first_date"] = first_date
            if last_date is not None and (
                group["last_date"] is None or last_date > group["last_date"]
            ):
                group["last_date"] = last_date

        # First non-empty label wins (the main row supplies it).
        proj = str(row.get("project") or "")
        if proj and not group["project"]:
            group["project"] = proj
        ttl = str(row.get("title") or "")
        if ttl and not group["title"]:
            group["title"] = ttl

        cost, base_cost, cache_cost = _row_cost_split(row, prices)

        # Top-level totals.
        group["requests"] += _int_usage(row, "requests")
        group["input_tokens"] += _int_usage(row, "input_tokens")
        group["output_tokens"] += _int_usage(row, "output_tokens")
        group["cache_read_tokens"] += _int_usage(row, "cache_read_tokens")
        group["cache_creation_tokens"] += _int_usage(row, "cache_creation_tokens")
        group["web_search_requests"] += _int_usage(row, "web_search_requests")
        group["partial"] = group["partial"] or cost.partial
        group["unknown"] = group["unknown"] or cost.unknown_pricing
        if cost.total_cost is not None:
            group["base_cost"] += base_cost
            group["cache_cost"] += cache_cost
            group["cost"] += cost.total_cost

        # Per-kind bucket.
        kind = group["kinds"].get(request_kind)
        if kind is None:
            kind = {
                "request_kind": request_kind,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "web_search_requests": 0,
                "base_cost": 0.0,
                "cache_cost": 0.0,
                "cost": 0.0,
                "partial": False,
                "unknown": False,
            }
            group["kinds"][request_kind] = kind
        kind["requests"] += _int_usage(row, "requests")
        kind["input_tokens"] += _int_usage(row, "input_tokens")
        kind["output_tokens"] += _int_usage(row, "output_tokens")
        kind["cache_read_tokens"] += _int_usage(row, "cache_read_tokens")
        kind["cache_creation_tokens"] += _int_usage(row, "cache_creation_tokens")
        kind["web_search_requests"] += _int_usage(row, "web_search_requests")
        kind["partial"] = kind["partial"] or cost.partial
        kind["unknown"] = kind["unknown"] or cost.unknown_pricing
        if cost.total_cost is not None:
            kind["base_cost"] += base_cost
            kind["cache_cost"] += cache_cost
            kind["cost"] += cost.total_cost

    result = []
    for group in groups.values():
        group["kinds"] = sorted(
            group["kinds"].values(), key=lambda k: k["cost"], reverse=True
        )
        result.append(group)
    return sorted(result, key=_session_sort_metric, reverse=True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_usage_kind_session_queries.py tests/test_dashboard_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/usage_dashboard.py tests/test_usage_kind_session_queries.py tests/test_dashboard_api.py
git commit -m "feat(dashboard): build_sessions_json emits kinds[] + project/title label"
```

---

## Task 8: Snapshot specs + old-import defaulting

**Files:**
- Modify: `src/smart_proxy/db.py` — `SNAPSHOT_TABLE_SPECS` usage_session entry (~502), base `_encode_snapshot_value` (~901), add module-level defaults map
- Test: `tests/test_db_snapshot_roundtrip.py` (extend + fix 17-col seeds)

**Interfaces:**
- Consumes: new `usage_session` shape.
- Produces: snapshot export/import carrying `request_kind`, `project`, `title`; old snapshots (missing those keys) import with defaults `'unknown'`/`''`/`''` instead of raising `NOT NULL`.

- [ ] **Step 1: Write the failing test**

Fix any 14-col `usage_session` seed tuples in `tests/test_db_snapshot_roundtrip.py` to the 17-col order, then add:

```python
def test_snapshot_roundtrip_new_columns(self):
    async def run():
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "a.db"))
            await db.connect()
            await db.upsert_usage_session_batch([
                ("s1","sp-k","main","anthropic","m","2026-07-01","2026-07-02","smart-proxy","hi",10,0,0,0,0,0,0,4),
            ])
            snap = await db.export_snapshot()
            db2 = Database(str(Path(td) / "b.db"))
            await db2.connect()
            await db2.replace_snapshot(snap)
            cur = await db2.db.execute(
                "SELECT request_kind, project, title FROM usage_session WHERE session_id='s1'")
            row = await cur.fetchone()
            assert (row["request_kind"], row["project"], row["title"]) == ("main", "smart-proxy", "hi")
            await db.close(); await db2.close()
    asyncio.run(run())

def test_snapshot_import_defaults_missing_new_columns(self):
    async def run():
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "c.db"))
            await db.connect()
            # Old-shape snapshot row: no request_kind/project/title keys.
            old_snap = {"usage_session": [{
                "session_id": "s9", "proxy_key": "sp-k", "provider": "anthropic",
                "model": "m", "first_date": "2026-07-01", "last_date": "2026-07-01",
                "input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
                "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 1,
            }]}
            await db.replace_snapshot(old_snap)  # must not raise
            cur = await db.db.execute(
                "SELECT request_kind, project, title FROM usage_session WHERE session_id='s9'")
            row = await cur.fetchone()
            assert (row["request_kind"], row["project"], row["title"]) == ("unknown", "", "")
            await db.close()
    asyncio.run(run())
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_db_snapshot_roundtrip.py -v`
Expected: FAIL — spec lacks new columns; old-import raises `NOT NULL`.

- [ ] **Step 3: Implement**

Update the `usage_session` entry in `SNAPSHOT_TABLE_SPECS` (~502):

```python
    (
        "usage_session",
        (
            "session_id", "proxy_key", "request_kind", "provider", "model",
            "first_date", "last_date", "project", "title",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cache_creation_5m_tokens",
            "cache_creation_1h_tokens", "web_search_requests", "requests",
        ),
        "session_id, proxy_key, request_kind, provider, model",
    ),
```

Add a module-level defaults map (near `SNAPSHOT_TABLE_SPECS`):

```python
# Import-time defaults for columns absent from older snapshots (their NOT NULL
# columns would otherwise get None → IntegrityError on restore).
_SNAPSHOT_COLUMN_DEFAULTS: dict[tuple[str, str], object] = {
    ("usage_session", "request_kind"): "unknown",
    ("usage_session", "project"): "",
    ("usage_session", "title"): "",
}
```

Replace the base `_encode_snapshot_value` (~901) body:

```python
    def _encode_snapshot_value(
        self,
        table: str,
        column: str,
        value,  # noqa: ANN001
    ):
        if value is None:
            default = _SNAPSHOT_COLUMN_DEFAULTS.get((table, column))
            if default is not None:
                return default
        return value
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_db_snapshot_roundtrip.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_db_snapshot_roundtrip.py
git commit -m "feat(db): snapshot carries session kind/label; old imports default safely"
```

---

## Task 9: Traffic UI — Title column + expandable per-kind sub-rows

**Files:**
- Modify: `web/src/views/TrafficView.svelte`
- Build: `cd web && npm run build`

**Interfaces:**
- Consumes: `/api/sessions` items with `project`, `title`, `kinds[]` (Task 7).

- [ ] **Step 1: Extend the `Session` type + add a `Kind`-per-session type and expand state**

In the `<script>` block, extend the `Session` type and add expand state:

```ts
  type SessionKind = {
    request_kind: string; requests: number
    input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number
    web_search_requests: number
    base_cost: number | null; cache_cost: number | null; cost: number | null
    partial: boolean; unknown: boolean
  }
  type Session = {
    session_id: string; proxy_key: string; key_name: string
    project: string; title: string
    first_date: string | null; last_date: string | null; requests: number
    input_tokens: number; output_tokens: number
    cache_read_tokens: number; cache_creation_tokens: number
    web_search_requests: number
    base_cost: number | null; cache_cost: number | null; cost: number | null
    partial: boolean; unknown: boolean
    kinds: SessionKind[]
  }

  let expanded = $state<Set<string>>(new Set())
  const skey = (s: Session) => `${s.session_id}:${s.proxy_key}`
  function toggle(s: Session) {
    const k = skey(s)
    const next = new Set(expanded)
    next.has(k) ? next.delete(k) : next.add(k)
    expanded = next
  }
  const sessionLabel = (s: Session) => s.project || (s.title ? '' : '—')
```

- [ ] **Step 2: Replace the Top-sessions table markup**

Replace the `<h2>Top sessions by cost</h2>` table (~line 121) with an expandable version (Title column, `▸/▾` affordance, per-kind sub-rows):

```svelte
<h2>Top sessions by cost</h2>
{#if sessions.length === 0 && !error}
  <p class="muted">No sessions recorded yet.</p>
{/if}
{#if sessions.length > 0}
  <table>
    <thead>
      <tr>
        <th></th><th>Key</th><th>Title</th><th>Session</th>
        <th>First &rarr; Last</th><th>Requests</th><th>Cost</th>
      </tr>
    </thead>
    <tbody>
      {#each sessions as s (`${s.session_id}:${s.proxy_key}`)}
        <tr class="session-row" onclick={() => toggle(s)}>
          <td class="expander">{expanded.has(skey(s)) ? '▾' : '▸'}</td>
          <td>{keyLabel(s)}</td>
          <td class="title-cell">
            {#if s.project}<span class="project">{s.project}</span>{/if}
            {#if s.title}<span class="snippet" title={s.title}>{s.title}</span>{/if}
            {#if !s.project && !s.title}—{/if}
          </td>
          <td>{s.session_id ? s.session_id.slice(0, 8) + '…' : '—'}</td>
          <td>{s.first_date ?? '—'} &rarr; {s.last_date ?? '—'}</td>
          <td>{n(s.requests)}</td>
          <td>{fmtCost(s.cost, s.unknown, s.partial)}</td>
        </tr>
        {#if expanded.has(skey(s))}
          {#each s.kinds as k (`${s.session_id}:${s.proxy_key}:${k.request_kind}`)}
            <tr class="kind-row">
              <td></td><td></td>
              <td class="kind-name">{k.request_kind}</td>
              <td></td><td></td>
              <td>{n(k.requests)}</td>
              <td>{fmtCost(k.cost, k.unknown, k.partial)}</td>
            </tr>
          {/each}
        {/if}
      {/each}
    </tbody>
  </table>
{/if}
```

- [ ] **Step 3: Add minimal styles**

Add to the component's `<style>` (create the block if absent):

```css
  .session-row { cursor: pointer; }
  .expander { width: 1.2em; color: #888; user-select: none; }
  .title-cell .project { font-weight: 600; }
  .title-cell .snippet { display: block; color: #888; font-size: 0.85em;
    max-width: 32ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kind-row td { color: #666; font-size: 0.9em; }
  .kind-row .kind-name { padding-left: 1.5em; }
```

- [ ] **Step 4: Build to verify it compiles**

Run: `cd web && npm run build`
Expected: build succeeds, emits to `../src/smart_proxy/static/app/` with no Svelte/TS errors (no `each_key_duplicate`). The build output is gitignored (compiled on the Forge server at deploy) — do NOT commit it.

- [ ] **Step 5: Commit (source only — the built app is gitignored)**

```bash
git add web/src/views/TrafficView.svelte
git commit -m "feat(ui): expandable per-kind session rows + project/title label"
```

---

## Final Verification

- [ ] **Full suite (sqlite-forced + real Postgres):**
  Run: `.venv/bin/python -m pytest -q` (with a local `smart_proxy_tests` Postgres for the PG-path tests) and `DATABASE_URL= .venv/bin/python -m pytest -q` (sqlite-only).
  Expected: all pass; the two `*_postgres.py` tests skip gracefully when no PG is configured.
- [ ] **Manual smoke (optional):** load a prod DB dump into the local scratch DB, run the dashboard, open `#traffic`, confirm sessions show project/title and expand into per-kind sub-rows.

## Self-Review (completed by plan author)

- **Spec coverage:** kind breakdown (Tasks 2,3,4,6,7,9); label capture (Tasks 1,4,5); migration preserving rows (Task 2); upsert last-write-wins (Task 3); query per-model preserved (Task 6, per C1); JSON kinds[] + lenient kind (Task 7); snapshot + old-import (Task 8); UI expandable (Task 9); deployment note (Global Constraints); test churn (folded into Tasks 3,4,5,6,7,8). All spec sections map to a task.
- **Placeholder scan:** none — every code/test step shows full content.
- **Type consistency:** canonical 17-col order identical across Tasks 2/3/4/6/8; `RequestClass.project/title` (Task 1) consumed in Task 5; `kinds[]` field names identical in Task 7 producer and Task 9 consumer.
