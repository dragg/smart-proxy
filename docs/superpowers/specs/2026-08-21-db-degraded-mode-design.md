# DB-degraded mode: keep serving with a dead PostgreSQL

**Date:** 2026-08-21 · **Status:** approved for implementation · **Source:** adversarial review of the single-connection Postgres design, all claims verified against code at commit `e569bca`.

**Requirement:** As an absolute last resort the proxy must keep serving even if the database is completely dead. An alert is expected in that case, but as long as requests can still be answered without traffic accounting and the like, they should be.

**Line numbers in this doc are as of `e569bca`.** Each slice shifts lines below it; re-grep the anchors (function names) rather than trusting absolute numbers after slice 1 lands.

---

## 1. What is already true (verified, do not re-build)

The `/v1/messages` happy path — valid token, upstream 200 — does **zero** DB operations:

| Step | Where | DB? |
|---|---|---|
| `check_auth` | `anthropic_proxy.py:337-344` | no — in-memory set |
| `KeyLimiter.check`/`add` | `key_limits.py:156-199` | no — dict lookups |
| `pick()` | `anthropic_proxy.py:497` (sync def) | no |
| `ensure_valid_token`, no refresh due | `anthropic_proxy.py:722-749` | no |
| `UsageTracker.record` | in-memory buffers, 60s flush (`_USAGE_FLUSH_INTERVAL`, `anthropic_proxy.py:3830`) | no |
| audit events | `_record_anthropic_event` swallows + alerts (`anthropic_proxy.py:1271-1330`) | best-effort |
| utilization recording | fire-and-forget task (`anthropic_proxy.py:2833-2840` → `_maybe_record_utilization:2360`, "Never raises") | best-effort |
| `api_key`-type keys (fallback role) | `anthropic_proxy.py:746-747` — returned directly, never refreshed | never |

What actually breaks the requirement today — the four gaps this spec closes:

1. **Permanent poisoning**: one connection at `db_postgres.py:74-78`, no reconnect, no `closed` check. A 30-second Postgres restart degrades the process until a service restart.
2. **Failure paths raise**: upstream 429/401/403 handling does unguarded DB writes → client-facing 500 via `_failure_alert_middleware` (`anthropic_proxy.py:1220-1246`) instead of failover.
3. **The refresh brick**: a refresh during a DB outage consumes the single-use rotated refresh token, fails to persist it (`logger.critical` only, no Telegram — `anthropic_proxy.py:945-954`), and bricks the key on the next restart or `pool.reload()`.
4. **Boot requires the DB**: `_on_startup` (`anthropic_proxy.py:4261-4278`) — out of scope (see Non-goals).

Also fixed here (slice 5): a **healthy-DB brick vector** — see §8.

---

## 2. Scope at a glance

| # | Slice | Commit | Delivers |
|---|---|---|---|
| 0 | Alert on double-persist failure | 1 | The most dangerous silent event becomes a page |
| 1 | Breaker + reconnect in `_PostgresConnectionAdapter` | 2 | Blips self-heal; outages fail fast; open/close alerts |
| 2 | Refresh-safety rule + `_reread_token_if_changed` guard | 3 | No token is ever consumed while the DB is down |
| 3 | Five failure-path writes become best-effort | 4 | Upstream 429/401/403 fail over instead of 500ing during an outage |
| 4 | Recovery reconciliation + `reload()` freshness guard | 5 | Post-recovery brick vectors closed; short blips heal retroactively |
| 5 | `autocommit=True` + explicit transactions | 6 | Cross-coroutine commit/rollback corruption killed, incl. the healthy-DB token-loss vector |

No schema migrations in any slice — the deploy-time "stop all writers" procedure is not needed.

---

## 3. Non-goals — decided, do not relitigate

- **Full connection pool (`psycopg_pool.AsyncConnectionPool`).** Dropped. The adapter's `commit()` (`db_postgres.py:47-52`) and the `execute→execute→commit` pattern across `db.py` (2454 lines, 26 `await self.db.commit()` sites) are semantically wrong on a pool — `commit()` would commit a *different connection's* empty transaction. A pool strictly requires slice 5 first, is the largest item considered, and delivers none of the requirement: head-of-line blocking barely touches the request path (§1). Revisit only if, after slice 5, flush/refresh-persist latency is *measured* to matter.
- **Boot snapshot cache on disk.** Deferred. `SNAPSHOT_TABLE_SPECS`/`replace_snapshot` (`db.py:522, 2424-2440`) are a DB-to-DB migration/test tool (delete-all + reinsert; only `tests/` call them) — not reusable as a cache. A bespoke JSON file would work, but the scenario it covers is "deploy or crash *during* the DB outage"; systemd keeps the process alive otherwise. Decide after slices 1-4 have run in production.
- **Rotated-token disk journal.** Deferred, not dropped. It would remove the ~8h ceiling on degraded serving (refresh during outage, persist to disk, replay on recovery). Small (~60 lines) but new secret-bearing state on disk. Decide after seeing whether the slice-2 refuse-window is ever actually hit in production.
- **Usage re-merge on failed flush.** Dropped. `UsageTracker.flush` swaps buffers out before the DB write (`usage.py:685-696`), so a failed flush drops that minute of usage. The user explicitly accepted losing accounting during an outage, and unbounded re-buffering during a long outage is its own memory risk. The 60s flush alert (`anthropic_proxy.py:3897-3908`) already reports each loss.

---

## 4. Slice 0 — alert the double-persist failure (commit 1)

**Change:** `anthropic_proxy.py:945-954`, inside `_refresh_locked`. Both `except` blocks around `update_anthropic_oauth_tokens` currently do `logger.critical` only. Add `_alert_failure(source="rotated token persist", exc=exc)` in each (bind `except Exception as exc:`). `_alert_failure` never raises by contract (`anthropic_proxy.py:1117-1130`) and throttles per signature via `AlertThrottle` (`notifier.py:21`).

**Why first:** this is the exact event that bricks a key on restart, and today it is journald-only.

**Tests (write failing first):**
- `tests/test_proxy_failure_alerts.py` (extend): a refresh whose `update_anthropic_oauth_tokens` raises twice fires a notifier message containing the key id, and the refresh still returns the new token (serving continues). Pure fakes — fake `Database` raising from `update_anthropic_oauth_tokens`, fake notifier via the `_ALERT_FALLBACK` pattern already used in that file. No Postgres needed.

**Checkpoint before slice 1:** full suite green (531 tests), deploy, confirm a normal refresh in prod logs `Refreshed OAuth token` with no new alerts.

---

## 5. Slice 1 — circuit breaker + reconnect in `_PostgresConnectionAdapter` (commit 2)

All changes in `src/smart_proxy/db_postgres.py` plus one exception class and one hook in `src/smart_proxy/db.py`. This slice **subsumes** the earlier "(a) reconnect" idea — do not ship reconnect separately.

### 5.1 `DbUnavailable` contract

Define in `db.py` (importable by both `db_postgres.py` and `anthropic_proxy.py` without cycles):

```python
class DbUnavailable(Exception):
    """The database is unreachable and the operation was not attempted
    (breaker open) or died with the connection. Chains the original error."""
```

- **Raised by:** `_PostgresConnectionAdapter.execute/executemany/commit` — (a) immediately when the breaker is open and the probe interval has not elapsed; (b) wrapping the original exception when an operation dies a *connection-level* death (§5.3).
- **Must catch it:** every best-effort site — already-broad `except Exception` blocks cover it (`_record_anthropic_event:1305`, the flush loop `:3897`, all background loops, and the slice-3 guards). No site should catch `DbUnavailable` *more narrowly than* `Exception` unless it needs to distinguish "DB down" from "SQL bug" (only `_refresh_locked` does, via `is_available()`, not via catching).
- **Must NOT catch it (let it propagate):** `_on_startup` (dead DB at boot must still fail the boot — boot cache is a non-goal); `db_migrations.py` and every CLI entry point (abort loudly); dashboard read handlers (a 500 on `/api/usage` during an outage is correct and visible). The slice-4 reconciler logs per-key failures but must not silently drop them.
- **Not a subclass of `psycopg.Error`** — deliberately, so generic `except psycopg.Error` handlers (there are none today; keep it that way) never confuse the two.

### 5.2 Breaker states and transitions

Two states, merged with reconnect — the probe *is* a reconnect attempt:

```
CLOSED --connection-level failure--> OPEN (alert once, record t_open)
OPEN   --op arrives, now < last_probe + PROBE_INTERVAL--> raise DbUnavailable (fast, no socket I/O)
OPEN   --op arrives, probe due--> attempt reconnect (this is HALF-OPEN)
         success --> CLOSED (alert recovery with outage duration; fire on_recovered hook, §7)
                     then run the op normally
         failure --> stay OPEN, reset probe timer, raise DbUnavailable
```

- `PROBE_INTERVAL = 5.0` seconds (constant in `db_postgres.py`; see Open decisions).
- Threshold is 1: a single connection-level failure opens the breaker. Safe because of the §5.3 classifier — a healthy connection does not produce `broken/closed` states spuriously.
- Reconnect recreates the connection exactly as `connect()` does (`db_postgres.py:74-78`) — **must pass `row_factory=dict_row`** or every subsequent read silently changes shape.
- Reconnect and state transitions run under a dedicated `asyncio.Lock` in the adapter: concurrent coroutines hitting an open breaker must not stampede reconnects; losers of the lock re-check state after acquiring and either proceed (now CLOSED) or raise `DbUnavailable`.
- Expose `is_available() -> bool` (breaker CLOSED) on the adapter, surfaced as `Database.is_available()`; the base (SQLite) `Database` returns `True` always. This is what `_refresh_locked` consults (§6) — a cheap state read, no I/O.
- Alerting: the adapter has no notifier. Add an optional `on_state_change(state: str, exc, outage_seconds: float | None)` callback attribute on `PostgresDatabase`, set in `_on_startup` after `_build_notifier` (`anthropic_proxy.py:4278-4283`) to a closure that calls `_alert_failure` / `pool._notify`. Open alert once per outage; close alert once, carrying the duration. Callback failures are swallowed (same posture as `_alert_failure`).

### 5.3 Connection-level vs SQL error — the classifier (trap #1)

**Do not classify by exception type alone.** Verified against installed psycopg 3.3.3: `QueryCanceled` (statement_timeout, admin cancel) and `AdminShutdown` are *both* subclasses of `psycopg.OperationalError`. Class-based classification would open the breaker on a mere cancelled statement and mute the DB while it is healthy.

Correct rule, after catching any exception from an operation:

```python
except psycopg.Error as exc:
    if self._conn.closed or self._conn.broken:      # connection actually died
        self._open_breaker(exc)
        raise DbUnavailable(...) from exc
    await self._rollback_on_error()                  # SQL-level: today's path (db_postgres.py:21-26)
    raise
```

Both `closed` and `broken` exist on `psycopg.AsyncConnection` (verified). `InFailedSqlTransaction` is an `InternalError` subclass and never trips `broken` — SQL-level, keeps today's rollback path. A **failed rollback** inside `_rollback_on_error` is itself evidence of a dead connection: after the rollback attempt, re-check `closed/broken` and open the breaker if set (today's code logs and moves on — `db_postgres.py:25-26` — leaving a poisoned connection).

### 5.4 Trap #2 — never blind-retry the failed statement

On reconnect, the in-flight statement's fate is unknown (it may have committed server-side before the connection died). The adapter must **reconnect-then-raise**, never reconnect-and-retry: the caller's existing error handling (persist retry at `anthropic_proxy.py:946`, best-effort guards, flush loop) decides what to do. This is what makes the slice-0 "retry once" meaningful: the retry arrives as a *new* operation and gets the reconnected connection.

### 5.5 First-failure latency

The op that discovers a dead DB still eats the TCP/connect timeout. Mitigate on the reconnect path by passing `connect_timeout=5` to `AsyncConnection.connect()` unless the DSN already sets one (see Open decisions). Ops after breaker-open fail in microseconds.

### 5.6 Tests

New file `tests/test_db_postgres_breaker.py`:
- **Pure fakes** (adapter takes `conn: Any`, `db_postgres.py:18` — inject a fake with scriptable `execute/rollback/closed/broken`):
  1. connection-level failure (`OperationalError`, `broken=True`) → `DbUnavailable` raised, breaker open, `on_state_change("open", ...)` fired once.
  2. second op within probe interval → `DbUnavailable` without touching the fake conn (assert no `execute` call).
  3. op after probe interval → reconnect factory called; on success the op runs on the new conn, `on_state_change("closed", ...)` fired with duration; `is_available()` back to True.
  4. probe failure → still open, timer reset, no duplicate open-alert.
  5. `QueryCanceled` with `broken=False` → **no** breaker trip, rollback called, original exception propagates (pins the classifier trap).
  6. failed rollback with `broken=True` after → breaker opens (pins §5.3 last paragraph).
  7. concurrent ops during reconnect → exactly one reconnect attempt (the stampede lock).
- **Real Postgres** (gate on `db_test_utils.get_test_database_url()`, skip when empty, like existing `*_db.py` suites): open a `PostgresDatabase`, kill its backend via a second connection (`SELECT pg_terminate_backend(pid)`), assert next op raises `DbUnavailable`, then after the probe interval the same `Database` object serves queries again with dict rows.

**Checkpoint before slice 2:** suite green; deploy; run the `pg_terminate_backend` drill from §10.3 in prod and watch the open→closed alert pair arrive.

---

## 6. Slice 2 — refresh-safety rule (commit 3)

**The rule:** never call `/token` while the DB is unavailable. Consuming the single-use rotated refresh token without being able to persist it converts a temporary DB outage into a permanent key loss (manual re-auth). Refusing converts it into temporary key unavailability starting at token expiry. The asymmetry decides.

### 6.1 The branch, exactly

In `_refresh_locked` (`anthropic_proxy.py:762`), insert **after** the `_refresh_dead` latch check (`:792-794`) and **before** the `refresh_attempt` audit write (`:795`) — no point recording an attempt we refuse, and the write would only fail-swallow-alert anyway:

```python
if not self._db.is_available():
    if token_valid:                       # computed at :778-784, floor 30s
        self._defer(key, now_mono)        # existing backoff, :563-567
        self._fire_alert(category="transient", key=key,
                         code="db_unavailable", valid_ms_left=...)
        return key.access_token
    return self._REFRESH_BLOCKED          # NEVER None while the DB is down
```

- `token_valid` branch mirrors the three existing "transient failure, token still valid" branches — this is a fourth member of an established pattern, not new machinery.
- **`_REFRESH_BLOCKED`, never `None`:** `None` makes every caller deactivate — serving path `:2537`, smoke `:4038`, keepwarm `:4140` — which is itself a DB write that will fail, and it bans a key that becomes usable the moment the DB returns. `_REFRESH_BLOCKED` already means "try the next key, do not deactivate" (`:742`). The key stays pickable; each request re-checks cheaply (breaker state read under the already-held `_refresh_lock`).
- Known residual window, accepted: `is_available()` can be true and the DB can die *between* the check and the persist. Slice 0 alerts it, slice 4 heals it on recovery, the deferred journal would close it entirely.

### 6.2 `_reread_token_if_changed` guard

`anthropic_proxy.py:576-590` does an unguarded `get_anthropic_key`. Wrap in `try/except Exception: return False`. Returning False in the fatal-4xx path latches the key (`_mark_refresh_dead`) — correct even with the DB down: `invalid_grant` proves the in-memory refresh token is dead regardless of DB state. (The staleness bug in this function is fixed in slice 4, not here.)

### 6.3 Degraded-serving windows (for the alert text and the runbook)

| Constant | Value | Where |
|---|---|---|
| Serving-path refresh buffer | 5 min | `_REFRESH_BUFFER_MS`, `anthropic_proxy.py:75` |
| Serve floor | 30 s | `_TOKEN_VALID_FLOOR_MS`, `:84` |
| Access-token lifetime | ~8 h empirical | `expires_in` from response, `anthropic_oauth.py:301-302` |
| Standby keep-warm buffer | 120 min | `create_app` default, `anthropic_proxy.py:4354` |

Per OAuth key, degraded serving lasts its *remaining validity*: worst ~4.5 min, expectation ~4 h. Standbys hold ≥2 h. `api_key` fallback keys serve indefinitely.

### 6.4 Tests

Extend `tests/test_anthropic_oauth_refresh.py` (pure fakes; the file already fakes `Database` + httpx):
1. DB unavailable + token valid → old access token returned, **no** `/token` HTTP call made (assert on the fake transport), refresh deferred (backoff set), transient alert fired.
2. DB unavailable + token expired → `_REFRESH_BLOCKED`, no `/token` call, key **not** latched, **not** deactivated.
3. DB recovers → next `ensure_valid_token` refreshes normally (rule leaves no residue).
4. Fatal `invalid_grant` while DB down → `_reread_token_if_changed` swallows, key latched, no crash.
5. Serving path: with two keys, one DB-blocked-expired, the handler serves via the second key (extend `tests/test_anthropic_proxy_oauth_messages.py`).

Fake `Database` needs an `is_available()` knob — add it to the shared fake in whichever helper these files use; base class already returns True so untouched tests keep passing.

**Checkpoint before slice 3:** suite green; deploy; grep journal for `refresh` behaviour unchanged in normal operation (no `db_unavailable` deferrals while the DB is healthy).

---

## 7. Slice 3 — five failure-path writes become best-effort (commit 4)

All five already mutate in-memory state **before** the DB write, so wrapping the write changes durability only, not behaviour:

| Site | Memory-first at | DB write at | Change |
|---|---|---|---|
| `AnthropicKeyPool.deactivate` | `:640-642` (`_banned.add`, `status`) | `:649` `set_anthropic_key_status` | wrap write in `try/except Exception` → `logger.warning` + `_alert_failure(source="key deactivation persist")` |
| `mark_low_balance` (`:684`) | `:700-701` | `:702` | same pattern |
| `promote_to_primary` (`:667`) | `:674` (`key.role = "primary"`, commented "memory-first") | `:675` `set_anthropic_key_role` | same pattern |
| `note_fallback_serve` (`:445`) | throttle at `:457-460` | `:466` `record_anthropic_key_event` | same pattern (audit-only write) |
| 429 rate-limit record in `_classify_unsuccessful_response` (`:2068`) | n/a | `:2242-2251` `db.record_rate_limit` | mirror the guarded twin at `:2377-2391` (warning + `_alert_failure`) |

Catch `Exception`, not `DbUnavailable` — matches project style (`_record_anthropic_event:1305`) and a SQL bug should not 500 these paths either; the alert surfaces both kinds.

Accepted looseness (do not fix here): a failed `promote_to_primary` persist followed by a later `reload()` reverts the role in memory; the next serve re-promotes (idempotent by design, `:670-672`). Reconciliation of *roles* is out of scope; only tokens are reconciled in slice 4.

### Tests

New `tests/test_anthropic_pool_best_effort_writes.py` (pure fakes — fake `Database` whose write methods raise):
1. per site: the method completes, in-memory state is correct (banned/status/role/throttle), one alert fired, nothing propagates.
2. End-to-end: handler test where upstream returns 429 and `record_rate_limit` raises → client gets the 429 failover behaviour, **not** a 500 (extend `tests/test_anthropic_proxy_oauth_messages.py` or `test_proxy_classify_wiring.py`, whichever already drives `_classify_unsuccessful_response`).
3. End-to-end: upstream 401 with dead DB → key banned in memory, next attempt uses the other key, response is not a 500.

**Checkpoint before slice 4:** suite green; deploy; prod drill §10.3 again — this time also fire one request *during* the ~5s outage and confirm it is served or failed over, not 500.

---

## 8. Slice 4 — recovery reconciliation + freshness guards (commit 5)

Closes the two post-recovery brick vectors: (i) `pool.reload()` replacing a fresher in-memory token with a stale DB row; (ii) nothing re-persisting an in-memory token after the DB returns.

### 8.1 Freshness rule (shared by all three changes)

Memory wins iff `mem.expires_at` and `row.expires_at` are both set and `mem.expires_at > row.expires_at`. Anthropic rotates on every refresh, so a strictly newer expiry identifies the strictly newer (and only living) refresh token.

### 8.2 Changes

1. **`reload()` (`anthropic_proxy.py:289-336`):** before `self._keys = [...]` (`:293`), capture `old_by_id = {k.key_id: k for k in self._keys}`. After building the new list, for each new key where the old one is fresher (§8.1), copy `access_token/refresh_token/expires_at` from the old object, `logger.warning("DB row staler than memory for key %s — keeping in-memory tokens", ...)`, and remember the key for re-persist. Attempt `update_anthropic_oauth_tokens` for it best-effort (guarded — the DB may still be flaky).
2. **`_reread_token_if_changed` (`:576-590`):** adopt the DB row only when the *row* is fresher (§8.1 reversed). Today it adopts on any difference, which after a failed persist means adopting the dead token. With the guard, the legit rescue (another writer rotated and persisted → row expiry is newer) still works; the stale-row case returns False.
3. **Reconciler:** new `AnthropicKeyPool.reconcile_tokens()` — for each in-memory oauth key, `get_anthropic_key`; where memory is fresher, `update_anthropic_oauth_tokens(..., audit_event_type="recovery_repersist", audit_decision="update_tokens", audit_source="db_recovery")`. Per-key failures: log + alert, continue with the rest. Wire it to the slice-1 `on_state_change("closed", ...)` hook in `_on_startup` via a fire-and-forget task (use the existing `_watch_background_task` / `pool._alert_tasks` strong-ref pattern, `:1247`). Count and include "reconciled N key(s)" in the recovery alert.

### 8.3 Tests

Extend `tests/test_anthropic_oauth_refresh.py` + a new `tests/test_anthropic_pool_reconcile.py` (pure fakes):
1. `reload()` with a staler row → in-memory tokens survive, re-persist attempted; with a fresher row → row wins (pins both directions).
2. `_reread_token_if_changed`: row fresher → adopt + True; row staler → no adopt + False (this is the regression test for the dead-token-adoption bug).
3. `reconcile_tokens`: two keys, memory fresher on one → exactly one `update_anthropic_oauth_tokens` with `audit_event_type="recovery_repersist"`; a raising key does not stop the other.
4. Integration: refresh succeeds → persist fails (fake DB down) → DB "recovers" → reconcile → fake DB holds the rotated token; then `reload()` returns the same token (the full incident replay, end to end).
- **Real Postgres** (extend `tests/test_anthropic_db_contract.py` or a `*_db.py` sibling): `update_anthropic_oauth_tokens` round-trip of the reconciler's audit event, so the Postgres schema accepts `recovery_repersist`.

**Checkpoint before slice 5:** suite green; deploy; full-outage drill §10.4 once, off-peak: stop Postgres ~2 min, confirm serving continues, restart, confirm recovery alert with reconcile count.

---

## 9. Slice 5 — `autocommit=True` + explicit transactions (commit 6, correctness debt)

**Why this is not optional cleanliness.** With one shared implicit transaction (autocommit is never set; psycopg defaults False), the adapter's rollback-on-any-failure (`db_postgres.py:21-26`) **discards other coroutines' uncommitted statements**. Concrete healthy-DB brick: coroutine A runs `update_anthropic_oauth_tokens` (`db.py:1797-1858` — snapshot INSERT, token UPDATE, event INSERT, single `commit()` at `:1858`); a concurrent dashboard query fails between A's UPDATE and A's commit; the failing query's rollback discards A's UPDATE; A's `commit()` then commits an empty transaction **with no error anywhere**. DB keeps the dead rotated refresh token → brick on next restart. Same incident class as 2026-07-20, reachable with a perfectly healthy DB.

### 9.1 Changes

1. `PostgresDatabase.connect` (`db_postgres.py:64-79`): pass `autocommit=True`.
2. Adapter `commit()` becomes a no-op under autocommit (keep the method; 26 call sites in `db.py` keep working unchanged — removing them is optional follow-up, not this commit).
3. New `Database.transaction()` async context manager:
   - Postgres: acquire the adapter-wide tx `asyncio.Lock`, then `async with conn.transaction():`.
   - SQLite base class: acquire the same lock, passthrough, `commit()` on exit (today's semantics).
4. **The lock covers every operation, not just transactions** (trap): with an explicit transaction open on the *shared* connection, any interleaved bare statement from another coroutine **joins that transaction** — psycopg cannot prevent it. So `execute`/`executemany` also acquire the tx lock. psycopg already serialises per-statement on its internal `ALock` (`connection_async.py:81`), so added contention is negligible.
5. **Reentrancy trap:** `db.py` methods called *inside* a `transaction()` block would deadlock acquiring the non-reentrant lock. Record the owner task (`asyncio.current_task()`) when `transaction()` acquires; `execute`/`executemany` skip acquiring when the current task is the owner. Pin this with a dedicated test.
6. Wrap the multi-statement units in `transaction()` and drop their per-statement `commit(..., commit=False)` plumbing where it becomes redundant:
   - `update_anthropic_oauth_tokens` (`db.py:1797`)
   - `set_anthropic_key_status` (`:1863`), `set_anthropic_key_role` (`:1980`), `insert_anthropic_key` (`:1731`)
   - flush batches: `upsert_usage_batch` (`:1263`), `upsert_usage_kind_batch` (`:1280`), `upsert_usage_session_batch` (`:1293`), `upsert_usage_hourly_batch` (`:1307`) — keeps a batch atomic; under bare autocommit `executemany` would half-apply increment upserts on failure
   - `record_oauth_window_observations` (`:2155`), `attribute_oauth_window_usage` (`:2297`)
7. **SAVEPOINT rewrites** (savepoints require a transaction; under autocommit they raise): `apply_migration` (`db_postgres.py:98-118`) and `replace_snapshot` (`db.py:2424-2440`) replace the savepoint dance with `async with self.transaction():` — identical semantics, less code. Both are deploy-time/test-only paths.
8. Breaker interaction: a connection-level death inside `transaction()` must release the tx lock (context manager unwind does this) and surface as `DbUnavailable` per slice 1.

### 9.2 Tests

- Pure fakes: reentrancy (a `db.py` method using `execute` inside `transaction()` completes without deadlock); lock exclusivity (two concurrent `transaction()` blocks serialise).
- **Real Postgres** (`tests/test_db_postgres_autocommit_db.py`, gated like the other `*_db.py`):
  1. the §9 vector as a regression test: task A inside `update_anthropic_oauth_tokens` with an injected pause; task B issues a failing statement + rollback; assert A's token UPDATE **survives** (fails on `e569bca`, passes after).
  2. a failed statement no longer poisons subsequent statements (no `InFailedSqlTransaction` ever surfaces).
  3. batch upsert atomicity: a poisoned row in an `executemany` batch leaves zero rows applied.
  4. migration runner + `replace_snapshot` round-trip still pass (existing `test_db_snapshot_roundtrip.py` and migration tests are the guard — they must run against Postgres in this checkpoint, i.e. with `TEST_DATABASE_URL` set).

**Checkpoint:** full suite green **with `TEST_DATABASE_URL` set** (SQLite-only green is not sufficient for this slice); deploy; watch one refresh cycle and one dashboard-heavy period in the journal for anomalies.

---

## 10. How to verify in production

Unit is `smart-proxy` (see the README's Deployment section).

### 10.1 Journal signatures to watch

```
journalctl -u smart-proxy -f
```

| Event | Expect after this work |
|---|---|
| Breaker opens | one `PostgreSQL connection lost — degraded mode` warning (exact message fixed in slice 1) + one Telegram alert; then *fast* `DbUnavailable` failures, no TCP-timeout stalls |
| Refresh during outage | `refresh deferred (db_unavailable)` info lines; **never** `rotated token not persisted` — if that critical appears at all, slice 0 guarantees a Telegram page |
| Requests during outage | `>>> POST /v1/messages` / `<<< ... status=200` continue; upstream 429/401 lines show failover, no `Internal Server Error` |
| Usage | `Usage flush error` warning + alert each 60s (expected, accepted loss) |
| Recovery | one `PostgreSQL reconnected after Ns` + `reconciled N key(s)` + Telegram recovery alert |
| Steady state (healthy DB) | zero `db_unavailable` deferrals; `Flushed N usage rows` each minute |

### 10.2 Before any drill

Check refresh-due times: `/_oauth_usage` includes human refresh-due (`_format_refresh_due_in_human`). Run drills only when no key is due within ~30 min, and prefer the off-peak window. (After slice 2 this is belt-and-braces — the rule protects the tokens — but before slice 2 has landed it is the *only* protection.)

### 10.3 Small drill — single connection kill (after slice 1)

From `psql` as superuser: `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='' AND usename='<proxy user>' AND pid <> pg_backend_pid();` — expect one open/closed alert pair within ~5-10s and no service restart. This tests reconnect without a real outage.

### 10.4 Full drill — dead DB (only after slices 1-3; slice 4 makes it boring)

Off-peak: `systemctl stop postgresql` → send 2-3 real requests through the proxy (must serve), watch §10.1 signatures for ~2 min → `systemctl start postgresql` → recovery alert + reconcile count → next morning, confirm no key latched (`/_oauth_usage`, dashboard) and refreshes proceeding normally. **Do not run this drill before slice 2 is deployed** — a refresh coming due mid-drill would consume a token it cannot persist.

---

## 11. Open decisions — pick before or during implementation

1. **`PROBE_INTERVAL`** — spec says 5s. Cheaper recovery detection vs. hammering a struggling server. 5s is fine; flagging in case Postgres restarts are routinely slower.
2. **`connect_timeout`** — spec says pass 5s on reconnect unless the production DSN already sets it. **Check the prod `DATABASE_URL` first**; if it sets a timeout, honour it and skip the override.
3. **Reconciler audit `event_type`** — spec says the new value `recovery_repersist`. Alternative: reuse `refresh_succeeded` (no schema impact either way — `event_type` is free text; dashboards/queries filtering by type will simply show a new row kind). Genuine naming choice; spec's pick keeps the incident trail honest.
4. **Role reconciliation** — deliberately out of scope (§7). If a real outage shows annoying re-promotion churn, add roles to the reconciler later.
5. **SQLite path in slice 5** — spec serialises SQLite through the same tx lock for uniformity. Harmless (dev/test only), but if any test times out on it, scoping the lock to Postgres alone is acceptable.
6. **Alert loudness of breaker-open** — spec routes it through `_alert_failure` (throttled Telegram). If the DB dying should page harder than ordinary failures, give it its own unthrottled `pool._notify` message instead — user's call on channel discipline.
