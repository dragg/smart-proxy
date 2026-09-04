# Hour-granularity usage for the dashboard (`usage_bucket`)

**Date:** 2026-09-04
**Components:** `src/smart_proxy/db.py` (table, upsert builder, five accessors, snapshot registry,
sqlite `transaction()` rollback), `src/smart_proxy/db_migrations.py` (`0014_usage_bucket`),
`src/smart_proxy/usage.py` (`UsageTracker` fifth buffer, `flush()` rewrite, `UsageFlushError`),
`src/smart_proxy/anthropic_proxy.py` (`_flush_usage`, Postgres boot guard),
`src/smart_proxy/usage_dashboard.py` (`UsageRange`, `_usage_range`, series builder),
`src/smart_proxy/dashboard_api.py` (`/api/usage`, `/api/usage/kinds`),
`web/src/views/UsageView.svelte`, `README.md`, tests.
**Status:** design — corrected by adversarial review, all claims verified against code at `7fe2386`.
**Line numbers are as of `7fe2386`**; re-grep the anchors once the first slice lands.

The file name keeps the working title `usage-bucket`; the table is also called `usage_bucket` —
see ruling C for why, and why its time column is `hour_utc`, not `bucket_utc`.

---

## 1. Problem / intent

`GET /api/usage` and the Usage tab filter by **date** only. The user wants, for an arbitrary
time window, *who* spent how many tokens and how much money. The finest persisted grain today is
the hour, in `usage_key_hourly` (`db.py:336-350`), which has only `(hour_utc, proxy_key, model)`
— no credential, group, provider, compat flag or request kind — is pruned after 35 days
(`_HOURLY_RETENTION_DAYS`, `anthropic_proxy.py:4483`), and is the spend limiter's re-seed source
(`key_limits.py:133`). `usage_daily` / `usage_kind_daily` are per date; `usage_session` keeps
only `first_date`/`last_date`. Nothing timestamped per request exists.

## 2. Decisions taken by the user (not relitigated here)

1. No per-request event log.
2. No minute buckets; **hour** grain.
3. A new table carrying the **full `usage_daily` dimension set** at hour grain.
4. **No retention / pruning** on the new table for now ("надо будет потом отдельно это продумаем").
5. `usage_key_hourly` and its prune stay **completely untouched** — it feeds limit enforcement.

## 3. What the review changed in the proposed design, and why

| # | Proposed | Corrected | Why (evidence) |
|---|---|---|---|
| 1 | Source chosen automatically: hours given *or range inside coverage* → new table, else `usage_daily` | **The parameter grammar alone decides.** `YYYY-MM-DD` → `usage_daily`; `YYYY-MM-DDTHH` → `usage_bucket`. Never coverage-based. | Coverage-based switching makes the *same* date query flip source as coverage grows or after a partial flush failure, and lets `/api/usage` disagree with `/_usage` (which reads `usage_daily`, `usage_dashboard.py:552`). It also changes the day path's DB calls, which `tests/test_dashboard_api.py:155-169` mocks explicitly (`db = MagicMock()` — any new `await db.x()` there raises `TypeError`). |
| 2 | New query "mirrors `query_usage_by_key_model` exactly" while the PK gains `request_kind` | The `/api/usage` bucket query **sums across `request_kind`** (it is not in the `GROUP BY`). Kind breakdown lives on `/api/usage/kinds`, which gains the hour grammar. | `_build_usage_cost_groups` appends one `models[]` entry **per input row** (`usage_dashboard.py:172-188`) and groups only by `(proxy_key, group_name, key_name, via_openai_compat)` (`:118-124`). Rows split by kind would show each model 2–4 times under a key. |
| 3 | Hour range half-open `[start, end)`; daily stays inclusive; "convert in one place" | **Both grammars are inclusive of the last bucket.** | `start`/`end` are bucket *labels* in this API. Inclusive is what `usage_daily` already does (`db.py:1583`) and what the UI shows ("10:00 through 14:00"). Half-open forces the UI to send a label in the future for "last 6h". `query_usage_key_hourly` is half-open (`db.py:1498`) because it takes window *instants* from `KeyLimiter` — not a precedent for a label API. |
| 4 | `flush()` weakness noted; decision B open | **Fix now, but as *independence + one atomic `usage_daily`+`usage_bucket` write*, not re-merge.** New `UsageFlushError` carries the rows that did land so window attribution still runs. | Re-merge was explicitly dropped on 2026-08-21 (`docs/superpowers/specs/2026-08-21-db-degraded-mode-design.md` §3 "Usage re-merge on failed flush. Dropped."). Independence is what actually protects `usage_key_hourly`; the atomic pair is the only way "bucket sums == daily sums" holds under partial failure. See §5.2. |
| 5 | (not in design) | **Postgres boot guard**: refuse to start with pending migrations. | Postgres migrations run only from `smart-proxy db migrate` (`__main__.py:529-540`); `PostgresDatabase.connect()` (`db_postgres.py:261-268`) checks nothing. New code on an unmigrated DB = every 60s flush fails, alert storm, and the exact 2026-08-21 shape (`anthropic_proxy.py:4558-4562`) if the bucket write sits before `usage_key_hourly`. |
| 6 | (not in design) | sqlite `Database.transaction()` **rolls back on exception**. | Its docstring promises "commit or roll back together" (`db.py:1201-1210`) but the body is `yield; commit()` — nothing in `db.py` ever calls `rollback` (grep: zero hits). An exception mid-block leaves sqlite's implicit transaction open and the *next* unrelated `commit()` commits the half-written work. The atomic pair in #4 is not atomic on sqlite without this. |
| 7 | `CREATE INDEX idx_usage_bucket_bucket ON usage_bucket(bucket_utc)` | **No secondary index.** | The PK's leading column is the hour; sqlite's `sqlite_autoindex_usage_bucket_1` and Postgres's PK btree both serve a prefix range scan. A second index on the hottest write path buys nothing. (`idx_usage_daily_date` / `idx_usage_key_hourly_hour` have the same redundancy; left alone.) |
| 8 | Column `bucket_utc` | Column **`hour_utc`**, same `'%Y-%m-%dT%H'` label as `usage_key_hourly.hour_utc` and `key_limits.HOUR_FMT`. | The format bakes the grain in; a neutral name hides it. See ruling C. |
| 9 | (not in design) | Hour-grammar span capped at **92 days** (2208 buckets) → `400`. | The series is zero-filled per hour; without a cap a three-year hour query returns ~26k points. Day grammar stays unbounded as today. |
| 10 | `granularity` and `covered_from` on every response | `granularity` on every response; **`covered_from` and `series` only on hour responses.** | Keeps the day path's DB calls byte-identical (see #1). |
| 11 | (not in design) | **Fix `usage_daily`'s snapshot spec**: it omits `via_openai_compat`, a PK member. | `SNAPSHOT_TABLE_SPECS` (`db.py:611-629`) lists no `via_openai_compat`. Export drops it; restore writes `DEFAULT 0`, so a compat row silently becomes native, and a day with both a native and a compat row for the same `(date, key, cred, provider, model)` fails restore with a PK violation. `test_export_snapshot_round_trips_all_tables` inserts `via_openai_compat=1` but compares export-to-export, so it cannot see it. Same class of bug the design's item 5 worries about; one-line fix, done here. |
| 12 | (not in design) | Hour labels are **canonicalised** after parsing. | `datetime.strptime("2026-09-05T9", "%Y-%m-%dT%H")` succeeds; the string range `hour_utc >= '2026-09-05T9'` is then wrong. `_usage_date_range` already re-serialises dates via `.isoformat()` (`usage_dashboard.py:50`); do the same with `strftime`. |
| 13 | (not in design) | `usage_bucket` added to `_COUNTER_TABLES` in `tests/test_usage_counter_bigint_postgres.py:59-64`. | That test is the guard that every usage counter is BIGINT; a table it does not list is not guarded. |

Pre-existing, **not fixed here**, recorded so nobody rediscovers them:

- `x-smart-proxy-openai-compat` is read from the *client's* request headers for attribution
  (`anthropic_proxy.py:3277`) and only dropped on the way *upstream* (`:115-121`). Any `sp-` key
  holder can label their own native traffic "· OpenAI". Cosmetic; the new table inherits it.
- `/api/usage/kinds` passes unvalidated `start`/`end` straight to SQL as bind parameters
  (`dashboard_api.py:195-197`). Safe from injection; garbage just returns nothing. Fixed as a
  side effect of §7.3 (it now goes through `_usage_range` and 400s).
- A group (Anthropic key) rename mid-range splits that key's rows into two dashboard groups. True
  across days today (`GROUP BY u.group_name`, `db.py:1584`); true across hours with this table.

## 4. Non-goals

Retention for `usage_bucket` (user decision; §11 has the growth math and the hook). Backfilling
hours before deploy (no source exists). Minute grain. Touching `usage_key_hourly`, its prune, or
`KeyLimiter`. Hour grammar on `/_usage` (the legacy HTML page stays day-only). A kinds-by-hour UI
(the API gains it; the Traffic tab keeps its fixed 7-day window). Removing `usage_kind_daily` even
though it becomes a projection.

---

## 5. Design

### 5.1 Data model — `usage_bucket`

One row per `(hour_utc, proxy_key, credential_id, provider, model, via_openai_compat,
request_kind)`. `usage_daily` is `GROUP BY substr(hour_utc, 1, 10)` of it with `request_kind`
summed away; `usage_kind_daily` is the same projection keeping `request_kind` and dropping
`credential_id`/`via_openai_compat`. `session_id` is deliberately absent (unbounded cardinality —
that is the per-request log the user rejected; `usage_session` owns that axis).

**sqlite — append to `SCHEMA_SQL` (`db.py:82`), after the `usage_key_hourly` block at `:350`.**
`SCHEMA_SQL` is re-executed by `executescript` on every `connect()` (`db.py:1042`) before
`_run_migrations`, so `CREATE TABLE IF NOT EXISTS` covers fresh and existing sqlite files alike;
no entry in `MIGRATIONS` is needed.

```sql
-- Full usage_daily dimension set (+ request_kind) at hour grain, for the
-- dashboard's arbitrary-window view. usage_daily and usage_kind_daily are
-- projections of this table (GROUP BY substr(hour_utc, 1, 10)); the writer
-- keeps them in step by writing usage_daily and usage_bucket in ONE transaction.
-- Deliberately separate from usage_key_hourly, which feeds the spend limiter
-- and is pruned; this table is never pruned (decision 2026-09-04).
CREATE TABLE IF NOT EXISTS usage_bucket (
    hour_utc                 TEXT    NOT NULL,   -- '%Y-%m-%dT%H', UTC (key_limits.HOUR_FMT)
    proxy_key                TEXT    NOT NULL DEFAULT '',
    group_name               TEXT,
    credential_id            TEXT    NOT NULL,
    provider                 TEXT    NOT NULL,
    model                    TEXT    NOT NULL,
    via_openai_compat        INTEGER NOT NULL DEFAULT 0,
    request_kind             TEXT    NOT NULL DEFAULT 'unknown',
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_utc, proxy_key, credential_id, provider, model,
                 via_openai_compat, request_kind)
);
```

No `CREATE INDEX` (review #7). sqlite `INTEGER` is 64-bit.

**Postgres — new entry appended to `POSTGRES_MIGRATIONS` (`db_migrations.py:5`), after
`0013_oauth_limit_wipe_forensics`:**

```python
    (
        # Hour-grain usage with the full usage_daily dimension set (+ request_kind),
        # for the dashboard's arbitrary-window view. BIGINT counters from day one:
        # 0012 exists because int4 overflowed on cache_read_tokens in production.
        # CREATE TABLE only -- takes no lock on existing tables, so the proxy
        # does not need to be stopped for this one.
        "0014_usage_bucket",
        (
            """
            CREATE TABLE IF NOT EXISTS usage_bucket (
                hour_utc                 TEXT    NOT NULL,
                proxy_key                TEXT    NOT NULL DEFAULT '',
                group_name               TEXT,
                credential_id            TEXT    NOT NULL,
                provider                 TEXT    NOT NULL,
                model                    TEXT    NOT NULL,
                via_openai_compat        INTEGER NOT NULL DEFAULT 0,
                request_kind             TEXT    NOT NULL DEFAULT 'unknown',
                input_tokens             BIGINT  NOT NULL DEFAULT 0,
                output_tokens            BIGINT  NOT NULL DEFAULT 0,
                cache_read_tokens        BIGINT  NOT NULL DEFAULT 0,
                cache_creation_tokens    BIGINT  NOT NULL DEFAULT 0,
                cache_creation_5m_tokens BIGINT  NOT NULL DEFAULT 0,
                cache_creation_1h_tokens BIGINT  NOT NULL DEFAULT 0,
                web_search_requests      BIGINT  NOT NULL DEFAULT 0,
                requests                 BIGINT  NOT NULL DEFAULT 0,
                PRIMARY KEY (hour_utc, proxy_key, credential_id, provider, model,
                             via_openai_compat, request_kind)
            )
            """,
        ),
    ),
```

**Registry (`db.py`):**

- `SNAPSHOT_TABLE_SPECS` (`:605`): append
  ```python
  (
      "usage_bucket",
      (
          "hour_utc", "proxy_key", "group_name", "credential_id", "provider", "model",
          "via_openai_compat", "request_kind",
          "input_tokens", "output_tokens", "cache_read_tokens",
          "cache_creation_tokens", "cache_creation_5m_tokens",
          "cache_creation_1h_tokens", "web_search_requests", "requests",
      ),
      "hour_utc, proxy_key, credential_id, provider, model, via_openai_compat, request_kind",
  ),
  ```
  and, per review #11, insert `"via_openai_compat"` after `"model"` in the existing `usage_daily`
  spec (`:612-628`) and append `, via_openai_compat` to its `order_by`.
- `SNAPSHOT_DELETE_ORDER` (`:866`): add `"usage_bucket"` right after `"usage_key_hourly"`.
- `_SNAPSHOT_COLUMN_DEFAULTS` (`:856`): add `("usage_bucket", "request_kind"): "unknown"` and
  `("usage_bucket", "via_openai_compat"): 0`, and `("usage_daily", "via_openai_compat"): 0` so a
  snapshot taken by today's code (which lacks the column) restores instead of raising on the
  NOT NULL column.

**Upsert builder (`db.py`, next to `build_usage_hourly_upsert_sql` at `:1003`):**

```python
def build_usage_bucket_upsert_sql(backend: str) -> str:
    q = "usage_bucket." if backend == "postgres" else ""
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_bucket\n"
        "    (hour_utc, proxy_key, group_name, credential_id, provider, model,\n"
        "     via_openai_compat, request_kind,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(hour_utc, proxy_key, credential_id, provider, model,\n"
        "            via_openai_compat, request_kind) DO UPDATE SET\n"
        f"                   group_name = COALESCE(excluded.group_name, {q}group_name),\n"
        f"                   {sets}"
    )
```

Row tuple (16 columns, canonical order, used everywhere):
`(hour_utc, proxy_key, group_name|None, credential_id, provider, model, via_openai_compat:int,
request_kind, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)`.

`group_name` uses the same `COALESCE(excluded, existing)` as `usage_daily` (`db.py:933`): a
rename lands on later upserts, `NULL` never erases a name. `executemany` issues one statement per
row, so two buffer rows that differ only in `group_name` (rename inside one flush interval) do not
trigger Postgres's "cannot affect row a second time".

**Accessors (`db.py`, class `Database`, in the "Usage tracking" section after
`upsert_usage_hourly_batch` at `:1465`):**

```python
async def upsert_usage_bucket_batch(self, rows: list[tuple]) -> None:
    """Batch-upsert hour-grain usage rows (16-col canonical order). Standalone
    entry point for tests/tooling; the tracker uses the atomic pair below."""
    async with self.transaction():
        if not rows:
            return
        await self.db.executemany(build_usage_bucket_upsert_sql(self._backend), rows)
        await self.db.commit()

async def upsert_usage_daily_and_bucket_batch(
    self, daily_rows: list[tuple], bucket_rows: list[tuple]
) -> None:
    """Write usage_daily and usage_bucket in ONE transaction.

    The two tables must agree (usage_daily is a projection of usage_bucket),
    so they land together or not at all. One commit, after both executemany
    calls -- a commit between them would break atomicity on sqlite.
    """
    async with self.transaction():
        if daily_rows:
            await self.db.executemany(build_usage_upsert_sql(self._backend), daily_rows)
        if bucket_rows:
            await self.db.executemany(build_usage_bucket_upsert_sql(self._backend), bucket_rows)
        if daily_rows or bucket_rows:
            await self.db.commit()

async def query_usage_bucket_by_key_model(
    self, start_hour: str, end_hour: str
) -> list[dict]:
    """Same shape as query_usage_by_key_model, over hour_utc in [start_hour, end_hour]
    (both inclusive, '%Y-%m-%dT%H'). request_kind is summed away on purpose:
    _build_usage_cost_groups appends one models[] entry per row."""
    cur = await self.db.execute(
        """SELECT u.proxy_key, u.group_name,
                  COALESCE(pk.name, '') AS key_name,
                  u.provider, u.model, u.via_openai_compat,
                  SUM(u.input_tokens)  AS input_tokens,
                  SUM(u.output_tokens) AS output_tokens,
                  SUM(u.cache_read_tokens) AS cache_read_tokens,
                  SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                  SUM(u.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                  SUM(u.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                  SUM(u.web_search_requests) AS web_search_requests,
                  SUM(u.requests)      AS requests
           FROM usage_bucket u
           LEFT JOIN proxy_api_keys pk ON pk.key = u.proxy_key
           WHERE u.hour_utc >= ? AND u.hour_utc <= ?
           GROUP BY u.proxy_key, u.group_name, pk.name, u.provider, u.model,
                    u.via_openai_compat
           ORDER BY COALESCE(NULLIF(pk.name, ''), NULLIF(u.group_name, ''), u.proxy_key),
                    u.provider, u.model""",
        (start_hour, end_hour),
    )
    return [dict(row) for row in await cur.fetchall()]

async def query_usage_bucket_series(
    self, start_hour: str, end_hour: str
) -> list[dict]:
    """Per-(hour_utc, provider, model) sums over the inclusive range, so each
    row can be priced by model before summing per hour. Hours with no
    traffic are absent; the dashboard zero-fills."""
    cur = await self.db.execute(
        """SELECT hour_utc, provider, model,
                  SUM(input_tokens)  AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                  SUM(cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                  SUM(web_search_requests) AS web_search_requests,
                  SUM(requests)      AS requests
           FROM usage_bucket
           WHERE hour_utc >= ? AND hour_utc <= ?
           GROUP BY hour_utc, provider, model
           ORDER BY hour_utc, provider, model""",
        (start_hour, end_hour),
    )
    return [dict(row) for row in await cur.fetchall()]

async def query_usage_bucket_by_kind(
    self, start_hour: str, end_hour: str
) -> list[dict]:
    """Same shape as query_usage_by_kind, over the inclusive hour range."""
    cur = await self.db.execute(
        """SELECT u.proxy_key, COALESCE(pk.name, '') AS key_name, u.request_kind,
                  u.provider, u.model,
                  SUM(u.input_tokens) AS input_tokens,
                  SUM(u.output_tokens) AS output_tokens,
                  SUM(u.cache_read_tokens) AS cache_read_tokens,
                  SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                  SUM(u.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                  SUM(u.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                  SUM(u.web_search_requests) AS web_search_requests,
                  SUM(u.requests) AS requests
           FROM usage_bucket u
           LEFT JOIN proxy_api_keys pk ON pk.key = u.proxy_key
           WHERE u.hour_utc >= ? AND u.hour_utc <= ?
           GROUP BY u.proxy_key, pk.name, u.request_kind, u.provider, u.model""",
        (start_hour, end_hour),
    )
    return [dict(row) for row in await cur.fetchall()]

async def min_usage_bucket_hour(self) -> str | None:
    """Earliest hour with a bucket row, or None. Index seek on the PK prefix."""
    cur = await self.db.execute("SELECT MIN(hour_utc) AS h FROM usage_bucket")
    row = await cur.fetchone()
    return (row["h"] if row else None) or None
```

**sqlite `Database.transaction()` (`db.py:1201`) — review #6:**

```python
@asynccontextmanager
async def transaction(self):
    """Group statements so they commit or roll back together.

    sqlite opens an implicit transaction on the first DML; without an explicit
    rollback an exception here leaves it open and the next unrelated commit()
    would commit the half-written work. The Postgres backend overrides this
    with a real transaction on the shared connection.
    """
    try:
        yield self
    except BaseException:
        await self.db.rollback()
        raise
    await self.db.commit()
```

`PostgresDatabase.transaction()` (`db_postgres.py:280`) is unchanged; psycopg's
`conn.transaction()` already rolls back on exception.

### 5.2 Writer — `UsageTracker` (`usage.py`)

**Buffer.** Add to `__init__`, after `_hour_buf` (`usage.py:602`):

```python
# bucket key: (hour_utc, proxy_key, group, credential_id, provider, model,
#              via_openai_compat, request_kind) -> 8 counters
self._bucket_buf: dict[_BucketKey, list[int]] = {}
```
with, next to `_BufKey` (`:585`):
```python
_BucketKey = tuple[str, str, str, str, str, str, int, str]
```

**`record()` (`:614`).** After the `hkey` accumulate (`:656-657`) add:

```python
bkey = (hour, proxy_key, group, credential_id, provider, model,
        int(via_openai_compat), request_kind or "unknown")
self._accumulate(self._bucket_buf, bkey, counters)
```

`hour` and `date` are both derived from the single `now` at `:634-636`, so
`hour_utc[:10] == date` holds for every record by construction. `group` is the same
`(group_name or "").strip()` as the daily key; `request_kind or "unknown"` is the same
normalisation as the kind key (`:654`). `record()` is synchronous and never awaits, so it cannot
interleave with the swap in `flush()`.

**`UsageFlushError`** (new, `usage.py`, module level):

```python
class UsageFlushError(Exception):
    """One or more usage tables failed to flush; every table was still attempted.

    ``failures`` maps table name -> exception. ``rows`` holds the usage_daily
    rows that DID land (empty when the usage_daily+usage_bucket pair failed),
    so the caller can still attribute them to OAuth windows.
    """

    def __init__(self, failures: dict[str, Exception], rows: list[tuple]) -> None:
        self.failures = failures
        self.rows = rows
        detail = "; ".join(f"{name}: {exc!r}" for name, exc in failures.items())
        super().__init__(f"usage flush failed for {', '.join(failures)} -- {detail}")
```

**`flush()` (`:673-717`) — replace the body after the lock block:**

```python
async def flush(self, db: object) -> list[tuple]:
    """Move buffered data to the database.

    usage_daily and usage_bucket are written in ONE transaction (they must
    agree); usage_key_hourly, usage_kind_daily and usage_session are each
    written independently, so one table's failure no longer discards the
    others' data (the 2026-08-21 failure). A failed table's buffer is still
    lost -- re-merging was rejected on 2026-08-21 (degraded-mode spec §3).
    Raises UsageFlushError after attempting every write if any failed.

    Returns the flushed usage_daily row tuples (15-col upsert order).
    """
    from smart_proxy.db import Database
    assert isinstance(db, Database)

    async with self._lock:
        if not self._buf:
            return []
        snapshot, self._buf = self._buf, {}
        kind_snapshot, self._kind_buf = self._kind_buf, {}
        session_snapshot, self._session_buf = self._session_buf, {}
        hour_snapshot, self._hour_buf = self._hour_buf, {}
        bucket_snapshot, self._bucket_buf = self._bucket_buf, {}

    rows = [(k[0], k[1], (k[2] or None), k[3], k[4], k[5], k[6], *v)
            for k, v in snapshot.items()]
    bucket_rows = [(k[0], k[1], (k[2] or None), k[3], k[4], k[5], k[6], k[7], *v)
                   for k, v in bucket_snapshot.items()]
    hour_rows = [(k[0], k[1], k[2], *v) for k, v in hour_snapshot.items()]
    kind_rows = [(k[0], k[1], k[2], k[3], k[4], *v) for k, v in kind_snapshot.items()]
    session_rows = [(k[0], k[1], k[2], k[3], k[4], v[0], v[1], v[2], v[3], *v[4:])
                    for k, v in session_snapshot.items()]

    failures: dict[str, Exception] = {}
    landed: list[tuple] = []
    try:
        await db.upsert_usage_daily_and_bucket_batch(rows, bucket_rows)
        landed = rows
    except Exception as exc:
        failures["usage_daily+usage_bucket"] = exc
    # Spend-limiter source first among the independents.
    for name, write, batch in (
        ("usage_key_hourly", db.upsert_usage_hourly_batch, hour_rows),
        ("usage_kind_daily", db.upsert_usage_kind_batch, kind_rows),
        ("usage_session", db.upsert_usage_session_batch, session_rows),
    ):
        try:
            await write(batch)
        except Exception as exc:
            failures[name] = exc
    if failures:
        raise UsageFlushError(failures, landed)
    logger.debug("Flushed %d usage rows to DB", len(rows))
    return rows
```

The return contract is unchanged (`tests/test_usage_tracker_hourly.py:55-61` asserts arity 15;
`_window_usage_deltas` at `anthropic_proxy.py:4492-4505` indexes `row[3]`, `row[4]`, `row[5]`,
`row[7+i]`).

**`_flush_usage` (`anthropic_proxy.py:4508-4521`):**

```python
async def _attribute_flushed_usage(db: Database, rows: list[tuple], app: object | None) -> None:
    deltas = _window_usage_deltas(rows)
    if not deltas:
        return
    try:
        await db.attribute_oauth_window_usage(deltas)
    except Exception as exc:
        logger.exception("OAuth window usage attribution failed")
        _alert_failure(app, source="usage attribution to the OAuth window", exc=exc)


async def _flush_usage(tracker: UsageTracker, db: Database, app: object | None = None) -> int:
    """Flush buffered usage; attribute whatever landed to OAuth rate-limit windows.

    A partial flush still raises (so the loop alerts and _resync_limiter /
    _on_shutdown behave as before), but the usage_daily rows that did land are
    attributed first -- they are committed, and would otherwise never be.
    """
    try:
        rows = await tracker.flush(db)
    except UsageFlushError as exc:
        if exc.rows:
            await _attribute_flushed_usage(db, exc.rows, app)
        raise
    if rows:
        await _attribute_flushed_usage(db, rows, app)
    return len(rows)
```

`_usage_flush_loop` (`:4548`), `_resync_limiter` (`:4524`) and `_on_shutdown` (`:5074-5077`)
are untouched; the loop's `except Exception` (`:4558-4562`) already logs and alerts with the
exception text, which now names the failed table(s). Import `UsageFlushError` alongside
`UsageTracker`.

### 5.3 Postgres boot guard (`anthropic_proxy.py`, `_on_startup` at `:4960`)

Immediately after `await db.connect()` (`:4965`):

```python
async def _require_migrations_applied(db: Database) -> None:
    """Postgres only: refuse to boot with pending migrations.

    Migrations run only from `smart-proxy db migrate`; nothing else checks. New
    code against an unmigrated schema fails every 60s flush and, before this
    change, took usage_key_hourly (the spend limiter's re-seed source) down
    with it. A refused boot is loud and cheap; a silent hole is neither.
    """
    if getattr(db, "_backend", "sqlite") != "postgres":
        return
    from smart_proxy.db_migrations import POSTGRES_MIGRATIONS
    await db.ensure_migration_table()
    applied = await db.get_applied_migrations()
    pending = [name for name, _statements in POSTGRES_MIGRATIONS if name not in applied]
    if pending:
        raise RuntimeError(
            "pending PostgreSQL migrations: " + ", ".join(pending)
            + " -- run `smart-proxy db migrate` before starting the proxy"
        )
```

`ensure_migration_table` / `get_applied_migrations` exist on `PostgresDatabase`
(`db_postgres.py:283-298`). A fresh Postgres already cannot boot without migrations
(`pool.reload()` at `:4969` reads `anthropic_keys`); this makes the *partial* case equally loud.

### 5.4 Read path — endpoint contract

**Parameter grammar** (`/api/usage` and `/api/usage/kinds`; `/_usage` keeps `_usage_date_range`):

| Form | Grammar | Source | Semantics |
|---|---|---|---|
| `start=YYYY-MM-DD&end=YYYY-MM-DD` | day | `usage_daily` (and `usage_kind_daily` for kinds) | inclusive of both dates; UTC; unchanged from today |
| `start=YYYY-MM-DDTHH&end=YYYY-MM-DDTHH` | hour | `usage_bucket` | inclusive of both hour buckets; UTC |

Rules, in order:

1. `hourly = "T" in start_raw or "T" in end_raw`. If not hourly → existing `_usage_date_range`
   (defaults: last 7 days ending today; its two `400`s unchanged).
2. If hourly, each *given* value must parse with `datetime.strptime(v, "%Y-%m-%dT%H")`; otherwise
   `400 text/plain "start and end must both use YYYY-MM-DD or both use YYYY-MM-DDTHH (UTC)"`.
   This is what rejects a mixed request such as `start=2026-09-05&end=2026-09-05T14`.
3. Defaults in hour grammar: missing `end` → the current UTC hour; missing `start` → `end − 23h`
   (a 24-bucket window, the hour analogue of the 7-day day default).
4. `start > end` → `400 "start must be before or equal to end"`.
5. `end − start >= 92 days` (i.e. more than 2208 inclusive buckets) →
   `400 "hour ranges are limited to 92 days; use YYYY-MM-DD for longer spans"`.
6. Labels are re-serialised with `strftime("%Y-%m-%dT%H")` before use (`T9` → `T09`).
7. Comparisons use **naive** datetimes throughout (`strptime` yields naive; build the default
   `now` as `datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)`)
   — mixing aware and naive raises `TypeError`.

`usage_dashboard.py` additions:

```python
HOUR_FMT = "%Y-%m-%dT%H"          # same label as key_limits.HOUR_FMT and usage_bucket.hour_utc
_MAX_HOUR_SPAN = timedelta(days=92)   # reject when end - start >= this

@dataclass(frozen=True)
class UsageRange:
    granularity: str   # "day" | "hour"
    start: str         # canonical label, inclusive
    end: str           # canonical label, inclusive

def _usage_range(request: web.Request) -> UsageRange | web.Response: ...
def _iter_hours(start_hour: str, end_hour: str) -> list[str]: ...   # inclusive, canonical labels
def build_usage_bucket_series(rows: list[dict], prices: dict,
                              start_hour: str, end_hour: str) -> list[dict]: ...
def build_usage_cost_json(start, end, rows, prices, *,
                          granularity: str = "day",
                          covered_from: str | None = None,
                          series: list[dict] | None = None) -> dict: ...
```

`build_usage_cost_json` always emits `"granularity"`; it emits `"covered_from"` and `"series"`
only when `granularity == "hour"` (review #10). Existing positional callers are unaffected.

`build_usage_bucket_series` prices each `(hour, provider, model)` row with `_row_cost_split`
(`usage_dashboard.py:95`), sums per hour, then zero-fills every hour of `_iter_hours(start, end)`:

```json
{"hour": "2026-09-05T10", "requests": 12,
 "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
 "cache_creation_tokens": 0, "web_search_requests": 0,
 "cost": 1.2345, "partial": false, "unknown": false}
```
`cost` is the sum of known model costs (a float, `0.0` for empty hours); `unknown` is true when
any model in that hour has no price, mirroring `groups[]`.

**`GET /api/usage` — `_api_usage` (`dashboard_api.py:174`):**

```python
rng = _usage_range(request)
if isinstance(rng, web.Response): return rng
db = ...  # unchanged 500 when None
prices = build_price_lookup(await db.get_all_model_prices())
if rng.granularity == "day":
    rows = await db.query_usage_by_key_model(rng.start, rng.end)
    return web.json_response(build_usage_cost_json(rng.start, rng.end, rows, prices))
rows = await db.query_usage_bucket_by_key_model(rng.start, rng.end)
series_rows = await db.query_usage_bucket_series(rng.start, rng.end)
covered_from = await db.min_usage_bucket_hour()
return web.json_response(build_usage_cost_json(
    rng.start, rng.end, rows, prices, granularity="hour", covered_from=covered_from,
    series=build_usage_bucket_series(series_rows, prices, rng.start, rng.end)))
```

Day response = today's payload plus `"granularity": "day"`. Hour response:

```json
{
  "granularity": "hour",
  "start": "2026-09-05T10", "end": "2026-09-05T14",
  "covered_from": "2026-09-04T18",
  "total_known_cost": 12.34, "partial": false, "unknown": false,
  "groups": [ ...identical shape to today... ],
  "series": [ ...one entry per hour from start to end inclusive... ]
}
```
`covered_from` is `null` when the table is empty. It is `MIN(hour_utc)` — it says when writing
began, not that coverage is gap-free (a DB outage leaves the same hole in every usage table).

**`GET /api/usage/kinds` — `_api_usage_kinds` (`:189`):** goes through `_usage_range` (so garbage
now 400s instead of returning nothing); day → `query_usage_by_kind`, hour →
`query_usage_bucket_by_kind`; payload gains `"granularity"`. `TrafficView.svelte:86-91` sends
dates, so it is unaffected.

**Error cases, complete list:** `401` unauthorized (unchanged); `400` bad grammar / mixed grammar /
`start > end` / hour span over 92 days (all `text/plain`, matching `_usage_date_range`); `500`
`{"error": "usage database unavailable"}` when `app["db"]` is missing (unchanged). A `DbUnavailable`
from the Postgres breaker propagates as today (dashboard reads are meant to be loud — `db.py:1018-1027`).

### 5.5 UI — `web/src/views/UsageView.svelte`

The dashboard is UTC end to end (comment at `UsageView.svelte:25-26`); hour inputs must not go
through `datetime-local`, which is local time. Changes:

- State: `mode: 'day' | 'hour'` (`$state('day')`), existing `start`/`end` date strings, new
  `startHour`/`endHour` (`0–23`). Query string: day → as today; hour →
  `start=${start}T${hh(startHour)}&end=${end}T${hh(endHour)}` with `hh = n => String(n).padStart(2,'0')`.
- Form: a two-way toggle (`<label><input type="radio" bind:group={mode} value="day"> Days</label>`
  / `value="hour"` → "Hours (UTC)"). In hour mode an `<select>` of `00`…`23` appears after each
  date input. Existing `presets` buttons set `mode = 'day'`.
- New hour presets (computed in UTC, `nowHour = new Date(); nowHour.setUTCMinutes(0,0,0)`,
  label = `d.toISOString().slice(0,13)`): **Last 6h**, **Last 24h**, **Last 48h** (start =
  `nowHour − (N−1)h`, end = `nowHour`) and **Today by hour** (start = today `T00`, end = `nowHour`).
  Each sets `mode = 'hour'`, fills the four fields from the labels, and calls `load()`.
- `type Usage` gains `granularity: 'day' | 'hour'`, `covered_from?: string | null`,
  `series?: SeriesPoint[]`.
- Badge next to "Total known cost": `hour precision` / `day precision` (a `<span class="muted">`).
- Coverage notice (`<p class="warn">`, class exists in `app.css:16`) when
  `data.granularity === 'hour' && (data.covered_from == null || data.start < data.covered_from)`:
  "Hourly data starts at `{covered_from ?? '—'}` UTC; earlier hours are only available at day precision."
- Second chart, rendered only when `data.series` is present, in its own `.chart-wrap`: Chart.js
  bar, labels `p.hour.slice(5).replace('T', ' ') + 'h'` (e.g. `09-05 14h`), data `p.cost`,
  legend hidden — same options as the existing chart at `:76-91`. The existing per-group bar
  chart stays in both modes. Destroy/recreate on every `load()` like the existing one.
- Table: unchanged (the `groups` shape is identical).

### 5.6 Docs

`README.md:161-164` ("Accounting" paragraph): add `usage_bucket` — hour-grain, full dimension set,
never pruned, dashboard source for `YYYY-MM-DDTHH` ranges. `README.md:464-466`: mention that
`/api/usage/kinds` accepts the hour grammar. Add `0014_usage_bucket` to any migration list the
README keeps (`:249` shows the `db migrate` command; nothing else to update).

---

## 6. Rulings on the three open decisions

**A. `request_kind` in the PK — YES.** Cardinality is bounded by `classify_request`
(`request_classify.py:137-153`): exactly `helper | main | subagent`, plus `unknown` only when
`record_kwargs_for` swallows an exception or the body is not a dict (`anthropic_proxy.py:1877-1899`).
That is ≤4× rows, not "N×". `req_body` at `:2912` is the pristine client body, so the kind the
bucket sees is the kind `usage_kind_daily` sees — the projection property is real. Without it the
user's "key X vs key X's subagents in the last 6h" question needs yet another table later. The
price is the §3 #2 constraint: the `/api/usage` query must sum kinds away.

**B. Fix `flush()` now — YES, but not the way proposed.** Not re-merge (explicitly dropped on
2026-08-21; re-merging during a long outage grows without bound and the user accepted losing
accounting during outages). Instead: every table is written regardless of the others'
outcome, `usage_daily`+`usage_bucket` share one transaction, and `UsageFlushError` reports all
failures after all attempts while still handing the landed rows to window attribution. This is
strictly better than today on every axis the 2026-08-21 incident exposed: `usage_key_hourly` can
no longer be starved by a `usage_session` overflow, and the new dashboard source cannot drift from
`usage_daily`. Ordering alone ("bucket right after daily") is not a fix — it just picks which
table gets starved.

**C. Name — `usage_bucket`, with column `hour_utc`.** `usage_hourly` is the obvious name and it
is wrong for this codebase: the *unqualified* word "hourly" already means `usage_key_hourly`
everywhere — `build_usage_hourly_upsert_sql` (`db.py:1003`), `upsert_usage_hourly_batch`
(`:1465`), `_hour_buf` / `hour_rows` (`usage.py:602, 715`), "Hourly usage prune error"
(`anthropic_proxy.py:4573`). A table named `usage_hourly` would sit next to methods named
`*_usage_hourly_*` that write a *different* table; every future grep would mislead. Renaming
those methods touches the spend-limiter path the design rightly wants untouched. `usage_bucket`
collides with nothing. The grain then lives where it is self-describing: the column `hour_utc`,
carrying the same `'%Y-%m-%dT%H'` label as `usage_key_hourly.hour_utc` and `key_limits.HOUR_FMT`,
so one format constant serves all three. If a finer grain is ever wanted, it is a new column in a
new table regardless of what this one is called.

---

## 7. Tests

Existing pattern: `unittest.IsolatedAsyncioTestCase`, sqlite temp file via
`build_database_from_config(database_url="", db_path=...)`, or `tests.db_test_utils.connect_test_database`
for tests that must also run on Postgres when `TEST_DATABASE_URL` is set. Write each failing first.

**`tests/test_usage_tracker_bucket.py` (new)**

1. `test_record_buffers_full_dimension_key` — one `record(...)` with group, compat, kind produces
   exactly one `_bucket_buf` entry keyed `(hour, key, group, cred, provider, model, 1, kind)`.
2. `test_flush_writes_usage_bucket_rows` — frozen clock (`_FrozenDatetime` as in
   `test_usage_tracker_hourly.py:15-20`); three records (two same key/model, one other key);
   `SELECT * FROM usage_bucket` shows summed counters, `requests` counts, `group_name` NULL for `''`.
3. `test_bucket_is_a_projection_of_daily_and_kind` — records across two hours of one day and one
   hour of the next, several keys/models/kinds/compat flags; assert
   `SELECT substr(hour_utc,1,10), proxy_key, credential_id, provider, model, via_openai_compat, SUM(each counter) … GROUP BY …`
   equals `SELECT … FROM usage_daily` row-for-row, and the kind projection equals `usage_kind_daily`.
4. `test_flush_clears_bucket_buffer` — second flush writes nothing new.
5. `test_flush_return_contract_unchanged` — arity 15, daily rows only.
6. `test_secondary_failure_does_not_starve_the_others` — patch `db.upsert_usage_session_batch` to
   raise; `flush` raises `UsageFlushError` with `failures.keys() == {"usage_session"}`,
   `exc.rows` equals the daily rows, and `usage_daily`, `usage_bucket`, `usage_key_hourly`,
   `usage_kind_daily` all contain the data.
7. `test_pair_failure_reports_empty_rows_and_still_writes_secondaries` — patch
   `db.upsert_usage_daily_and_bucket_batch` to raise; `exc.rows == []`; hourly/kind/session written.
8. `test_daily_and_bucket_are_atomic_on_sqlite` — patch `smart_proxy.db.build_usage_bucket_upsert_sql`
   to return invalid SQL; after the raised `UsageFlushError`, `usage_daily` has **zero** rows
   (rollback), and a subsequent successful flush commits normally.

**`tests/test_usage_bucket_db.py` (new; `connect_test_database`, runs on both backends)**

9. `test_upsert_sums_counters_and_coalesces_group` — same PK upserted twice, second with
   `group_name=None`: counters summed, name kept.
10. `test_query_by_key_model_is_inclusive_and_collapses_kinds` — rows at `T09`, `T10`, `T14`,
    `T15` with two kinds at `T10`; query `("…T10", "…T14")` returns one row per
    `(key, group, provider, model, compat)` with kinds summed, joined `key_name`, excludes `T09`/`T15`.
11. `test_query_series_per_hour` — per-hour rows, inclusive bounds, ordered by hour.
12. `test_query_by_kind_keeps_kinds` — per-kind rows over the range.
13. `test_min_hour_none_when_empty_then_min` — `None`, then earliest label.
14. `test_usage_daily_snapshot_keeps_via_openai_compat` (in `tests/test_db_snapshot_roundtrip.py`) —
    export/restore of a `via_openai_compat=1` row leaves it `1` on the target (query the column, do
    not compare exports), and a native+compat pair for the same key/day restores without error.
15. `test_snapshot_round_trips_usage_bucket` (extend `test_export_snapshot_round_trips_all_tables`)
    and extend `test_replace_snapshot_clears_preexisting_kind_and_session_rows` with a pre-existing
    `usage_bucket` row that must be cleared.
16. `test_every_usage_counter_column_is_bigint` — add `"usage_bucket"` to `_COUNTER_TABLES`
    (`tests/test_usage_counter_bigint_postgres.py:59`). Postgres-only, skips otherwise.
17. `test_sqlite_transaction_rolls_back_on_exception` (new sqlite-only test in
    `tests/test_db_transaction_autocommit_db.py`) — `async with db.transaction(): execute(INSERT); raise`
    → row absent; a following insert+commit works.
18. `test_0014_creates_usage_bucket` (Postgres-only; skip without `TEST_DATABASE_URL`) —
    `run_postgres_migrations` is idempotent and `information_schema` shows the seven PK columns.

**`tests/test_usage_dashboard.py` (extend)**

19. `test_range_day_grammar_unchanged` — no params → last 7 days, `granularity == "day"`.
20. `test_range_hour_grammar_canonicalises` — `start=2026-09-05T9&end=2026-09-05T14` →
    `("2026-09-05T09", "2026-09-05T14")`.
21. `test_range_mixed_grammar_400`, `test_range_hour_24_400`, `test_range_hour_start_after_end_400`,
    `test_range_hour_span_over_92_days_400`, `test_range_hour_span_exactly_92_days_ok`.
22. `test_range_hour_defaults` — frozen clock; only `start=…T10` → `end` = current hour; only
    `end=…T10` → `start` = 23 hours earlier, crossing a day boundary.
23. `test_iter_hours_crosses_day_boundary` — `("2026-09-05T22", "2026-09-06T01")` → 4 labels.
24. `test_build_series_zero_fills_and_prices_per_model` — two models in one hour, one unpriced
    → that hour `unknown: true`, cost = the priced model only; empty hours present with zeros.
25. `test_cost_json_day_has_no_series_key`, `test_cost_json_hour_has_series_and_covered_from`.

**`tests/test_dashboard_api.py` (extend, `MagicMock`/`AsyncMock` db as at `:155-169`)**

26. `test_usage_hour_grammar_uses_bucket_queries` — asserts `query_usage_bucket_by_key_model`,
    `query_usage_bucket_series`, `min_usage_bucket_hour` were awaited with the canonical labels
    and `query_usage_by_key_model` was **not**; payload has `granularity: "hour"`, `covered_from`,
    `len(series) == 5` for `T10`–`T14`.
27. `test_usage_day_grammar_calls_are_unchanged` — extend `test_usage_returns_groups` to assert
    `payload["granularity"] == "day"` and `"series" not in payload`.
28. `test_usage_mixed_grammar_400`.
29. `test_kinds_hour_grammar_uses_bucket_query`, `test_kinds_garbage_range_400`.

**`tests/test_anthropic_proxy_usage_flush.py` (new, fakes only)**

30. `test_partial_flush_still_attributes_landed_rows` — tracker stub raising
    `UsageFlushError({"usage_session": RuntimeError()}, rows=[…])`; `_flush_usage` awaits
    `db.attribute_oauth_window_usage` with the deltas of those rows and re-raises.
31. `test_pair_failure_attributes_nothing` — `UsageFlushError(..., rows=[])` → no attribution call.

**`tests/test_database_wiring.py` (extend)**

32. `test_boot_guard_is_noop_on_sqlite`.
33. `test_boot_guard_raises_naming_pending_migrations` — fake object with `_backend="postgres"`,
    `ensure_migration_table`, `get_applied_migrations` returning all names but `0014_usage_bucket`
    → `RuntimeError` containing `0014_usage_bucket`.

**Manual UI checklist** (no web test harness exists): Last 24h shows 24 bars; toggling to Days
keeps the date fields; a range before `covered_from` shows the notice; the group table totals in
hour mode for a whole past day equal the day-mode totals for that date.

---

## 8. Slices / commit order

1. `db.py` sqlite `transaction()` rollback + test 17.
2. Table on both backends, upsert builder, `upsert_usage_bucket_batch`,
   `upsert_usage_daily_and_bucket_batch`, `min_usage_bucket_hour`, snapshot registry (+ the
   `usage_daily` fix), bigint guard — tests 9, 13–16, 18.
3. Tracker buffer, `flush()` rewrite, `UsageFlushError`, `_flush_usage` — tests 1–8, 30–31.
4. Boot guard — tests 32–33.
5. Queries + `_usage_range` + series builder + endpoints — tests 10–12, 19–29.
6. UI + README.

Full suite (700 tests at `7fe2386`) green after every slice.

## 9. Deployment

Postgres: `smart-proxy db migrate` **first**, then restart. `0014` is `CREATE TABLE` only — no lock
on existing tables, no need to stop the proxy before migrating (unlike `0012`; the
"stop every writer" procedure applies to PK-locking migrations only). With the boot guard, a
restart before migrating refuses to start and says so. sqlite: nothing to do; the table appears on
the next `connect()`.

Hour data begins at the first flush after restart; `covered_from` reports it. There is no backfill.

## 10. Invariants this design commits to

- For every `(date, proxy_key, credential_id, provider, model, via_openai_compat)`:
  `SUM(usage_bucket counters WHERE substr(hour_utc,1,10) = date) == usage_daily counters` — by
  construction: same `now`, one buffer swap under one lock, one transaction (§5.1, §5.2).
- For every `(date, proxy_key, request_kind, provider, model)`: the same against
  `usage_kind_daily`, **except** after a partial failure where `usage_kind_daily` failed and the
  pair succeeded (or vice versa) — each such event is alerted and named.
- `/api/usage` with day grammar performs exactly the DB calls it performs today.
- `usage_key_hourly`, `prune_usage_key_hourly`, `KeyLimiter` — unchanged, byte for byte.

## 11. Risks / known limitations

- **Unbounded growth (user decision).** Rows per hour ≈ keys × credentials-per-key × models × kinds
  (≤4) × compat (≤2) actually active in that hour. Realistic: 30 keys × ~2 models × ~3 kinds ≈
  180–270 rows/hour ≈ 2–2.4M rows/year ≈ 300–500 MB/year on Postgres including the PK index.
  When retention is decided, the hook is the hourly prune block in `_usage_flush_loop`
  (`anthropic_proxy.py:4565-4576`); a `DELETE … WHERE hour_utc < ?` on the PK prefix is cheap.
  Nothing in this change pre-wires it.
- **Attribution time is response completion**, as for every other usage table (`record()` runs
  after the stream ends, `anthropic_proxy.py:3249-3279`). A 40-minute stream that starts at 13:50
  is counted in the 14:00 bucket. Consistent with `usage_daily`; not fixable without a start
  timestamp.
- **Coverage has holes where the DB was down.** `covered_from` is a lower bound only. Both tables
  lose the same minutes, so the projection invariant still holds.
- **Partial-failure drift between `usage_bucket` and `usage_kind_daily`** is possible (see §10)
  and alerted, but not self-healing. Re-merge remains rejected.
- **Compat flag is client-asserted** (pre-existing, §3). The `· OpenAI` split is advisory.
- **Group renames split groups across hours**, exactly as across days today.
- **Old snapshots lacking `usage_bucket`** restore to an empty table (`snapshot.get(table, [])`,
  `db.py:2861`); old code restoring a new snapshot silently ignores the extra key.
- **A 92-day hour query returns 2208 series points plus the groups** — a few hundred KB of JSON.
  Chart.js handles it; the cap is there so nobody asks for three years by accident.
- **The boot guard makes an unmigrated Postgres a hard start failure.** That is the point, but it
  is a behaviour change for anyone who used to restart first and migrate later.
