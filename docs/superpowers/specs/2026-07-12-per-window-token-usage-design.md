# Per-window token usage for OAuth keys

Date: 2026-07-12
Status: approved

## Problem

`/_oauth_usage_history` shows, per observed rate-limit window (`five_hour`,
`seven_day`, `limit:*`), only upstream utilization percentages
(`oauth_window_log.last_utilization` / `max_utilization`). Actual token traffic
lives in `usage_daily`, but only at day granularity, so per-window token totals
can be reconstructed only approximately (window edges land mid-day). We want
exact per-window accounting of the tokens that passed through the proxy for
each OAuth key, in the same shape as `/_usage` (per model, with cost).

## Constraints and context

- Window observations are recorded only when `/_oauth_usage` is hit
  (`_build_oauth_usage_payload` → `_record_window_observations`); there is no
  background poll. A window rollover becomes visible in `oauth_window_log`
  only at the next poll.
- `UsageTracker` buffers per-request usage in memory and flushes every 60 s
  (`_USAGE_FLUSH_INTERVAL`) to `usage_daily`. `flush()` is also called from
  the aiohttp cleanup hook and from `scheduler.py`.
- Window identity is `(key_id, window_kind, resets_at)` with jitter tolerance;
  `oauth_window_log.id` is a stable PK for an observed window instance.
- `UsageTracker` is shared by both proxies; smart_proxy traffic must not gain
  new per-flush DB work.

## Decision (approved approach: "A — pending bucket")

Accumulate actual token counters per observed window instance, per model, in a
child table of `oauth_window_log`. Attribute at flush time to the latest known
window of each kind. Tokens that arrive after a window expired but before the
next poll observes the successor go to a persistent pending bucket, drained
into the new window row when it is first observed.

Rejected alternatives:
- **B (A + poll-on-rollover):** flush triggers an immediate one-key upstream
  usage poll when it sees an expired window. Deferred — easy follow-up if
  pending latency (one dashboard poll interval) becomes annoying.
- **C (predict next window identity):** `resets_at` depends on first activity
  after expiry; predicted identity may miss the jitter-tolerance match and
  orphan rows.
- **Hourly usage table + read-time join:** heavier write path and read logic;
  user explicitly prefers counters on the window log.

## Schema (additive migration)

```sql
CREATE TABLE IF NOT EXISTS oauth_window_usage (
    window_id                 INTEGER NOT NULL,   -- oauth_window_log.id
    model                     TEXT    NOT NULL,
    input_tokens              INTEGER NOT NULL DEFAULT 0,
    output_tokens             INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens  INTEGER NOT NULL DEFAULT 0,
    web_search_requests       INTEGER NOT NULL DEFAULT 0,
    requests                  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (window_id, model)
);

CREATE TABLE IF NOT EXISTS oauth_window_usage_pending (
    key_id                    TEXT NOT NULL,
    window_kind               TEXT NOT NULL,
    model                     TEXT NOT NULL,
    -- same eight counter columns as oauth_window_usage --
    updated_at                TEXT NOT NULL,
    PRIMARY KEY (key_id, window_kind, model)
);
```

Both tables are added to `SCHEMA` and to `MIGRATIONS` (idempotent
`CREATE TABLE IF NOT EXISTS`). No existing table changes; no coordinated
service stop needed (the usual deploy restarts both services anyway, which is
required because shared `usage.py` changes).

## Write path

1. `UsageTracker.flush(db)` returns the flushed row tuples (same tuples it
   passes to `upsert_usage_batch`) instead of a bare count. Call sites:
   `_usage_flush_loop` and the cleanup hook in `anthropic_proxy.py` (both go
   through one new helper), and `scheduler.py` (ignores the return value).
2. The anthropic-proxy helper filters rows to `provider == 'anthropic'`,
   aggregates the eight counters by `(credential_id, model)`, and calls a new
   DB method `attribute_oauth_window_usage(deltas, now)`.
3. For each `(key_id, model)` delta, the DB method loads the latest
   `oauth_window_log` row per `window_kind` for that key:
   - window still live (`now <= resets_at_raw`, falling back to `resets_at`
     if raw is unparseable) → upsert-increment `oauth_window_usage
     (window_id, model)`;
   - window expired → upsert-increment `oauth_window_usage_pending
     (key_id, window_kind, model)`, set `updated_at = now`;
   - kind never observed for this key → skip (tokens remain visible in
     `usage_daily` only).
   Only top-level window kinds accumulate counters; `limit:*` kinds are
   skipped at write time — their upstream utilization is model-scoped while
   proxy counters are whole-key, so per-window usage for them would be
   misleading. The per-model breakdown of the overlapping `seven_day` window
   covers the same need.

API-key credentials naturally no-op (no `oauth_window_log` rows).

## Pending drain

In `record_oauth_window_observations`, whenever a **new** window row is
inserted for `(key_id, window_kind)`: move any
`oauth_window_usage_pending` rows for that `(key_id, window_kind)` into
`oauth_window_usage` under the new `window_id`, then delete them. Drain runs
unconditionally on new-row insert (first-ever rows have no pending by
construction, so this is harmless there).

## Read path: `/_oauth_usage_history`

For each window entry, add a `usage` block built from `oauth_window_usage`
joined via `window_id`:

```json
"usage": {
  "models": {
    "<model>": {"input_tokens": ..., "output_tokens": ..., "...": ...,
                 "requests": ..., "cost_usd": <float|null>}
  },
  "totals": {"input_tokens": ..., "...": ..., "requests": ...,
              "cost_usd": <float|null>, "cost_partial": <bool>}
}
```

Cost uses the same helpers as `/_usage` (`build_price_lookup` +
`calculate_cost` from `usage.py`); models without a price row get
`cost_usd: null` and set `cost_partial` on totals. Per key, add a `pending`
block so tokens awaiting the next poll are visible rather than silently
absent:

```json
"pending": {
  "<window_kind>": {
    "models": {"<model>": {"input_tokens": ..., "...": ..., "requests": ...}},
    "updated_at": "<ISO>"
  }
}
```

The current (latest) window
appears in history as soon as it is observed, so "how much has passed through
this key in the current seven_day window" is directly readable.

## Known limitations (documented behavior)

- Only proxy traffic is counted; use of the same OAuth account outside the
  proxy affects upstream utilization % but not these counters.
- Boundary accuracy ≈ one flush interval (60 s) around `resets_at`.
- If several rollovers of a kind happen between polls, all pending traffic is
  drained into the newest observed window (attribution granularity = poll
  frequency).
- On an undeclared reset (`utilization_drop` while `resets_at` unchanged),
  tokens keep accumulating in the same window row.
- Tokens recorded before the first-ever observation of a kind for a key are
  not window-attributed.
- Crash loses at most the in-memory tracker buffer (≤ 60 s), same as
  `usage_daily`; pending is persistent.
- `limit:*` window instances carry no `usage` block (counters are kept only
  for top-level kinds).

## Testing

- **DB unit tests:** attribution to live window; routing to pending on expired
  window; skip on never-observed kind; drain on new-window insert (including
  idempotent re-observation of the same window); upsert-increment semantics.
- **Endpoint tests** (extend `test_anthropic_proxy_oauth_usage_history.py`):
  `usage` block totals and per-model costs, `cost_partial` when a price is
  missing, `pending` block presence.
- **E2E** (pattern of `test_oauth_window_tracking_e2e.py`): proxy request →
  flush → usage lands on current window; window expiry → flush → pending →
  poll observes new window → pending drained into it.
