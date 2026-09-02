# OAuth rate-limit window reset tracking — design

**Date:** 2026-07-02
**Status:** draft, awaiting user review

## Problem

The Anthropic subscription "7-day" rolling window appears to sometimes reset
after only 3–4 days. We want hard data: when do the windows *actually* reset
for our OAuth keys?

We already poll `https://api.anthropic.com/api/oauth/usage` frequently — the
Claude Code status bar hits our `/_oauth_usage` endpoint constantly (120 s
cache), and each cache miss calls `_build_oauth_usage_payload()` in
`src/smart_proxy/anthropic_proxy.py`. The upstream payload already contains
everything needed:

```json
{
  "five_hour": {"utilization": 4.0, "resets_at": "2026-07-02T02:10:00.028978+00:00", ...},
  "seven_day": {"utilization": 1.0, "resets_at": "2026-07-02T11:00:00.028998+00:00", ...},
  "limits": [
    {"kind": "session",       "group": "session", "percent": 4, "severity": "normal", "resets_at": "...", "scope": null, "is_active": true},
    {"kind": "weekly_all",    "group": "weekly",  "percent": 1, "severity": "normal", "resets_at": "...", "scope": null, "is_active": false},
    {"kind": "weekly_scoped", "group": "weekly",  "percent": 0, "severity": "normal", "resets_at": "...", "scope": {"model": {"display_name": "Fable"}}, "is_active": false}
  ]
}
```

A change in a window's `resets_at` **is** the reset event — no heuristics on
utilization drops needed. The spacing between consecutive `resets_at` values
per key/window is the observed window length.

**Verified quirk:** `resets_at` differs in the *fractional-seconds* part
between polls of the same window (`02:10:00.874141` vs `02:10:00.028978`).
Window identity must therefore compare `resets_at` truncated to **minute
precision** (observed values are minute-aligned).

**Verified quirk 2 (live data, 2026-07-02):** upstream also moves `resets_at`
by whole *minutes* between polls of the same window (two seven_day rows 5 min
apart appeared in production). Identity is therefore tolerance-based, not
exact: an observation within `RESETS_AT_JITTER_TOLERANCE_MINUTES` (120) of
the latest known window for that key/kind updates that window; only a larger
move is a `window_reset`. The row keeps its first-observed `resets_at`;
120 min of noise is negligible against multi-hour/-day windows.

## Approach

Chosen: **window event log, piggybacked on existing polls** (option A).

- A: **one row per window instance** — a row describes the whole lifecycle of
  one window ("usage started at 05:00, peaked at 55 %, reset 4.2 days later"),
  updated in place on every successful usage fetch; a *new* row appears only
  when `resets_at` changes (= the window actually reset). Compact
  (~70 rows/week/key), queryable, no new pollers. **Chosen.**
- B: full snapshot table (every poll → row). More flexible but noisy; we only
  care about transitions. Rejected (YAGNI).
- C: log lines only. Not queryable, lost on rotation. Rejected.

No dedicated background poller for now: the status-bar traffic already gives
near-continuous coverage. If coverage gaps appear (nobody working for days),
a poller can be added later — the write path is a single function call.

## Data model

New table in `smart-proxy.db` (migration in `src/smart_proxy/db.py`, same
`CREATE TABLE IF NOT EXISTS` + migrations-list pattern as `rate_limit_log`):

```sql
CREATE TABLE IF NOT EXISTS oauth_window_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id           TEXT    NOT NULL,   -- anthropic_keys.id
    window_kind      TEXT    NOT NULL,   -- see naming below
    resets_at        TEXT    NOT NULL,   -- minute-truncated ISO UTC, window identity
    resets_at_raw    TEXT    NOT NULL,   -- as last received from upstream
    first_seen_at    TEXT    NOT NULL,   -- ISO UTC of first observation
    first_active_at  TEXT,               -- first observation with utilization > 0
    last_seen_at     TEXT    NOT NULL,
    observations     INTEGER NOT NULL DEFAULT 1,
    last_utilization REAL,               -- percent 0–100 at last observation
    max_utilization  REAL,
    max_utilization_at TEXT              -- when max_utilization was first observed
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_owl_identity
    ON oauth_window_log(key_id, window_kind, resets_at);
```

`window_kind` naming:

- top-level windows: `five_hour`, `seven_day` (other non-null top-level
  `seven_day_*` objects use their JSON key verbatim);
- `limits[]` entries: `limit:<kind>`, with model scope appended when present —
  `limit:session`, `limit:weekly_all`, `limit:weekly_scoped:Fable`.

Top-level `seven_day` and `limits.weekly_all` are duplicates today, but both
are recorded — cheap, and protects against either representation disappearing.

Upsert per observation (in-place — polls never add rows for a known window):

- row with same `(key_id, window_kind, resets_at)` exists → bump
  `last_seen_at`, `observations`, `last_utilization`; set `first_active_at`
  if it is NULL and utilization > 0; when utilization exceeds the stored
  `max_utilization`, update it and stamp `max_utilization_at`;
- no row → INSERT. If an older row for the same `(key_id, window_kind)` with a
  different `resets_at` exists, this insert *is* the observed reset; log INFO:
  `oauth window reset key=<id> kind=seven_day prev_resets_at=... new_resets_at=... span=4.2d`.

A finished row reads as the aggregated story the tracking is for: usage in
this window first appeared at `first_active_at`, reached `max_utilization`
(at `max_utilization_at`), was last seen at `last_utilization` just before
the reset, and the next row's `resets_at` minus this row's `resets_at` is the
actual window length.

## Write path

In `_build_oauth_usage_payload()` (anthropic_proxy.py, ~line 1899), after a
successful 200 + JSON parse: extract observations from the payload and call a
new `db.record_oauth_window_observations(key_id, observations, seen_at)`.

- Extraction is a pure helper `_extract_window_observations(usage: dict) ->
  list[WindowObservation]` (kind, resets_at_raw, utilization) — unit-testable
  without HTTP.
- Skip windows with missing/unparseable `resets_at`; never let recording
  errors break the usage endpoint (wrap in try/except with a warning log).
- Recording happens only on real upstream fetches (cache hits never reach
  this code), so `observations` counts are meaningful.

## Read path

New endpoint `GET /_oauth_usage_history` (same optional-auth rule as
`/_oauth_usage`, i.e. the `oauth_usage_require_auth` flag). Response:

```json
{
  "generated_at": "...",
  "keys": [
    {
      "id": "...", "name": "pro-sp-auth",
      "windows": {
        "seven_day": [
          {
            "resets_at": "2026-07-02T11:00:00Z",
            "first_seen_at": "...", "first_active_at": "...",
            "last_seen_at": "...",
            "observations": 812,
            "last_utilization": 34.0,
            "max_utilization": 71.0, "max_utilization_at": "...",
            "span_days_since_prev": 4.19
          }
        ],
        "limit:weekly_scoped:Fable": [ ... ]
      }
    }
  ]
}
```

`span_days_since_prev` = `resets_at − previous row's resets_at` for the same
key/kind, in days — the headline number that answers "is the 7-day window
really 7 days". Computed at read time (no stored derived data). Query params:
`?kind=seven_day` filter, `?limit=N` per-kind row cap (default 50, newest
first).

## Error handling

- Recording failures (DB locked, weird payload) log a warning and never fail
  the `/_oauth_usage` response.
- Unparseable `resets_at` → that window skipped for that observation.
- Key deleted later: rows remain (key_id is not a FK), history endpoint shows
  them only for keys still present in `anthropic_keys`.

## Testing

Unit tests (pytest, following `tests/test_anthropic_proxy_oauth_usage_endpoint.py`
patterns with fake transport):

1. `_extract_window_observations`: real-shaped payload → expected kinds incl.
   scoped limit naming; minute truncation; null windows skipped.
2. Upsert: same window twice → one row, `observations=2`, utilization fields
   updated; changed `resets_at` → second row; span computation across rows.
3. Endpoint: seeded DB → response shape, `span_days_since_prev`, `kind`
   filter, auth flag honored.
4. Integration: usage fetch through the handler records rows (extend existing
   endpoint test).

## Amendment (2026-07-02): undeclared reset detection

Observed in practice: the weekly window showed 85 % utilization, then dropped
to 0 while the API still claimed ~13 h until `resets_at`. That divergence —
the window resetting *earlier* than declared — is the primary thing this
tracking exists to catch, and `resets_at`-identity alone misses it (the drop
just updates the same row in place).

Addition: when an observation for an **existing** window row (same
`resets_at`) shows utilization falling by **≥ 5 percentage points in a single
step** versus the stored `last_utilization`, record a drop event in a new
table:

```sql
CREATE TABLE IF NOT EXISTS oauth_window_drop_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id           TEXT NOT NULL,
    window_kind      TEXT NOT NULL,
    resets_at        TEXT NOT NULL,   -- what the API claimed at drop time
    dropped_at       TEXT NOT NULL,   -- observation that saw the low value
    prev_seen_at     TEXT NOT NULL,   -- when from_utilization was last seen
    from_utilization REAL NOT NULL,
    to_utilization   REAL NOT NULL
);
```

The 5 pp single-step threshold separates a genuine reset (85→0 between two
polls ~2 min apart) from the gradual decline a rolling window produces as old
usage ages out. The window row itself still updates in place (`max_utilization`
keeps the pre-drop peak, `last_utilization` follows down).

`record_oauth_window_observations` returns typed events —
`{"type": "window_reset", ...}` (as before) and
`{"type": "utilization_drop", "from_utilization", "to_utilization",
"prev_seen_at", "dropped_at", "resets_at", ...}` — and the proxy logs both at
INFO. `/_oauth_usage_history` gains a per-key `drops` section (grouped by
kind, newest first, same `kind`/`limit` params) with computed
`hours_before_claimed_reset` = `resets_at − dropped_at`, answering "how long
before the promised reset did it actually reset". Whether another reset then
happens at the originally claimed time shows up as a normal `window_reset`
event on the same timeline.

## Out of scope (possible later)

- Dedicated background poller for coverage during idle periods.
- Alerting/notification when a reset is detected earlier than expected.
- UI/HTML view (the JSON endpoint is enough to eyeball or script against).
