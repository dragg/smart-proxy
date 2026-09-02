# Per-proxy-key spend limits

**Date:** 2026-07-30
**Components:** `src/smart_proxy/db.py`, `src/smart_proxy/db_migrations.py` (two new tables + accessors),
`src/smart_proxy/usage.py` (`UsageTracker` hourly buffer), `src/smart_proxy/key_limits.py` (**new** —
`KeyLimiter`), `src/smart_proxy/anthropic_proxy.py` (enforcement gate, spend accumulation,
`/_oauth_usage` block, wiring), `src/smart_proxy/dashboard_api.py` +
`web/src/views/KeysView.svelte` (limits form)
**Status:** design (approved)

## Problem / intent

`sp-*` proxy keys are currently unbounded: any holder can spend the account's quota without limit.
Add a **per-key spend cap in USD over a 24h window** that the proxy enforces itself, returning a
429 shaped like Anthropic's own rate-limit error but with a SmartProxy-specific message. Limits are
edited from the `/_app/#keys` tab and take effect immediately — no restart, no reload.

Every existing key stays unlimited. Limits are opt-in per key.

## Decisions (confirmed with the user)

1. **Window anchor:** local midnight, timezone-configurable, defaulting to `Europe/Paris` (the
   codebase already treats Paris as "server local" for OAuth smoke windows).
2. **Spend granularity:** hourly buckets, built now rather than deferred — so future limit kinds
   (12h, 36h, rolling-5h, monthly) need no new storage work.
3. **Limits are a table of (key, kind) rows,** not a column — a new limit kind is a new row, not a
   migration. The UI is a *form* driven by a kind list, not a single hardcoded field.
4. **Hot path does no I/O:** the enforcement check is an in-memory dict lookup. The DB is the
   recovery source (startup, window rollover, explicit reload), not a per-request dependency.
5. **`/_oauth_usage?key=` reports the limit as an entry appended to `usage.limits[]`,** tagged
   `kind: "smartproxy_daily_usd"` and `source: "smartproxy"`.

## Non-goals

Limits on Anthropic pool keys (`anthropic_keys`); per-model or per-request-kind limits; token- or
request-count limits; pre-flight cost estimation (see "Overshoot" below); alerting on limit hits;
shared/pooled budgets across several keys.

## Design

### 1. Data model — two new tables

Both are **new tables only**. Nothing touches `usage_daily` / `usage_session`, so the deploy needs
no stop-every-writer dance (per the PK-locking-migration note in the ops memory).

```sql
-- Limit configuration: one row per (key, kind).
CREATE TABLE IF NOT EXISTS proxy_key_limits (
    proxy_key  TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- 'daily_usd' is the only kind today
    amount     REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (proxy_key, kind)
);

-- Hourly spend buckets: tokens, not dollars.
CREATE TABLE IF NOT EXISTS usage_key_hourly (
    hour_utc                 TEXT    NOT NULL,   -- '2026-07-30T14'
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

**Why tokens and not dollars in the bucket table:** cost is then always recomputed from the current
`model_prices`, so the limiter and the Usage tab can never disagree, and a price correction applies
retroactively exactly as it already does everywhere else.

**No row in `proxy_key_limits`, or `amount <= 0`, means unlimited.** That is what makes every
existing key unlimited with zero backfill.

Migration mechanics, matching the conventions already in the repo:

- SQLite: add both `CREATE TABLE`s to `SCHEMA_SQL` (`db.py:61`) for fresh DBs. No `MIGRATIONS`
  (`db.py:273`) entries are needed — that list is for `ALTER`s; `SCHEMA_SQL` runs `CREATE TABLE IF
  NOT EXISTS` on every connect, so existing SQLite DBs pick the tables up.
- Postgres: one new `POSTGRES_MIGRATIONS` entry named `0010_proxy_key_limits` (the tuple has
  duplicate `0002`/`0003` prefixes historically; `0009_anthropic_keys_role` is the current highest,
  so `0010` is unique). `REAL` → `DOUBLE PRECISION`.
- Add both tables to `SNAPSHOT_TABLE_SPECS` (`db.py:474`) so SQLite↔Postgres snapshot roundtrips
  carry them.

New DB accessors on `Database`:

- `list_proxy_key_limits() -> list[dict]` — all rows, for `KeyLimiter` load.
- `set_proxy_key_limit(proxy_key, kind, amount)` — upsert.
- `delete_proxy_key_limit(proxy_key, kind)` — remove (→ unlimited).
- `upsert_usage_hourly_batch(rows)` — additive upsert, mirroring `upsert_usage_kind_batch`
  (`db.py:1143`).
- `query_usage_key_hourly(start_hour, end_hour) -> list[dict]` — token sums grouped by
  `proxy_key, model` over `hour_utc >= start AND hour_utc < end`.
- `prune_usage_key_hourly(before_hour) -> None` — retention delete (caller doesn't need the count).

### 2. Writing the buckets — `UsageTracker`

`UsageTracker` (`usage.py:590`) gains a third buffer next to `_kind_buf` / `_session_buf`:

- `_hour_buf: dict[(hour_utc, proxy_key, model), list[int]]` — the same 8 counters.
- `record()` computes `hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")` alongside the
  existing `date` and accumulates into `_hour_buf`.
- `flush()` snapshots and clears it with the others and calls `db.upsert_usage_hourly_batch()`.

No new write path and no new background loop: the existing `_usage_flush_loop`
(`anthropic_proxy.py:3163`, 60s) and the shutdown flush in `_on_cleanup` (`anthropic_proxy.py:3533`)
already cover it.

**Retention:** `_usage_flush_loop` calls `prune_usage_key_hourly` at most once per hour (guarded by
a last-run timestamp on the app) deleting rows older than **35 days** — enough for any plausible
window up to monthly, and it keeps the table bounded.

### 3. `KeyLimiter` — new module `src/smart_proxy/key_limits.py`

Owned by the app next to the pool (`app["key_limiter"]`), built in `_on_startup`
(`anthropic_proxy.py:3488`).

```python
LIMIT_KINDS = {
    "daily_usd": LimitKind(
        id="daily_usd",
        window_hours=24,
        label="24h",          # used in the 429 message
    ),
}
```

State:

- `_limits: dict[proxy_key, dict[kind, float]]` — from `proxy_key_limits`.
- `_spent: dict[proxy_key, float]` — live USD in the current window.
- `_window_start: datetime` — current window start (UTC-normalised).
- `_prices: dict` — `build_price_lookup(await db.get_all_model_prices())`.

Methods:

- `async load()` — reads `proxy_key_limits` **and** `model_prices`, then `seed()`. Called at
  startup and from `POST /api/reload` + `GET|POST /_reload`, so a price or limit change outside the
  dashboard is picked up by the same lever that already reloads the pool.
- `async seed()` — `query_usage_key_hourly(window_start, window_end)` → `calculate_cost` per
  `(proxy_key, model)` row → per-key USD totals into `_spent`. This is the "get the real state from
  the DB" path.
- `check(proxy_key) -> LimitBlock | None` — pure dict lookups, no I/O. Returns the blocking kind
  and seconds-until-reset when `_spent >= amount`, else `None`. Calls `_roll_if_needed()` first.
- `add(proxy_key, model, usage_tuple)` — computes cost with `_prices` and adds to `_spent`.
  Unknown-priced models contribute `0.0` (same as the dashboard treats them).
- `set_limit(proxy_key, kind, amount | None)` — writes the DB row (or deletes it) and updates
  `_limits` in the same call, which is what makes a UI edit take effect on the very next request.
- `snapshot(proxy_key) -> dict` — `{limit_usd, spent_usd, remaining_usd, percent, resets_at,
  exceeded}` per configured kind, for the dashboard API and `/_oauth_usage`.
- `_roll_if_needed()` — recomputes the window from the clock; on rollover clears `_spent` to 0 and
  advances `_window_start`. A new window genuinely starts empty for the process that serves and
  records every request.

**Window math** lives here and nowhere else: `window_start = local_midnight(now, tz)` where `tz` is
`ANTHROPIC_PROXY_LIMIT_WINDOW_TZ` (new setting in `config.py`, default `Europe/Paris`);
`resets_at = window_start + 24h`; `retry_after = ceil((resets_at - now).total_seconds())`. Paris's
offset is a whole number of hours in both DST phases, so window edges always land on a bucket
boundary. Setting the variable to `UTC` is a supported one-line change.

**Accuracy trade-off (stated explicitly):** `_spent` is exact for the lifetime of the process. A
restart re-seeds from `usage_key_hourly` and therefore loses at most the <60s the dying process had
not yet flushed. If a second proxy process ever shared the same DB, the two counters would drift
apart until a reload — the enforcement is per-process by design. A client that disconnects
mid-stream also goes unrecorded: no usage event fires, so nothing is added to `_spent`, even though
the tokens generated before the disconnect were already billed upstream. This is pre-existing
behavior for all usage accounting (the Usage tab has the same gap), not something this feature
introduces.

### 4. Enforcement — `_proxy_handler`

Inserted in `anthropic_proxy.py` immediately after `path = request.path` (line 1925), before any
body rewriting and before `pool.pick()`:

```python
if _is_billable_path(request.method, path):
    block = limiter.check(usage_proxy_key)
    if block is not None:
        message = (
            f"SmartProxy: you have reached your {block.label} limit. "
            f"Retry in {_humanize_seconds(block.retry_after)}"
        )
        return web.Response(
            status=429,
            headers={"retry-after": str(block.retry_after), "x-should-retry": "false"},
            body=json.dumps({
                "type": "error",
                "error": {"type": "rate_limit_error", "message": message},
            }).encode(),
            content_type="application/json",
        )
```

- `_is_billable_path` → `method == "POST" and "/v1/messages" in path and not
  path.endswith("/count_tokens")`. Token counting is free and clients call it constantly; blocking
  it would break them for no budget benefit. Every other path (health, OAuth, dashboard) is
  unaffected.
- `_humanize_seconds` (`anthropic_proxy.py:164`) already renders the two largest units — `59m 14s`,
  `5h 12m` — matching the format in the Anthropic message the user quoted.
- The response mirrors the existing pool-exhaustion 429 (`anthropic_proxy.py:1958`): same envelope,
  same `retry-after` in raw seconds, same `x-should-retry: false`.
- **OpenAI-compat is covered for free**: `_chat_completions_handler` loops back through
  `POST /v1/messages` on this same app (`openai_compat.py:730`), so it hits this gate and its
  `anthropic_error_to_openai` translates the 429 for OpenAI clients.
- **Passthrough is unlimited by construction**: `sk-ant-*` callers and keyless-open mode record
  under `usage_proxy_key == "claude-passthrough"`, which has no `proxy_key_limits` row.

**Overshoot semantics:** a request's cost is only known after its response completes, so the check
is "already at or over the cap" — the request that crosses the line runs to completion and the
*next* one is refused. `check()` takes no reservation, so this isn't limited to one request: any
number of concurrent requests can each observe `spent < amount` and proceed before any of their
costs are added. The cap can therefore be overshot by the *combined* cost of every request that was
already in flight when the cap was crossed, not merely by one request's own cost — for serial
traffic (the common case) those coincide, but under concurrency the bound is the sum, not a single
request. Pre-flight estimation is deliberately out of scope: it would need input-token counting
before forwarding, and would still misestimate output.

**Accumulation:** in the success path, right after the existing `tracker.record(...)`
(`anthropic_proxy.py:2194`), add `limiter.add(usage_proxy_key, model, usage)`. One extra call at one
site; the limiter owns the price lookup so the handler stays ignorant of cost.

### 5. Dashboard API

- `GET /api/keys` (`dashboard_api.py:142`) — each key gains:

  ```json
  {
    "key_prefix": "sp-a1b2c3d4e", "name": "laptop", "active": true, "created_at": "…",
    "limits": { "daily_usd": 25.0 },
    "usage":  { "daily_usd": { "limit_usd": 25.0, "spent_usd": 18.42, "remaining_usd": 6.58,
                               "percent": 73.7, "resets_at": "2026-07-31T00:00:00+02:00",
                               "exceeded": false } }
  }
  ```

  For an unlimited key, `limits` is `{}` and `usage.daily_usd` still reports `spent_usd` /
  `resets_at` with `limit_usd: null` — so the tab shows what a key is burning before you decide on
  a cap.

- `POST /api/keys/limits` — **new**. Body `{ "created_at": "…", "limits": { "daily_usd": 25.0 } }`.
  Semantics are a **partial update**: only the kinds present in `limits` are touched, a numeric
  value upserts that kind's row, and an explicit `null` deletes it (→ unlimited for that kind). A
  kind absent from the object is left exactly as it was, so the endpoint stays safe to call from a
  form that renders a subset of kinds.
  Gated by `_action_authorized` (strict `sp-*`, like the other mutating endpoints). The key is
  resolved by `created_at`, not prefix — same reasoning as `/api/keys/active`
  (`db.py:1093`): distinct keys can share a 12-char prefix. Rejects unknown kinds and negative
  amounts with 400. Calls `limiter.set_limit(...)`, so the change is live before the response is
  written.
- `POST /api/reload` also calls `limiter.load()`.

### 6. `/_oauth_usage?key=sp-…`

When `?key=` names a known proxy key **that has at least one configured limit**, each entry's
`usage.limits[]` gets one appended object per configured kind:

```json
{
  "kind":      "smartproxy_daily_usd",
  "source":    "smartproxy",
  "limit_usd": 25.0,
  "spent_usd": 18.42,
  "percent":   73.7,
  "resets_at": "2026-07-31T00:00:00+02:00"
}
```

`kind` is prefixed `smartproxy_` and `source` is explicit, so no consumer can mistake it for an
Anthropic-issued limit. When `?key=` is absent, unknown, or the key has no limit, the payload is
byte-for-byte what it is today.

**Cache correctness — the one real hazard here.** `_oauth_usage_handler` caches the whole payload
for `oauth_usage_cache_seconds` (default 60) across *all* callers
(`anthropic_proxy.py:2525-2572`). The injection must therefore happen **after** the cache read, on
every request, mutating a copy — never inside the cached payload. Otherwise key A's spend would be
served to key B for up to a minute. The cache continues to hold only upstream-derived data.

Because `spent_usd` comes from the limiter's in-memory counter, it is live even when the surrounding
payload is served from cache.

`?key=` additionally satisfies auth when `oauth_usage_require_auth` is on, consistent with
`_usage_dashboard_authorize` (`anthropic_proxy.py:875`).

**Statusline:** `web/src/lib/statusline.sh` currently skips every `limits[]` entry whose kind is not
`weekly_scoped` (line 67), so it ignores the new entry until taught otherwise. Adding a branch that
renders `24h: 74% ($18.42/$25 ↺ 6h12m)` is a small, optional follow-up, listed in the plan as its
own step so it can be dropped.

### 7. UI — Keys tab

`KeysView.svelte` gains a **limits form**, not a hardcoded field, because more limit kinds are
expected:

```ts
const LIMIT_KINDS = [
  { id: 'daily_usd', label: '24h spend limit', unit: '$', hint: 'empty = unlimited' },
]
```

- The table gains a **Limits** column rendering the configured limits compactly — `$25.00 / 24h`,
  or `—` when unlimited — plus **Spent** (`$18.42 · 74%`) and **Resets in** (`6h 12m`), so a blocked
  key explains itself on screen.
- Each row gets an **Edit limits** button that toggles an inline `<tr>` below it containing a form
  built by iterating `LIMIT_KINDS`: one number input per kind, prefilled from `limits`, blank
  meaning unlimited. **Save** posts to `/api/keys/limits` and reloads the list; **Cancel** closes.
- Adding a future kind is one entry in `LIMIT_KINDS` plus one entry in the server-side kind
  registry. No layout work.

Styling follows the existing table/form idiom in the file — no new dependencies.

## Error handling

- DB unavailable at startup → `KeyLimiter.load()` raises and startup fails, exactly as the pool
  load already does. There is no "fail open silently" path for limits.
- A price lookup that cannot price a model contributes `$0` to spend, consistent with the Usage tab.
  It never blocks a request on an unknown model.
- `limiter.add()` is wrapped so a bug there can never break a response that has already been
  streamed to the client — it logs and continues.
- `POST /api/keys/limits` with an unknown kind or an amount that isn't a finite positive number →
  400 with a message; an unknown `created_at` → 404 (matching `_api_key_active`'s precedent,
  `dashboard_api.py`). Nothing is written until every kind in the request has been validated.

## Testing

New:

- `tests/test_key_limits.py` — window math (rollover, `resets_at`, `retry_after`, both Paris DST
  phases, and `UTC` via the setting); seeding from hourly rows; `check`/`add`; unlimited keys; a
  limit change taking effect without a reload.
- `tests/test_anthropic_proxy_key_limit.py` — over-limit returns 429 with the exact message,
  `retry-after`, and `x-should-retry: false`; under-limit forwards; `count_tokens` is not gated;
  `claude-passthrough` is unlimited.
- `tests/test_usage_tracker_hourly.py` — `_hour_buf` accumulation, hour keying, flush row shape.

Extended:

- `tests/test_dashboard_api.py` — `GET /api/keys` limit/usage fields; `POST /api/keys/limits`
  set/clear/validation; mutation requires a real `sp-*`.
- `tests/test_anthropic_proxy_oauth_usage_endpoint.py` — `?key=` appends the entry; absent without
  `?key=`; **not leaked across cached responses when two different keys hit the cache**.
- `tests/test_anthropic_db_contract.py` and `tests/test_db_snapshot_roundtrip.py` — the two new
  tables on both backends.

## Deployment

1. `git pull`, `.venv/bin/pip install -e .`
2. `.venv/bin/python -m smart_proxy db migrate` — creates two new tables only; no lock on `usage_daily`,
   so writers do **not** need to be stopped first.
3. Rebuild the SPA (`cd web && npm ci && npm run build`) — the build output is gitignored.
4. Optional: set `ANTHROPIC_PROXY_LIMIT_WINDOW_TZ` in `.env` (defaults to `Europe/Paris`).
5. `systemctl restart smart-proxy`.

Hourly buckets start filling on the first flush after restart, so a key's first window after deploy
counts from the restart rather than from midnight. Stated here so the first day's numbers aren't
mistaken for a bug.
