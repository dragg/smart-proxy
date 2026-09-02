# OAuth limit-wipe forensics — design

**Date:** 2026-09-01
**Status:** draft, awaiting user review
**Supersedes nothing.** Extends `2026-07-02-oauth-window-reset-tracking-design.md`.

## Problem

A `seven_day` counter dropped to 0% mid-window while its declared
`resets_at`, still ~41 h away, did not move.
Nothing failed: no 429, no error, no alert. The event was recorded — the
existing `oauth_window_drop_log` caught it within 2 m 20 s — but nothing
surfaces it, and, more importantly, **we cannot say what caused it**, because
the write path discards almost the entire upstream payload.

This has happened at least three times.
The goal of this design is **not** to record the fact again. It is to retain
enough context that the *next* occurrence is diagnosable.

## Evidence

Established from live production data (consumption figures and incident
dates removed; the measurements below describe upstream behaviour, not this
deployment's usage):

1. All three recorded weekly drops land on exactly `0.0`, whatever level
   the counter had reached beforehand.
2. Each coincides to sub-second precision with the birth of a new `five_hour`
   window row — same poll, same observation timestamp.
3. That new 5h window opened **before** the previous 5h window's own declared
   `resets_at`: by 159, 70 and 18.5 minutes respectively.
4. Control: of 294 `five_hour` transitions only 6 opened >10 min early. Three
   are these events; the other three are all on one key on an earlier date, the
   tracker's first two days in production, where the original design doc
   independently documents `resets_at` jitter. Of 141 transitions following a
   long (>100 min) low-usage (≤10%) window, **zero** opened early — a quiet 5h
   window expires on schedule, so "the session went idle" does not explain it.
   Median normal 5h window life is 299.8 min.
5. There are **zero** `five_hour`/`limit:session` rows in
   `oauth_window_drop_log`, so no early re-open is hidden as a drop instead of
   a new window; the 294-transition census is complete.
6. Not correlated with our OAuth refresh: token TTL is 8 h and
   `anthropic_keys.updated_at == expires_at - 8h` for both active keys; the
   last refresh before the wipe was 16:48Z, 1 h 13 m earlier. Testable only for
   the September event — `updated_at` for the July key was overwritten on
   2026-07-20 — so n=1, and `updated_at` is also bumped by status/name/role
   writes, which weakens it further.
7. Not correlated with polling density (1 poll/14.7 min at one occurrence vs
   1 poll/2.3 min at another).

### Two claims retracted during review

- **The wipe is not atomic.** A scoped weekly counter dropped to 0% at
  **20:48:36Z**, 2 h 47 m after the 18:01 wipe, with polling running at 1 per
  2.2 min throughout. An earlier reading of this as simultaneous was wrong.
- **`seven_day` and `limit:weekly_all` are the same upstream counter recorded
  twice** (`_extract_window_observations` takes both the top-level object and
  the `limits[]` entry; the 2026-07-02 design says so explicitly). Their drop
  sets are byte-identical on both keys. Counting them as two independent
  signals was double-counting: 18:01 carried **two** independent signals — one
  weekly counter and one 5h window — not four.

### What remains unexplained

The weekly counter zeroes ahead of its declared `resets_at`, and the
model-scoped weekly counter zeroes separately hours later. The trigger is
unknown and **cannot be recovered retroactively** from what we store.

## Non-goals

- Re-recording the drop. `oauth_window_drop_log` works; it stays as-is.
- Per-segment cost accounting inside a window (max/cost before vs after a
  wipe). Useful, unrelated to causation, cheap to add later on top of this.
- Explaining the cause now. This design buys the ability to explain the *next*
  one.

## Design

### 1. Snapshot the raw upstream payload, deduplicated by content

New table:

```sql
CREATE TABLE IF NOT EXISTS oauth_usage_snapshot (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id         TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,   -- sha256 of the canonical form
    payload_json   TEXT NOT NULL,   -- raw upstream body, verbatim
    headers_json   TEXT NOT NULL,   -- selected upstream response headers
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ous_key_seen
    ON oauth_usage_snapshot(key_id, first_seen_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ous_key_hash_run
    ON oauth_usage_snapshot(key_id, payload_hash, first_seen_at);
```

**Write rule.** On each successful usage poll, compute the canonical hash. If
it equals the hash of the newest row for that key, `UPDATE` its `last_seen_at`
and `seen_count`. Otherwise `INSERT` a new row. Tracking `last_seen_at` is what
distinguishes "this state was still being served at 17:59" from "we stopped
polling at 17:40" — without it the row before a wipe proves nothing.

**Hook point.** `anthropic_proxy.py`, immediately beside the existing
`_record_window_observations(db, key.key_id, entry["usage"])` call (~line 3284),
where the httpx response `r` is still in scope. This matters: the `/_oauth_usage`
*handler* serves a shared 120 s-cached payload and injects per-caller
`smartproxy_*` limits, so snapshotting there would re-observe the cache with a
fresh timestamp and hash caller-specific data. Store `r.text` verbatim and a
whitelist of `r.headers` (`request-id`, `date`, `cf-ray`,
`anthropic-organization-id`, any `anthropic-ratelimit-*`).

**Canonicalisation, for hashing only.** Recursively truncate every `resets_at`
to the minute, then `json.dumps(sort_keys=True, separators=(",", ":"))`, then
sha256. Sub-second jitter in `resets_at` is documented upstream behaviour and
would otherwise make every poll a new row. Nothing is stripped from what is
*stored* — `extra_usage`, `used_dollars`/`limit_dollars`, `severity`,
`is_active`, and the codename buckets (`nimbus_quill`, `cinder_cove`,
`juniper_tide`, `tangelo`, `iguana_necktie`, `omelette_promotional`) are
retained. Those buckets carry `resets_at: null` and are silently dropped by
`_extract_window_observations` today; since the trigger is unknown, discarding
fields in advance is precisely the mistake that left us unable to answer this
time. Whole-minute `resets_at` drift will still produce a row; that is signal.

**Volume.** Utilization is integer percent and moves every few minutes under
load. At ~4400 polls/week/key, expect on the order of 1000–2500 distinct
payloads/week/key at ~1.5 KB, i.e. 2–4 MB/week/key. Retention is therefore
mandatory, not optional (§5).

### 2. Wipe detection — on event type, not on counting zeros

The rule considered first ("≥2 window kinds go to zero in one observation") is
wrong in both directions and must not be implemented:

- **False positives:** at every ordinary 5h boundary both `five_hour` and
  `limit:session` start a new row at 0; at the Thursday 11:00Z weekly boundary
  `seven_day`, `limit:weekly_all` and `limit:weekly_scoped:*` all go to 0
  together. It would have fired on essentially every boundary.
- **False negative:** after collapsing the duplicate pair, the 18:01 event
  carried exactly **one** logical weekly counter at zero. The rule would have
  missed the real event.

**Actual rule.** Collapse duplicate kinds to one logical counter
(`seven_day` ≡ `limit:weekly_all`). A wipe is an `utilization_drop` — the
existing branch in `record_oauth_window_observations` where the window row
already exists and `resets_at` did not move — on a **weekly-class** kind
(`seven_day`, `limit:weekly_*`) with `from_utilization > 0` and
`to_utilization == 0`. A `window_reset` (a *new* row) is never a wipe on its
own.

The `UTILIZATION_DROP_THRESHOLD_PP = 5.0` gate does not apply **to the poll
channel**: a wipe from 3% → 0 must still register, and a genuine 7-day counter
reaching exactly `0.0` inside one 2-minute poll is not natural decay. The 5 pp
threshold stays as-is for the general drop log, and the header channel needs a
floor of its own (§4).

Corroborating context is **recorded, not required** — the September and July
events all had it, but the Fable-scoped drop at 20:48 did not, and it is a real
event.

```sql
CREATE TABLE IF NOT EXISTS oauth_limit_wipe (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id                  TEXT NOT NULL,
    window_kind             TEXT NOT NULL,   -- logical kind, duplicates collapsed
    observed_at             TEXT NOT NULL,
    prev_seen_at            TEXT NOT NULL,
    from_utilization        REAL NOT NULL,
    resets_at_claimed       TEXT NOT NULL,
    hours_before_claimed    REAL,
    five_hour_rolled        INTEGER NOT NULL DEFAULT 0,  -- 5h window reset in the same observation
    five_hour_early_minutes REAL,                        -- vs the previous row's resets_at_raw
    source                  TEXT NOT NULL DEFAULT 'poll',-- 'poll' | 'headers'
    context_json            TEXT,             -- headers source: request-id, org id, model, proxy key, path
    snapshot_id             INTEGER,          -- oauth_usage_snapshot.id at the wipe (poll source)
    prev_snapshot_id        INTEGER           -- the state immediately before (poll source)
);
CREATE INDEX IF NOT EXISTS idx_olw_key ON oauth_limit_wipe(key_id, observed_at);
```

`five_hour_early_minutes` must be measured against the previous 5h row's
**`resets_at_raw`**, not its `resets_at`: a row keeps its first-observed
`resets_at` and absorbs up to ±120 min of upstream drift into `resets_at_raw`
alone, so measuring against the former puts the smallest observed value
(18.5 min) inside the noise band. Both 5h rows are available in the same call,
so this is computable at write time — but it is computed **before** the new row
supersedes the old one.

Snapshot linkage follows the existing `anthropic_key_events.snapshot_id`
pattern. To make it possible, `_record_window_observations` must compute one
`seen_at` after the HTTP call and pass it into both writes; today it passes
none and `record_oauth_window_observations` stamps its own `now`, so the
snapshot and the drop row would carry different timestamps for one observation.

### 3. Detect in memory, persist best-effort

Detection currently requires reading the previous state from the DB. With the
Postgres breaker open every operation raises `DbUnavailable` immediately, and
the broad `except` in `_record_window_observations` swallows it — so a wipe
during a database outage would be neither recorded nor alerted, which is
exactly when we would most want to know.

The pool keeps the last observation per `(key_id, logical_kind)` in memory and
detects from that. The alert fires from memory via the existing `_notify`,
throttled on signature `wipe:<key_id>:<window_kind>`. Database writes are
best-effort and split into their own `try`/`except`, so a snapshot INSERT
failure cannot suppress detection, alerting, or the drop-log write.

### 4. Response headers — the only request-granular channel

Every `/v1/messages` response carries `anthropic-ratelimit-unified-7d-utilization`,
`-7d-reset`, `-5h-reset`, `anthropic-organization-id` and `request-id`.
`_maybe_record_utilization` already parses these on every response, but
`record_rate_limit` deduplicates by `(credential_id, reset_at)` and
COALESCE-overwrites, so the history is lost; `anthropic-organization-id` is
stored nowhere at all.

This channel is denser than polling under load and is the only thing that can
say whether one of *our own* requests was the first to observe 0%, and the only
one producing a `request-id` that Anthropic support can act on. An organization
id that changes across a wipe would be a strong signal of a plan or org
migration.

Add: keep the last unified header values per key in memory; when
`-7d-utilization` transitions to 0 while `-7d-reset` is unchanged, write an
`oauth_limit_wipe` row with `source = 'headers'`, putting `request-id`,
organization id, model, proxy key and path into `context_json`. This reuses the
wipe table; `snapshot_id`/`prev_snapshot_id` are NULL for header-sourced rows,
and `window_kind` is `seven_day`.

The same poll- and header-sourced wipe will often be observed twice for one
underlying event. They are not deduplicated: they are independent observations
through different channels, and the gap between them is itself evidence about
which channel sees a wipe first.

Two guards this channel needs and the poll channel does not:

- **A floor.** The header value is a two-decimal *fraction* (`0.65`), so its
  resolution is a whole percentage point, and a key idling near 1% flickers
  between `0.01` and `0.00` from one response to the next. Without a floor
  every such flicker writes a wipe row. Require the previous reading to be at
  least `UTILIZATION_DROP_THRESHOLD_PP`; a wipe that small carries no forensic
  value anyway. (The value is also converted to percent before storage — the
  poll channel writes percent, and mixing `0.74` with `74.0` in one column
  would make the two sources incomparable.)
- **A latch.** Each response is handled in its own task, so responses reach the
  detector out of order and a late one reinstating a nonzero reading would
  re-arm the detector and duplicate the wipe. Once a window is reported wiped,
  latch it until the reset epoch moves.

### 5. Reconciliation from the drop log

Two writers feed `oauth_limit_wipe`, and they can diverge in one predictable
place: the detector runs off in-memory state, which is empty for the couple of
minutes after a restart, while the drop-log writer compares against the
database and has no such gap. A wipe landing in that window would reach
`oauth_window_drop_log` and nothing else.

`reconcile_limit_wipes_from_drops()` derives the missing rows: weekly-class
drops to exactly zero that have no `oauth_limit_wipe` row at the same
`(key_id, window_kind, observed_at)`. Both writers stamp the same observation
timestamp — `_record_window_observations` computes one `seen_at` and passes it
to both — so the identity match is exact.

For this to be a complete safety net the drop log must hold every wipe, so its
insert condition gains a second arm: a weekly counter reaching exactly zero is
recorded whatever its size, since a 3% → 0 wipe is still a wipe and the 5 pp
threshold would otherwise hide it. Other kinds keep the threshold unchanged.

Recovered rows carry `source='backfill'` — derived, with no payload snapshot
behind them, and not to be mistaken for detector output that has one. The
`limit:weekly_all` alias is dropped only where its `seven_day` primary exists
for that same instant, never unconditionally, or a key reporting solely the
alias would have its wipes discarded.

It runs hourly from `_usage_flush_loop`, next to the snapshot prune, so there
is no separate schedule to operate, and is exposed as
`python -m smart_proxy db backfill-wipes` to run it immediately after a deploy.
Being idempotent, it is also the repair path. Its first run recovers the four
events recorded before this table existed (three `seven_day`, one
`limit:weekly_scoped:Fable`), verified against production data.

Three details this needs to get right:

- **Reconciled wipes are alerted, but only recent ones.** A wipe recovered here
  is by definition one nobody saw live, so it is the one most worth a
  notification — but the first run recovers months of history, and a burst of
  alerts about events long past is noise. Only wipes newer than
  `_WIPE_ALERT_MAX_AGE_HOURS` reach `alert_limit_wipe`.
- **The 5h-roll match is one-sided.** A `five_hour` row born *after* the drop is
  a separate event, and treating it as the roll would also pick the wrong
  "previous" window for `five_hour_early_minutes`. This is reachable —
  `_build_oauth_usage_payload` has two independently cached call sites, so
  observations for one key can land a minute apart — and the live detector
  reports no roll in that case, so an unsigned match would make the backfilled
  field contradict the detector for identical circumstances.
- **The CLI always names the database it opened.** Settings read `.env` relative
  to the working directory, so running the documented command from anywhere but
  the repo root on a PostgreSQL host would otherwise fall through to SQLite,
  create an empty `smart-proxy.db`, and print an all-clear about production data it
  never touched. It prints the resolved backend (credentials redacted) and
  refuses to run against a SQLite path that does not exist.

**Cross-channel duplication is accepted, not prevented.** A single physical
wipe can produce a `headers` row and a `poll`-or-`backfill` row, because
reconciliation dedups on an exact `observed_at` and the header channel stamps
request time, unrelated to any poll. This is the same trade already made for
poll-versus-header duplicates in §4: they are independent observations, the gap
between them is evidence, and matching them across channels would need a fuzzy
time window that could just as easily suppress two genuinely separate events.

### 6. Retention, migrations, snapshot tooling

- Retention: hourly from the existing `_usage_flush_loop`, delete
  `oauth_usage_snapshot` rows older than 60 days that are not referenced by an
  `oauth_limit_wipe` row.
- Register both tables in `SNAPSHOT_TABLE_SPECS` and `SNAPSHOT_DELETE_ORDER`
  (`db.py`), in `_after_replace_snapshot`'s sequence-reset list
  (`db_postgres.py`), in the SQLite `MIGRATIONS` list, and add the matching
  Postgres migration. Use `TEXT` for the JSON columns — `JSONB` would break the
  `row_json`-based export path.

### 7. API and UI

`build_oauth_usage_history` applies `?kind=` to the `drops` block, so a wipe
would vanish under a filter, and the 5h early re-open is a `window_reset` that
never appears in `drops` at all. Wipes get their own top-level, unfiltered
section in the response. `WindowsView.svelte`, which today reads only
`max_utilization` and ignores the `drops` field entirely, renders a wipe list
grouped by key **name** (standby keys share a name and a subscription).

A window whose counter was wiped still shows its pre-wipe `max_utilization` as
if it had run its course; the view must mark it, and
`attribute_oauth_window_usage` keeps attributing tokens to a window the account
no longer counts. Flagging it in the UI is in scope; changing the attribution
model is not (see Non-goals).

## Testing

- `_canonical_usage_hash`: sub-second and whole-minute `resets_at` jitter; the
  second must change the hash, the first must not.
- Dedup: repeated identical payloads produce one row with a growing
  `seen_count`; a changed payload produces a second row.
- Wipe detector against the three real recorded events, and against synthetic
  ordinary 5h and weekly boundaries, which must produce **no** wipe.
- Duplicate-kind collapsing: `seven_day` + `limit:weekly_all` yield one wipe.
- Detection and alerting with the database unavailable.
- Header-sourced wipe with unchanged `-7d-reset`.

## Open questions

1. Retention of 60 days is a guess. One wipe every ~6 weeks means 60 days holds
   roughly one event; 180 days would hold three.
2. `is_active` semantics are unknown — the July fixture shows
   `weekly_all.is_active = false` with `percent = 1`, so it does not mean "has
   usage". Snapshot it; build no logic on it.
3. Whether a wipe should close the window row and synthesise a new one. Deferred
   with the rest of segment accounting.
