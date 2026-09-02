# Session title + per-session request-kind breakdown — design

Date: 2026-07-19
Status: approved (design), pending spec review → plan

## Problem

The Traffic tab's **Top sessions by cost** table lists each session only by a
truncated `session_id` (e.g. `70f42628…`), so the owner can't tell *what a
session was* or *what kind of traffic it contained*. Two gaps:

1. **No per-session request-kind breakdown.** `usage_kind_daily` has
   `request_kind` but no `session_id`; `usage_session` has `session_id` but no
   `request_kind`. No table links the two, so we can't say "this 156-request
   session was 4 main + 150 subagent + 2 helper" — the exact signal that
   distinguishes an interactive session from an automated batch.
2. **No human-readable session label.** Claude Code does not send a session
   title. Empirically (captured traffic, `anthropic-proxy-test/raw-debug/`):
   - `messages[0]` of a `main` request is almost always a `<system-reminder>`
     wrapper (SessionStart hook), not the human's words; the real input is in
     later turns interleaved with `<command-name>` / `<local-command-stdout>`.
   - The **system prompt of a `main` request reliably contains
     `Primary working directory: <path>`** → the project folder name
     (`smart-proxy`, `Acme-api`) is a clean, stable, low-PII label. Present
     only on standard CLI `main` requests; absent on custom SDK/persona
     harnesses, which may also have `metadata == null` (no
     `session_id`).

## Goals

- Show, per session, the split of requests by `request_kind`
  (main/subagent/helper/unknown), expandable to per-kind requests/tokens/cost.
- Show a human-readable label per session: **project folder** (from the main
  system prompt) plus a **best-effort snippet** of the first human-typed
  message.
- Keep one row per `session_id` in the view; the label is last-write-wins so a
  changed label just updates in place.
- No data loss migrating the existing (new, non-authoritative) `usage_session`.

## Non-goals

- Parsing streaming *responses* (e.g. Claude Code's own title-generation
  helper call). Too fragile/invasive.
- A perfect title. The snippet is explicitly best-effort and heuristic.
- Backfilling labels for sessions that predate this change (they get empty
  project/title → fall back to short `session_id`).
- A per-session-per-day grain. `usage_session` stays a lifetime rollup keyed by
  `session_id` (with `first_date`/`last_date`), not date-bucketed.

## Chosen approach (of 3)

**Widen the `usage_session` key.** Add `request_kind` to its primary key and
two attribute columns (`project`, `title`). The `UsageTracker._session_buf`
already exists — we widen its key and add two fields. Single source of truth.

Rejected: (a) a separate `usage_session_kind` table — two tables/buffers to keep
in sync, no benefit for a brand-new table; (b) adding `session_id` to
`usage_kind_daily` — wrong grain (date-bucketed) and explodes cardinality.

## Data model

### New `usage_session` shape (SCHEMA_SQL + sqlite MIGRATIONS create + PG)

```sql
CREATE TABLE IF NOT EXISTS usage_session (
    session_id   TEXT    NOT NULL,
    proxy_key    TEXT    NOT NULL DEFAULT '',
    request_kind TEXT    NOT NULL DEFAULT 'unknown',   -- NEW, in PK
    provider     TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    first_date   TEXT    NOT NULL,
    last_date    TEXT    NOT NULL,
    project      TEXT    NOT NULL DEFAULT '',           -- NEW (folder basename)
    title        TEXT    NOT NULL DEFAULT '',           -- NEW (prompt snippet)
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, proxy_key, request_kind, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_session_key ON usage_session(proxy_key);
```

`project`/`title` are session-level attributes stored redundantly across the
per-(kind, model) rows of a session; aggregation picks a non-empty one.

### Migration — **preserves existing rows**, both backends

**sqlite** — `MIGRATIONS` runs on *every* startup (no versioning), so the PK
change must NOT be a bare `DROP`/`CREATE` (that would wipe the table each
restart). Follow the existing guarded-rebuild pattern
(`_add_usage_via_openai_compat_column`): a new idempotent method
`_migrate_usage_session_add_kind_title` called from `_run_migrations`:
1. `PRAGMA table_info(usage_session)`. If table absent (fresh DB — SCHEMA_SQL
   already made the new shape) or `request_kind` already present → return.
2. Otherwise rebuild: create `usage_session_new` with the new schema, then
   `INSERT INTO usage_session_new (…) SELECT session_id, proxy_key, 'unknown',
   provider, model, first_date, last_date, '', '', <counters…> FROM
   usage_session`, `DROP TABLE usage_session`, `ALTER TABLE usage_session_new
   RENAME TO usage_session`, recreate the index. Idempotent (no-op once the
   column exists). Existing rows survive as `request_kind='unknown'`,
   `project=''`, `title=''`.

The `usage_session` `CREATE TABLE IF NOT EXISTS` in the sqlite `MIGRATIONS`
list and in `SCHEMA_SQL` are both updated to the new shape (fresh DBs are
correct; `IF NOT EXISTS` is a no-op on existing tables — the guarded rebuild
does the real work).

**Postgres** — versioned, applied-once. New `POSTGRES_MIGRATIONS` entry
`0008_usage_session_kind_title`:
```sql
ALTER TABLE usage_session ADD COLUMN IF NOT EXISTS request_kind TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE usage_session ADD COLUMN IF NOT EXISTS project      TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_session ADD COLUMN IF NOT EXISTS title        TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_session DROP CONSTRAINT IF EXISTS usage_session_pkey;
ALTER TABLE usage_session ADD PRIMARY KEY (session_id, proxy_key, request_kind, provider, model);
```
Existing rows keep `request_kind='unknown'`; adding a constant PK member can't
create a collision, so all rows are preserved.

### Snapshot specs

`SNAPSHOT_TABLE_SPECS`' `usage_session` entry gains `request_kind`, `project`,
`title` in its column tuple and `request_kind` in its conflict-key string.
`SNAPSHOT_DELETE_ORDER` already lists `usage_session` (unchanged). Round-trip
must carry the new columns (regression test).

**Old snapshots.** `_replace_table_rows` builds each INSERT via
`row.get(column)`, so a snapshot exported before this change has no
`request_kind`/`project`/`title` keys → `None` into `NOT NULL` columns →
IntegrityError on the whole restore. Default on import: coalesce missing
`request_kind` → `'unknown'`, `project`/`title` → `''` (in payload construction
or `_encode_snapshot_value`). Covered by a test importing an old-shape payload.
(Snapshot import has no prod caller today, but this keeps it non-footgun.)

## Deployment (coordinated restart required)

The `usage_session` PK swap is the same hazard as a `usage_daily`-locking
migration (see project memory `deploy-usage-daily-migrations`):

- **Postgres** migrations run only via the CLI `db migrate` command, not at
  service startup. Between migrate and restart, **old code** upserts with
  `ON CONFLICT(session_id, proxy_key, provider, model)` — which no longer
  matches the new 5-column PK — and Postgres errors on *every* flush
  (`no unique or exclusion constraint matching the ON CONFLICT specification`).
  New code before migrate fails symmetrically. `ADD PRIMARY KEY` also takes an
  ACCESS EXCLUSIVE lock.
- Prod runs **two** proxy services (Anthropic + OpenAI-compat), both write
  `usage_session`.

Procedure: **stop both services → `db migrate` → deploy new code → restart
both.** (sqlite/dev: the guarded rebuild is one-process-safe; if two processes
share the file, restart both on new code after the first rebuilds. sqlite uses
the default 5 s `busy_timeout` — fine for this tiny table.)

## Capture path

### `request_classify.py`

`RequestClass` gains two fields:
```python
project: str   # folder basename from the main system prompt; "" otherwise
title: str     # best-effort human-prompt snippet (≤140 chars); "" otherwise
```
Both filled **only when `kind == "main"`** (subagent/helper carry `""`, so
they never overwrite a real label on upsert). New pure, defensive helpers
(never raise):
- `_project(system) -> str`: reuse `_system_text_len`'s str-or-list handling to
  get the system text, then match `Primary working directory:\s*(.+)` capturing
  to end of line (NOT `\S+`, which truncates paths containing spaces), strip and
  drop a trailing slash. Label = the **last one or two path components** joined
  by `/` — a bare basename is often ambiguous (real capture:
  `~/Projects/Acme/api` → `Acme/api`, not just `api`).
  No match → `""`.
- `_title_snippet(messages) -> str`: iterate `messages`; consider only
  `role == "user"` messages. `content` may be a plain `str` (real human turn) or
  a `list` of blocks — for a list, look only at blocks with `type == "text"`
  (never `tool_result`, whose payload is under `content`, not `text`, so it must
  never become the title). Take the first text whose stripped value does NOT
  start with a skip prefix, collapse whitespace, truncate to 140 chars; else
  `""`. **Skip prefixes** (must include the compaction/interrupt boilerplate,
  which is plain text, not an XML wrapper, and would otherwise overwrite a real
  title under last-write-wins):
  ```
  "<system-reminder", "<command-", "<local-command", "<persisted-output",
  "<user-", "Caveat:", "This session is being continued", "[Request interrupted"
  ```
  All access is `isinstance`-guarded so malformed input returns `""`, never
  raises.

`classify_request(...)` returns these on the `main` branch, `""` elsewhere.

### `usage.py`

- `record(...)` gains `project: str = ""`, `title: str = ""`.
- `_session_buf` key becomes `(session_id, proxy_key, request_kind, provider,
  model)`; value becomes `[first_date, last_date, project, title, <8
  counters>]`. On each `record`, `project`/`title` overwrite the slot **only
  when the new value is non-empty** (last-write-wins non-empty).
- `flush()` emits session rows as
  `(session_id, proxy_key, request_kind, provider, model, first_date,
  last_date, project, title, <8 counters>)`.

### `build_usage_session_upsert_sql(backend)`

- **Canonical 17-column order (pin exactly; used by INSERT list, the `?`
  count, and the `flush()` tuple — they MUST match):**
  ```
  session_id, proxy_key, request_kind, provider, model,
  first_date, last_date, project, title,
  input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
  cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests
  ```
  sqlite is dynamically typed and will NOT error on a misaligned INSERT — a
  mismatch silently stores `title` into `input_tokens`. The plan must assert
  this order in a test (`test_usage_tracker_kind_session.py`).
- `ON CONFLICT` target += `request_kind`
  → `(session_id, proxy_key, request_kind, provider, model)`.
- DO UPDATE: counters additive (unchanged); `first_date` = MIN/LEAST,
  `last_date` = MAX/GREATEST (unchanged, backend-switched); and non-empty
  last-write-wins for the label:
  ```sql
  project = CASE WHEN excluded.project <> '' THEN excluded.project ELSE <tbl>.project END,
  title   = CASE WHEN excluded.title   <> '' THEN excluded.title   ELSE <tbl>.title   END
  ```
  (`<tbl>` = `usage_session.` on Postgres, empty on sqlite — same `q` prefix
  trick already used in this function.)

### `anthropic_proxy.py`

`record_kwargs_for(data, headers)` returns
`{request_kind, session_id, project, title}` (defensive; `""` on any parse
failure). Spread into the single `tracker.record(...)` call as today.

## Query + JSON shape

### `query_top_sessions(limit)`

**Keep per-model rows** — do NOT collapse models in SQL. `build_sessions_json`
prices each row via `_row_cost_split` → `calculate_cost(model=row["model"], …)`,
so the `model` column must survive; collapsing it makes every multi-model
session's cost "unknown"/wrong and breaks the cost-ranking tests
(`test_usage_kind_session_queries.py`, `test_dashboard_api.py`).

- Ranking subquery unchanged: whole sessions by total token volume, `LIMIT ?`
  (joins on `session_id, proxy_key` only — unaffected by the kind change).
- Outer query returns **one row per `(session_id, proxy_key, request_kind,
  provider, model)`** — i.e. add `s.request_kind` to both the SELECT and the
  `GROUP BY`, nothing else collapses. Because Postgres requires every selected
  non-aggregate to be grouped or aggregated, `project`/`title` (free text) are
  wrapped as `MAX(s.project) AS project`, `MAX(s.title) AS title` rather than
  added to `GROUP BY`; `s.request_kind` joins the existing grouped columns
  (`s.session_id, s.proxy_key, pk.name, s.provider, s.model, top.total_tokens`).
- Ordering by `top.total_tokens DESC` preserved.

### `build_sessions_json(rows, prices)`

Aggregate into one entry per `(session_id, proxy_key)`:
- Top-level: `key_name`, `first_date` (min), `last_date` (max), summed
  counters, `base_cost`/`cache_cost`/`cost`, `partial`, `unknown` — as today.
- `project`/`title`: first non-empty seen across the session's rows (main row
  supplies them).
- New `kinds: [{request_kind, requests, input_tokens, output_tokens,
  cache_read_tokens, cache_creation_tokens, web_search_requests, base_cost,
  cache_cost, cost, partial, unknown}]`, one per `request_kind`, **aggregated
  across the kind's per-model rows in Python** (call `_row_cost_split` per model
  row, sum the split costs into the kind bucket — same pattern as the top-level
  totals), sorted by cost desc.
- Read the kind leniently: `str(row.get("request_kind") or "unknown")` so
  tests/mocks that omit it don't crash.
- Final sort by `_session_sort_metric` (unchanged).

### `/api/sessions`

Same handler/auth. Response `sessions[]` items gain `project`, `title`, and
`kinds[]`. Existing fields unchanged (back-compatible superset).

## UI (`web/src/views/TrafficView.svelte`)

Top-sessions table columns: **Key | Title | Session | First → Last | Requests |
Cost**, each row expandable.
- `Title` cell: project folder in normal weight; snippet as a muted second
  line (or `title=` tooltip when space is tight). Both empty → show short
  `session_id…` in the Session column as today and `—` for Title.
- Expand: a local `$state` `Set<string>` of expanded `session_id:proxy_key`
  keys; a `▸/▾` affordance on the row. When expanded, render sub-rows from
  `s.kinds` (kind label indented, requests, tokens, cost via existing
  `fmtCost`).
- `Session` [`{#each}`] key stays `` `${s.session_id}:${s.proxy_key}` ``;
  sub-rows keyed by `` `${s.session_id}:${s.proxy_key}:${k.request_kind}` ``.
- `TrafficView` `Session` type extends with `project`, `title`, `kinds`.

## PII

The `title` snippet is **real user-typed text** (≤140 chars). It is visible to
anyone with dashboard auth and travels into snapshots/backups. This is a
deliberate shift — the proxy previously stored zero message content. The owner
opted into it explicitly ("folder + snippet"). Mitigations baked in: hard 140-char
truncation; captured only from `main` requests; wrapper/context blocks skipped
so we don't store harness boilerplate. (An env kill-switch to disable snippet
capture is possible but out of scope unless requested.)

## Testing

- `test_request_classify.py`: `_project` extracts the folder and basenames it;
  absent block → `""`. `_title_snippet` skips each wrapper prefix, returns the
  first genuine human block, truncates at 140, and returns `""` for
  all-wrapper / non-list / garbage input without raising. `classify_request`
  fills project/title only for `main`.
- `test_usage_kind_session_db.py` / a new focused test: `usage_session` upsert
  with `request_kind` in the key; non-empty last-write-wins for
  `project`/`title` (empty second write does not clobber); date merge intact.
- `test_usage_session_upsert_postgres.py`: extend for the new columns + PK on
  the live-Postgres path (skips gracefully without PG).
- `test_usage_kind_session_queries.py`: `query_top_sessions` returns per-kind
  rows; `build_sessions_json` produces `kinds[]` summing correctly, carries
  project/title, and ranking by cost is preserved.
- `test_db_snapshot_roundtrip.py`: round-trip the new columns; non-empty-restore
  regression (delete-order clears before restore).
- `test_dashboard_api.py::ApiKindsSessionsTests`: `/api/sessions` item shape
  includes `project`, `title`, `kinds[]`.
- Migration: a test that an existing old-shape `usage_session` (sqlite) rebuilds
  to the new shape once, preserving rows as `request_kind='unknown'`, and is a
  no-op on second run.

**Existing tests that BREAK and must be updated (not just "extended"):**
- `test_proxy_classify_wiring.py` — exact-dict assert on `record_kwargs_for`'s
  return now includes `project`/`title`.
- 14→17 element seed tuples for `upsert_usage_session_batch`:
  `test_usage_kind_session_db.py`, `test_usage_kind_session_queries.py`,
  `test_usage_session_upsert_postgres.py`, `test_db_snapshot_roundtrip.py`.
- `test_usage_tracker_kind_session.py` — `_session_buf` key/value shape changed.
- `test_dashboard_api.py` sessions mocks omit `request_kind` — OK only because
  `build_sessions_json` reads it leniently (required above); assert new shape.
- Drive-by: stale comment in `test_usage_session_upsert_postgres.py` (~line 36)
  claims `usage_session` isn't in `SNAPSHOT_TABLE_SPECS` — it is; fix it.

## Decisions made (flag at review if wrong)

1. **Rows preserved, not dropped.** Migration keeps existing `usage_session`
   rows, tagging them `request_kind='unknown'`, `project=''`, `title=''`.
   (Supersedes the earlier "drop and lose a few rows" idea — the guarded-rebuild
   pattern makes preservation free.)
2. **Label = last-write-wins non-empty**, not first-frozen. A changed
   project/snippet updates the session's label in place.
3. **Label captured from `main` only.** Pure-subagent/helper/SDK sessions (no
   main request, or `metadata==null`) get no label → fall back to short
   `session_id`.
