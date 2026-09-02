# Primary/standby Anthropic OAuth keys with warm failover

**Date:** 2026-07-21
**Components:** `src/smart_proxy/db.py`, `src/smart_proxy/db_migrations.py` (schema + role accessors),
`src/smart_proxy/anthropic_proxy.py` (`_AnthropicKey`, pool pick/promotion/keep-warm, smoke pass, usage
poller, deactivate/low_balance), `src/smart_proxy/dashboard_api.py` + `web/src/views/AnthropicView.svelte`
(role UI)
**Status:** design (revised after Fable review)
**Builds on:** the OAuth refresh durability fix merged 2026-07-21 (`ensure_valid_token` state machine).

## Problem / intent

On a single Anthropic subscription, keep a **second OAuth key dormant as a hot standby** that never
serves traffic (no inference/usage-tracking footprint, no per-key prompt-cache state) but is kept
credential-alive, and is promoted to serve automatically the moment the active (primary) key dies
from a refresh failure. This protects against the residual unrecoverable failure mode (a truly lost
`/token` response bricking a key) and account/session hiccups.

Constraints: **same subscription = shared quota** (a standby adds credential resilience, not
throughput; failing over on a rate-limit is pointless), and **both existing "touch every key"
paths make a footprint** — the daily smoke pass sends a real `/v1/messages` inference request per
key, and the `oauth_usage` poller (`_build_oauth_usage_payload`, hit by `/_oauth_usage`, which the
status line polls every ~60s, and the dashboard) refreshes+activates every key and GETs
`/api/oauth/usage`. Both must exclude the standby from their footprinted work.

## Decisions (confirmed)

1. **Failover trigger:** standby serves only when *no primary is alive*. A primary that is merely
   cooled (rate-limit / refresh-backoff) or `low_balance` still counts as alive → standby stays
   dormant. Only a **deactivated** (dead refresh token) primary triggers failover.
2. **Promotion:** auto-promote standby → primary the first time it *actually serves*.
3. **Keep-warm:** refresh-only (no activation, no inference), on the existing daily smoke windows.

## Non-goals

Multi-subscription throughput; automatic re-auth of a bricked key; a formal >2 tier; measuring the
unused-refresh-token TTL (the Deactivated column already surfaces it).

## Design

### 1. Data model — `role` column

Add `role TEXT NOT NULL DEFAULT 'primary'` to `anthropic_keys`, values `'primary' | 'standby'`.

- SQLite: add to the `CREATE TABLE anthropic_keys` schema (`db.py:139`) for fresh DBs **and** append
  `ALTER TABLE anthropic_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'primary'` to the `MIGRATIONS`
  list (`db.py:272`). The `_run_migrations` try/except loop (`db.py:826-831`) makes the duplicate-
  column re-run idempotent.
- Postgres: append a `POSTGRES_MIGRATIONS` entry with a **globally unique name** (the tuple already
  has duplicate `0002`/`0003` prefixes — pick e.g. `0006_anthropic_keys_role`) running
  `ALTER TABLE anthropic_keys ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'primary'`.
- Additive, non-PK-locking — no writer-stop needed. Existing rows default to `primary`.
- `SELECT *` accessors carry `role` through. `_AnthropicKey` gains `role: str = "primary"`;
  `_anthropic_key_from_row` sets `role=row.get("role") or "primary"` (covers code-before-migration).
- New DB methods: `set_anthropic_key_role(key_id, role, *, audit_...)` (writes `role`, records a
  `role_change` event). `insert_anthropic_key` gains optional `role="primary"` — **edit both the
  INSERT column list and the VALUES tuple** (`db.py:1563-1574`).
- `_api_anthropic_keys` payload gains `"role"`.

### 2. Failover in `pick()`

Define alive by **status**, so the answer is identical before and after a `reload()`:

```
primary_alive = any(k.role == "primary" and k.status != "inactive" for k in self._keys)
```

`low_balance` and cooled primaries have `status != "inactive"` → alive → no failover (matches
decision 1: shared billing/quota, so a standby wouldn't help). Only a deactivated primary
(`status == "inactive"`) is not alive.

To make this hold **mid-session** (before the next reload), the in-memory status must track the DB:
- `deactivate()` sets `key.status = "inactive"` in memory (in addition to `_banned` + DB write).
- `mark_low_balance()` sets `key.status = "low_balance"` in memory.
(Post-reload, a deactivated primary is simply absent from `_keys` since `get_active_anthropic_keys`
loads only `active`/`low_balance`; a low_balance primary reloads with `status="low_balance"` → still
alive. Both states agree.)

`pick()` changes:
- Restrict the normal sticky selection to `role == "primary"` keys.
- The api_key→oauth redirect (`_find_pickable_oauth_index`, `pick():289-293`) **must be restricted to
  the currently-eligible tier** — otherwise an api_key primary could redirect to an oauth standby
  while a primary is alive. Add a `role` filter (or an `allowed_roles` arg) to that scan.
- Only if `not primary_alive` do standbys become eligible (sticky selection + oauth-redirect over
  `role == "standby"` keys).

`_is_pickable` is unchanged (banned/low_balance/cooldown gates still apply within a tier).

**`next_available_in` mirrors the tiering.** It feeds the `retry-after` the handler returns when
`pick()` yields None. Role-blind, a dormant standby's zero cooldown makes it return 0; in the handler
`retry_after == 0` takes the **503 "No available Anthropic keys"** branch (`anthropic_proxy.py:1752-
1786`) — a misleading terminal 5xx during a mere primary cooldown, instead of a 429 with an accurate
retry-after. Fix: `next_available_in` considers only the eligible tier (primaries while
`primary_alive`, else standbys), so during a primary cooldown it returns the primary's remaining
cooldown. (`available` at `:643` stays informational; optionally exclude dormant standbys to avoid
ops confusion in `/api/reload`'s `active` count.)

### 3. Promotion

`async promote_to_primary(self, key, *, audit_op_id, audit_source, ...)`:
- `if key.role != "standby": return` (idempotent guard).
- Set `key.role = "primary"` in memory **before the first `await`** (closes the guard's window on the
  single event loop), then `await db.set_anthropic_key_role(key.key_id, "primary", ...)` recording a
  `role_change` event with `decision="promote"`.

Trigger — pinned: in the proxy request handler, **after `ensure_valid_token` returns a valid token
for the picked key and before `client.send`**:
`if key.role == "standby": await pool.promote_to_primary(key, audit_source="proxy_request", ...)`.
This promotes only a standby that is actually about to serve (not one that then returns
`None`/`_REFRESH_BLOCKED`), and only via the proxy path (never smoke/usage). A `reload()` that
interleaves between the DB write and its own `_keys` swap can resurrect a `role="standby"` in-memory
object → at most one duplicate promote + `role_change` event, converging on the next reload —
acceptable, not "serialized".

### 4. Keep-warm — refresh-only; standby excluded from both footprinted paths

Thread `activate: bool = True` through `ensure_valid_token(...)` → `_refresh_locked(...)`; the only
change is gating the success-path activation warmup with `if activate:`. `_refresh_oauth_token` hits
only `/token`, so `activate=False` is genuinely footprint-free (no activation, no inference). All
durability/keep-alive/classification/shield logic is reused unchanged.

- **Smoke pass (`_run_oauth_smoke_pass`)** per key:
  - **primary:** unchanged — `ensure_valid_token(..., activate=True, audit_source="scheduled_smoke")`
    then the `/v1/messages` smoke request.
  - **standby:** `ensure_valid_token(..., activate=False, audit_source="standby_keepwarm")`, then
    **skip** `_build_oauth_smoke_request`/`client.send`. Preserve the existing
    `was_expired and key not in pool._keys → should_reload` bookkeeping (`:3100-3101`) — don't drop
    it with a naive `continue`. On `None`/`_REFRESH_BLOCKED`, behave as today (deactivate / skip).
- **Usage poller (`_build_oauth_usage_payload`)**: **skip `role == "standby"` rows entirely** — no
  `ensure_valid_token`, no `/api/oauth/usage` GET. Rationale: it otherwise refreshes+activates the
  standby several times a day (defeating "zero activation") and double-records identical account
  windows (same subscription) under two key ids. The standby's usage equals the primary's, so its
  absence from the usage view is correct, not a gap.

Result: a dormant standby is touched only by the smoke pass, refresh-only — its ~8h access token is
usually expired by the window, so its refresh token rotates and stays alive with zero inference,
quota, activation, or prompt-cache footprint. If a standby's refresh genuinely dies (truly-expired +
auth-fatal), the smoke pass deactivates it like any key (visible in the Deactivated column → re-auth).

### 5. Dashboard

- New `POST /api/anthropic/keys/role` (proxy-key gated via `_action_authorized`, mirroring
  status/rename/delete): body `{id, role}`, validate `role in {"primary","standby"}`. **Refuse to
  demote the last alive primary** (return 400) — otherwise the sole key, set to standby, self-reverts
  on the next request (standby tier serves it → auto-promote) with a confusing `role_change/promote`.
  On success: `db.set_anthropic_key_role` → `pool.reload()`.
- UI (`AnthropicView.svelte`): a **Role** column (`primary`/`standby`) and a **Make standby / Make
  primary** toggle in the actions cell. Rebuild the SPA (`npm run build`).

### 6. Observability

- `role_change` event (`decision`: `promote` | `set_standby` | `set_primary`) on manual changes and
  auto-promotion.
- Keep-warm refreshes carry `source="standby_keepwarm"` (accurate now that the usage poller skips
  standbys, so the smoke pass is the only keep-warm driver), distinct from `scheduled_smoke` /
  `proxy_request`.

## Edge cases

- **Multiple standbys:** the served one promotes; others stay standby (still kept warm).
- **No primary alive (e.g. only a standby):** it's served and promoted immediately.
- **`api_key`-type standby:** allowed; keep-warm is oauth-only; it waits until failover, then serves
  + promotes.
- **All keys primary (default / today):** `primary_alive` is the full set → `pick()`, the oauth
  redirect, and `next_available_in` behave exactly as before. Zero behavior change until a key is
  marked standby (locked by an explicit regression test).
- **A promoted key later hits `low_balance`:** `_recheck_low_balance_loop` (`:2973-3013`) sends a real
  `/v1/messages` probe — normal for a serving (now-primary) key; a *dormant* standby never reaches
  low_balance because it takes no classified traffic.

## Testing (TDD)

DB / migration:
1. Migration adds `role`; existing rows read `"primary"`; fresh insert defaults `"primary"`; insert
   with `role="standby"` persists.
2. `set_anthropic_key_role` updates the column and records a `role_change` event.

Pool pick/failover:
3. Alive primary + standby → `pick()` returns the primary (sticky, repeatedly).
4. Primary **cooled** (not banned) + standby → `pick()` returns None (not the standby); and
   `next_available_in` returns the primary's remaining cooldown (not 0).
5. Primary **deactivated** (`status="inactive"`) + standby → `pick()` returns the standby.
6. Primary **low_balance** + standby → `pick()` returns None (no failover), **both** pre-reload
   (in-memory `mark_low_balance`) and post-reload (row `status="low_balance"`).
7. **Cross-tier redirect regression:** api_key **primary** (sticky) + oauth **standby**, a primary
   alive → `pick()` does **not** redirect to the standby.
8. **All-primary equivalence:** with only primaries, the `pick()` sequence is identical to the
   pre-role behavior (lock the no-regression claim).
9. `promote_to_primary` flips role + records `role_change/promote`; second call is a no-op.

Refresh flag / keep-warm:
10. `ensure_valid_token(activate=False)` on an expired oauth key persists the rotated token and
    returns it but makes **no** activation requests; `activate=True` still activates (regression).
11. Smoke pass over [primary, standby]: primary → refresh + activation + one smoke `send`; standby →
    refresh only, **no** `send`, `source="standby_keepwarm"` on its refresh events.
12. `_build_oauth_usage_payload` with a standby present → the standby is **absent** from the result
    and `ensure_valid_token` is **not** called for it (no refresh/activation).

Promotion / handler:
13. Standby picked to serve but its `ensure_valid_token` returns `None`/`_REFRESH_BLOCKED` → **not**
    promoted (still `role="standby"`).
14. Dashboard: `POST /api/anthropic/keys/role {id, role:"standby"}` → 200, `set_anthropic_key_role`
    awaited `("id","standby")` + `audit_source="dashboard"`, `pool.reload()` awaited; invalid role →
    400; unknown id → 404; non-proxy-key → 401; demoting the last alive primary → 400.

Full suite (`uv run pytest -q`) green; SPA builds (`npm run build`).

## Deployment note

Additive `ADD COLUMN` migration — safe without stopping writers. Deploy: run migrations → deploy code
(SPA bundle already built into `static/app`). No writer-stop dance.
