# Two subscriptions behind one proxy: priority routing with cache affinity

**Date:** 2026-09-18
**Components:** `src/smart_proxy/anthropic_proxy.py` (`_AnthropicKey`, `AnthropicKeyPool.pick` /
`next_available_in` / `cooldown` / `promote_to_primary` / `reload`, `_classify_unsuccessful_response`,
`_proxy_handler`), `src/smart_proxy/db.py` + `src/smart_proxy/db_migrations.py` (schema),
`src/smart_proxy/config.py` (thresholds, pin TTL), `src/smart_proxy/dashboard_api.py` +
`web/src/views/AnthropicView.svelte` + `src/smart_proxy/__main__.py` (operator surfaces)
**Status:** design, after an adversarial review against the code and the live database. Every
place where the review changed the proposal is marked **Corrected** (the reviewer's proposed
resolution, for the user to accept or reject) or listed under **Open questions** (no default
chosen). Nothing was fixed silently.
**Builds on:** `2026-07-21-anthropic-standby-key-failover-design.md` (standby semantics, kept
intact) and the `role='fallback'` paid tier that exists in the code today.

## Problem / intent

Two Anthropic **subscriptions** (two accounts, two quotas) behind one proxy, with strict priority:
subscription A serves everything; when A hits its 5-hour or 7-day limit, traffic moves to B on
its own; when A recovers, new work goes back to A. For prompt-cache hits, requests from one caller
keep landing on the same Anthropic credential while it is available.

**The correction that shapes everything:** `role='standby'` is *not* the second subscription. A
standby is a warm spare credential for the **same** account, held so a bricked OAuth refresh on
the primary does not take the account offline. Its rate limits are the primary's rate limits, so
failing over to it on a 429 is pointless, and
`tests/test_anthropic_standby_pick.py::test_cooled_primary_no_failover_and_retry_after` is
correct and must keep passing. The second subscription is a **second `role='primary'`**.

## Facts established by the review

The design under review made claims about the code; several were already corrected once during
discussion. Each claim was re-verified. The ones that matter are listed here so the decisions
below can cite them.

**Code (`src/smart_proxy/anthropic_proxy.py` unless stated):**

- F1. `pick()` (`:752-800`) is a global sticky cursor `self._index` (`:356`, advanced at `:786`).
  Nothing else in the package reads or writes `_index`; the only other reference is a comment in
  `_pick_fallback` (`:732`). One test does read it:
  `tests/test_anthropic_fallback_key.py:144-151`
  (`test_picking_the_fallback_leaves_the_sticky_index_alone`).
- F2. Inside the eligible tier, `pick()` **redirects an `api_key` primary to any pickable oauth
  primary** (`:790-796`, `_find_pickable_oauth_index` at `:497`). This preference beats stickiness
  and is locked by `tests/test_anthropic_proxy_schedule.py:168` (`prefers_available_oauth_over_sticky_api_key`),
  `:208` (`falls_back_to_api_key_when_preferred_oauth_is_in_cooldown`) and `:246`
  (`switches_back_to_oauth_once_cooldown_expires`). The proposed selection algorithm omitted it.
- F3. `_primary_alive()` (`:729`) is global: `any(role == "primary" and status != "inactive")`.
  Post-`reload()` an inactive key is simply absent from `_keys`, because
  `get_active_anthropic_keys` loads only `active`/`low_balance` (`db.py:2133-2137`).
- F4. `cooldown()` (`:802`) clamps every duration to `_MAX_COOLDOWN_SECONDS = 3600` (`:91`);
  `clear_cooldowns` (`:835`) and `cooldown_snapshot` (`:818`) know only `_cooldowns` and
  `_model_cooldowns`; `reload()` (`:390`) carries both dicts across a reload.
- F5. The pool keeps 7d utilization from response headers in `_unified_state` (`:388`, fed by
  `observe_unified_headers` `:636`), **and** 5h + 7d utilization per key from the usage poller in
  `_window_state` (`:382`, fed by `observe_windows`). Units differ: headers arrive as a fraction
  (0.65) and are multiplied by 100 before storage (`:2866-2872`); the poll channel stores percent.
  `_extract_rate_limit_headers` (`:2804`) reads `unified-reset` and `unified-7d-reset` but not
  `unified-5h-reset`, although that header exists (2026-09-01 forensics spec, section 4).
- F6. **The design's "cheap spike" source is wrong.** `record_rate_limit` is called from the 429
  branch (`:2713`) *and* from `_maybe_record_utilization` (`:2894`) on **every** `/v1/messages` 2xx
  (`:3372`). Worse, `record_rate_limit` (`db.py:2488-2525`) dedups by `(credential, reset_at)` and
  the UPDATE path never touches `retry_after` or `limit_type`, so a 429 that lands after a 2xx in
  the same window leaves no trace of itself. Live DB: 166 `rate_limit_log` rows, 6 with a
  `retry_after`, against 95 real 429s. The correct sample is
  `anthropic_key_events WHERE event_type='rate_limited'` (`retry_after`, `model`, and
  `context_json` with `limit_type`/`utilization_5h`/`utilization_7d`).
- F7. The classification gate (`:3131`) is `401/402/403/429/5xx`. A 404 falls through to the
  success path and is streamed to the client verbatim; no event is recorded for it.
- F8. A 403 whose error type is not `authentication_error` and whose message is not billing-shaped
  only logs (`:2681`) and returns `retry` (`:2685`) — no cooldown, no ban, **no event**. The handler
  then `continue`s (`:3196-3205`) and `pick()` at `:3007` has no `exclude`, so the same key is
  picked `max_attempts` times. Verified bug.
- F9. `pick()` at `:3007` and the 429 probe at `:3152` pass no `exclude`; only the overload branch
  (`:3176`) does. The comment at `:3171` documents that a plain 5xx is retried on the same key
  on purpose.
- F10. `promote_to_primary` (`:1040`) is one-way; nothing demotes. The handler promotes at `:3077`.
  Re-authorization never reuses a row: the OAuth callback inserts a new key id (`:4443`); the live
  table holds nine successive `pro-sp-auth` rows for one account, all but the newest `inactive`.
- F11. `_standby_keepwarm_step` (`:4910`) and `_standby_keepwarm_sleep_seconds` (`:4883`) already
  iterate every live oauth standby and sleep until the soonest one is due. Multiple standbys are
  handled today; no change needed.
- F12. `_upgrade_cache_ttl` (`:2978`) applies only to real CLI traffic that is not a sub-agent
  (`_is_real_claude_code_cli` `:2059`, `_is_claude_code_subagent` `:2070`). The 1h cache write
  premium the "stay on B" rationale rests on is a 1-hour TTL.
- F13. Migration plumbing for a new `anthropic_keys` column: SQLite `CREATE TABLE` at `db.py:160`
  **and** the `MIGRATIONS` copy at `db.py:400`, an `ALTER ... ADD COLUMN` appended after
  `db.py:611` (the runner at `db.py:1114` swallows the duplicate-column error on re-run), the
  Postgres `CREATE TABLE` at `db_migrations.py:79` **and** a new `POSTGRES_MIGRATIONS` entry after
  `0014_usage_bucket` (`db_migrations.py:524`) using `ADD COLUMN IF NOT EXISTS`, the snapshot
  column list at `db.py:700-722`, the import default map at `db.py:900-910`, and both the column
  list and the VALUES tuple of `insert_anthropic_key` (`db.py:2096-2131`). Every existing ALTER is
  `NOT NULL DEFAULT <literal>`.
- F14. No CLI command and no dashboard endpoint sets anything but `role` (`dashboard_api.py:487-527`,
  `__main__.py:588-760`). The design added two columns and no way to set them.

**Live database (`smartproxy_db`, events through 2026-09-17, read-only queries):**

- F15. 95 `rate_limited` events. `anthropic-ratelimit-unified-representative-claim` took exactly
  three values: `seven_day` (59), `five_hour` (30), `seven_day_overage_included` (6). No
  model-scoped claim has ever been observed, although the account does have a model-scoped weekly
  window (`oauth_window_log` carries `limit:weekly_scoped:Fable`, 19 rows).
- F16. Every `five_hour` 429 landed at 5h utilization **100-107%**; every `seven_day` 429 at 7d
  **exactly 100%**. `seven_day_overage_included` 429s landed at 7d 63-65% and 5h 5-10%, so
  utilization does not predict that claim at all.
- F17. Both `five_hour` and `seven_day` 429s are **account-wide**: on 2026-09-04 (5h) and
  2026-08-13 (7d) the same key returned 429 for opus, sonnet, haiku and fable within minutes. With
  today's per-`(key, model)` parking each model burned its own 429 and retry.
- F18. `retry-after` on `five_hour` claims ranged 1905-13767 s; on `seven_day`, 299-77885 s.
- F19. `anthropic_key_events` holds **zero** rows with HTTP 400/403/404 or error types
  `not_found_error`/`permission_error`/`invalid_request_error`, because F7 and F8 never record
  one. There is no evidence in this repo or database of what Anthropic returns for a model that a
  plan does not include.
- F20. Today's pool: one `active` primary (created 2026-09-11) and one `active` standby (created
  2026-09-17); every other row is `inactive`. The account is `subscription_type='pro'`.

## Decisions

### 1. Data model

Two columns on `anthropic_keys`:

- `priority INTEGER NOT NULL DEFAULT 100` — lower serves first. Existing rows get 100, so the
  ordering among them degrades to today's `created_at` order (F3).
- `standby_for TEXT NOT NULL DEFAULT ''` — the `id` of the primary this standby spares. Empty
  means "the old global rule" (see 3).

**Corrected from the design:** the design said `priority INTEGER` and `standby_for TEXT` with no
`NOT NULL DEFAULT`. A nullable `priority` breaks in two ways: Python sorts `None` against `int`
with a `TypeError`, and SQLite `ORDER BY` puts NULL *first*, so a freshly re-authorized row (F10)
would become the top priority. `_anthropic_key_from_row` (`:335`) must coalesce with an explicit
`is None` check, **not** `or 100` — priority `0` is the value the user is most likely to give A,
and `0 or 100` is 100.

`_AnthropicKey` gains `priority: int = 100` and `standby_for: str = ""` with defaults, so the
tests that construct it by keyword keep working. Migration plumbing is exactly F13; snapshot
import defaults are `100` and `''`.

### 2. Selection

`pick(model=None, *, fallback_for=None, exclude=None, affinity=None)`. `affinity` is the pin
lookup key (section 4); `None` means no pin is consulted or written, which is what every existing
test does.

```
candidates(filter) := primaries in _keys, not in exclude, _is_pickable(model),
                      and (filter is off or under both utilization thresholds)
order              := (priority, 0 if oauth else 1, position in _keys)

1. C = candidates(threshold on)
2. if a pin exists for `affinity` and its key is in C:
       chosen = pinned key
   else:
       chosen = first of C by `order`
3. oauth redirect (F2, unchanged semantics): if chosen is an api_key and C holds an
   oauth key, chosen = first oauth key of C by `order`
4. if C is empty: repeat 1-3 with the threshold off (an exhausted key beats a 429
   when nothing else can serve)
5. if still nothing: eligible standbys (section 3), same order, same redirect
6. if still nothing and fallback_for: _pick_fallback (unchanged, never pins)
```

`pick()` only **reads** the pin. It never writes it (section 4 says who does). `self._index`
is removed; deterministic `order` gives the same-key retry that the 5xx path relies on (F9) for
free, because a pinned session re-picks its pinned key and an unpinned one re-picks the first key.

**Corrected from the design:** step 3 is new. Without it, an `api_key` primary created before an
oauth primary sorts first at equal priority and `tests/test_anthropic_proxy_schedule.py:168`
fails; worse, a pinned session would stay on a paid `api_key` primary after the oauth key
recovered, which `:246` forbids today. The redirect is evaluated on the pinned key too, for that
reason.

**Corrected from the design:** "the pin is updated to it" at step 3 of the original is dropped;
see section 4 for why pin writes happen only on a 2xx.

`next_available_in` mirrors the same eligible set (primaries, then eligible standbys, then a
scoped fallback), exactly as it mirrors the tiering today (`:958-990`). When both subscriptions
are cooled the client gets the sooner of the two, as now.

### 3. Standby eligibility, per slot

A standby with `standby_for = X` is eligible only when X is **dead**: X is in `_keys` with
`status == "inactive"` (pre-reload, set by `deactivate` `:1006`) **or** X is absent from `_keys`
and the database row for X has `status == 'inactive'` (post-reload, F3). `reload()` loads the set
of inactive ids alongside the active rows to answer the second case; `pick()` must not depend on
any other state built in `reload()`, because the standby tests set `_keys` directly on a pool
with `db=None`.

A standby with empty `standby_for` keeps today's global rule: eligible only when no primary at
all is alive (F3). Eligible standbys are ordered by their own `priority` (see below), so when two
primaries are dead at once, A's standby serves before B's.

**Corrected from the design (dangling reference):** a `standby_for` that names a row which is
neither loaded nor `inactive` (deleted, `status='deleted'`, or a typo) does **not** make the
standby eligible. It falls back to the global rule and `reload()` logs a warning naming the key.
Rationale: the operator will delete bricked rows (there are nine in the live table, F20). A
standby whose primary was deleted holds the quota of whichever *new* row re-authorized that
account; engaging it on that row's cooldown is exactly the pointless failover the contract forbids.

**Corrected from the design (priority ownership):** the design had `promote_to_primary` copy
`priority` from the primary. Post-reload that primary is not loaded (F3), so there is nothing to
copy from. Instead the standby row **carries its own `priority`**, set equal to its primary's when
the standby is assigned (section 9), and promotion keeps it. Promotion clears `standby_for`. This
also keeps `promote_to_primary`'s single `set_anthropic_key_role` call, which the fake DB in
`tests/test_anthropic_standby_pick.py:60-72` tolerates only because it takes `**kw`; the new
`standby_for=""` write rides along as a keyword argument to that method.

**Corrected from the design (sibling standbys):** with two standbys `S1`, `S2` both `standby_for =
A`, promoting `S1` leaves `S2` eligible forever, because A stays dead. The next time `S1` (now a
primary) is cooled by a 429, `S2` engages and is promoted: a 429 woke a standby of the same
account, which is the contract violation this whole spec is built around. Today's global rule
does not have this bug (a promoted `S1` is an alive primary). Fix: when promoting `S1`, re-point
every loaded key with `standby_for == A` to `standby_for = S1`, in memory first and then in the
database, inside the same best-effort `try`. When there are no siblings no extra call is made, so
the promotion test's call log stays `[("stby", "primary", "promote")]`.

Steady state after a promotion stays as the design accepted: one-way, nothing demotes. If the
operator re-activates the dead row from the dashboard toggle (`dashboard_api.py:451`) two
same-account primaries share a priority and the older one serves; the operator demotes the other
by hand. The re-authorization case (F10: a *new* row) is a runbook item, see section 9 and open
question Q4.

### 4. Pins (cache affinity)

- Key: `(usage_proxy_key, session_id)` where `session_id` comes from `_session_id(req_body
  ["metadata"])` (`request_classify.py`), computed once before the attempt loop. Empty
  `session_id` (SDKs, OpenAI-compat loopback, direct API) degrades to `(usage_proxy_key, "")`.
  Claude Code sub-agents share the main session's id, so they share its pin.
- Value: `key_id`. In memory on the pool; survives `/_reload` (a separate dict, like
  `_model_cooldowns`); lost on restart (accepted).
- **Written only when upstream answers 2xx**, in the handler right after the gate at `:3131`
  decides the response is a success, before streaming. Never written in `pick()`, never on a
  failed attempt, never by the 429 probe at `:3152`, never by a fallback pick (the contract
  `tests/test_anthropic_fallback_key.py:144` protects today via `_index`).
  **Corrected from the design**, which said both "updated on 2xx" and "updated at step 3".
- Precedence: a pin is honoured only while its key is a *candidate* (pickable, not excluded,
  under threshold). A cooldown, a ban, a threshold breach, or an in-request `exclude` all move
  the session; nothing lets a pin keep a caller on a key the pool would not otherwise serve.
- Eviction: idle TTL plus an LRU size cap (default 10 000 entries). A pin to a key id that no
  longer exists is harmless (it is never a candidate) and is dropped by the cap; `reload()` may
  also prune pins whose key is gone, purely for tidiness.
- A `reload()` that swaps `_keys` instances does not invalidate pins (they hold ids, not objects).

**Idle TTL — see Q1.** The design proposes ~6h. The stated reason for keeping a session on B after
A recovers is the 1h cache-write premium (F12). Once a session has been idle longer than the cache
TTL its cache is gone on *both* keys, so returning it to A costs nothing extra while leaving it on
B drains B's window for no benefit. The reviewer recommends a default of 1h (matching
`_upgrade_cache_ttl`) exposed as `anthropic_proxy_pin_idle_seconds`.

### 5. Returning to the preferred subscription

Unchanged from the design: when A becomes a candidate again, sessions pinned to B stay on B (their
pin is honoured); only sessions without a live pin start on A. Within the pin TTL this is the
cheapest choice; beyond it, Q1 decides.

Note for the two-equal-primaries case: today the global cursor stays on B after A recovers for
*everyone*; the new rule sends new sessions to A. This is the intended feature and is listed as an
accepted deviation in section 11. The live pool has one primary (F20), so nothing in production
changes on deploy.

### 6. Utilization thresholds

Settings `anthropic_proxy_switch_utilization_5h` and `anthropic_proxy_switch_utilization_7d`,
fractions, both default `1.0` = disabled. F16 supports the user's reading: real 429s land at or
above 100%, and one claim (`seven_day_overage_included`) fires at 65% with no warning in the
utilization at all. A 429 stays the authoritative signal; the threshold is an optional early
hand-off.

**Corrected from the design (sources and units):** the design said the pool "keeps only 7d
utilization". It keeps both channels (F5). The threshold reads the **freshest** observation per
`(key, window)` across `_unified_state` (headers, request-granular, only updated by that key's
own 2xx responses, so an idle key never refreshes it) and `_window_state` (poller, refreshed for
idle keys too). Both must be compared in one unit; the header channel is already converted to
percent before storage and the settings are fractions, so the comparison site converts once and
is covered by a unit test. Extend `_extract_rate_limit_headers` to read `unified-5h-reset` and
`observe_unified_headers` to keep `(utilization_5h, reset_5h)` next to the 7d pair.

Staleness: an observation whose window reset epoch is in the past counts as 0%, otherwise a
long-idle key looks exhausted until its next request. The poll channel's `resets_at` and the
header channel's `-5h-reset`/`-7d-reset` provide the epoch per window.

Flapping: a session pinned to B moves to A only if B breaches and A is under; it does not
ping-pong while both are under. If both are over, step 4 keeps it where it is.

### 7. What a 429 parks — by claim, with evidence

`_classify_unsuccessful_response` already reads the claim (`:2701`). Parking rule at `:2766`:

| `representative-claim` | park | evidence |
|---|---|---|
| `five_hour`, `seven_day`, `seven_day_overage_included` | whole key, `min(retry-after, 1h)` | F15, F17: account-wide, all models 429 together |
| anything else, or empty | `(key, model)` only — today's behaviour | no model-scoped 429 observed yet (F15); the `limit:weekly_scoped:Fable` window exists, its claim string is unknown |

Unknown claims are logged at WARNING with the value so the vocabulary grows from real traffic.
The `cooldown()` signature is unchanged; the claim logic lives in the 429 branch, so
`tests/test_anthropic_proxy_model_cooldown.py` (which calls `cooldown(model=...)` directly) is
unaffected.

**Corrected from the design (the spike):** the spike is done (F15-F18) and it was run on
`anthropic_key_events`, not `rate_limit_log`, for the reason in F6. Whole-key parking on the three
known claims is also a fix for today's behaviour: on 2026-09-04 one exhausted key produced
fifteen separate 429s across five models because each model was parked on its own.

### 8. Model unavailable on a credential (404 / 403)

**Verified:** F7 — a 404 reaches the client verbatim today; no retry, no record.
**Not verified, no evidence exists:** what Anthropic returns for a model a plan does not include
(F19). Everything below is therefore staged so the proxy gathers evidence before it acts on it.

Stage 1 (ship with this work): record an `anthropic_key_events` row for every 404 and every
non-auth, non-billing 403 (`event_type='model_unavailable_candidate'`, with `error_type`, the
first 300 chars of the message, `model`, `path`). This costs nothing and turns F19 into data.

Stage 2 (behaviour), split by how safe each half is:

- **404 `not_found_error` whose message contains the requested model string** → mark
  `(key, model)` unsupported for `anthropic_proxy_model_unsupported_ttl_seconds` (default 24h),
  retry on the next key with `exclude`. A typo'd model id marks every key for a model that does
  not exist, which is harmless; a real plan gap is exactly the case wanted. Out of keys → return
  the original 404 body as-is.
- **403 `permission_error`** → `exclude` the key for the rest of *this* request (always correct:
  same key, same body, same answer) and record the event. It does **not** set the 24h mark.
  **Corrected from the design:** a 403 can be about the request (a beta, a tool, a feature) rather
  than the model; a 24h `(key, model)` mark on it would park a model that serves every other
  request fine. Promote 403 to the 24h mark only if stage 1 shows a model-shaped message.

Mechanics of the mark (**corrected from the design**, which under-specified them): it is a
**separate** dict `_model_unsupported[(key_id, model)] -> deadline`, not `_model_cooldowns`,
because `cooldown()` clamps to 1h (F4) and the two mean different things. `_is_pickable(model)`
consults it; `reload()` carries it across like `_model_cooldowns`; `clear_cooldowns()` clears it
too (the operator's "I upgraded the plan, ask again" is the same gesture); `cooldown_snapshot()`
lists it with a `kind` so the dashboard shows why a model is off a key. `next_available_in` must
**ignore** it: otherwise a model that no key supports produces a 429 "You've reached your model
limit. Retry in 23h", which is false. Instead, the handler checks
`pool.model_unsupported_everywhere(model)` before the retry-after branch at `:3015` and answers a
404 `not_found_error` ("model X is not available on any configured subscription").

### 9. Operator surfaces (missing from the design)

**Corrected from the design:** two columns without a way to set them is a feature usable only by
SQL. Minimum surface, mirroring the existing role endpoint:

- `POST /api/anthropic/keys/priority` `{id, priority}` (admin-gated like `role`).
- `POST /api/anthropic/keys/role` accepts an optional `standby_for` when `role='standby'`. It
  must reference an existing `role='primary'` oauth row that is not the key itself; on success
  the standby's `priority` is set to that primary's. `role='primary'` clears `standby_for`.
- `_api_anthropic_keys` payload (`dashboard_api.py:337-352`) adds `priority` and `standby_for`;
  the Svelte table shows both; "Make standby" asks which primary.
- CLI: `anthropic-key set-priority <id-prefix> <n>` and
  `anthropic-key set-standby-for <id-prefix> <primary-id-prefix>`; `add-oauth` and `add-apikey`
  take `--priority`.
- Runbook line for the dashboard help and the README: after re-authorizing a dead primary, set
  the new row's `priority` and re-point its standby (`standby_for`) at the new id. Until then the
  standby follows the old dead row (section 3) and the new row sits at priority 100.

### 10. Cooldowns keep the 1h clamp

Unchanged and deliberate. F18 shows `retry-after` up to 3.8h on 5h claims and 21.6h on 7d
claims; clamping re-probes the subscription hourly, which the user has seen lift early. The 24h
mark of section 8 is the one exception and lives in its own dict for that reason.

### 11. Backward-compatibility contract

With one primary and one standby everything behaves as today. Walk of
`tests/test_anthropic_standby_pick.py` against section 2 (all calls are `pick()` with no
`affinity`, no thresholds set):

| test | new algorithm | result |
|---|---|---|
| `test_serves_primary_while_alive` | C = [prim]; first by order → prim | pass |
| `test_cooled_primary_no_failover_and_retry_after` | C empty (cooldown is in `_is_pickable`); step 4 empty; step 5: standby has empty `standby_for` → global rule, prim alive → not eligible → `None`; `next_available_in` mirrors → prim's cooldown | pass |
| `test_deactivated_primary_fails_over_to_standby` | steps 1-4 empty; step 5 global rule, no primary alive → stby | pass |
| `test_low_balance_primary_no_failover` | banned → C empty; prim `status != inactive` → alive → `None` | pass |
| `test_api_key_primary_does_not_redirect_to_standby` | C = [apik]; redirect looks for oauth *in C* → none → apik | pass |
| `test_all_primary_unchanged` | C = [a, b], equal priority, both oauth → position → a | pass |
| `test_deactivate_and_low_balance_update_in_memory_status` | untouched | pass |
| `test_promote_flips_role_records_event_idempotent` | one `set_anthropic_key_role(kid, "primary", standby_for="", ...)` call; fake absorbs `**kw`; no siblings → no second call | pass |

Also walked: `tests/test_anthropic_fallback_key.py` (`:100`, `:127`, `:134` pass under the same
reasoning; `:144` see below), `tests/test_anthropic_proxy_schedule.py:124/168/208/246` (pass
because of the step-3 redirect), `tests/test_anthropic_proxy_model_cooldown.py` (unaffected).

**Known test edit — flagged, not hidden:**
`tests/test_anthropic_fallback_key.py:144-151` reads `pool._index`, which no longer exists. It
asserts an implementation detail of a contract this spec keeps ("a fallback pick does not move
the primary tier's position"). It must be rewritten to assert that a fallback pick leaves the
pin untouched. This is the only test in the suite that touches `_index` (F1). If the user would
rather keep `_index` as a vestigial attribute so no test changes, see Q6.

Guarantees restated for the single-subscription case:

- equal `priority` on existing rows → `created_at` order, as today;
- empty `standby_for` → the old global rule;
- one primary → every pin resolves to it → indistinguishable from the global cursor;
- thresholds default to 1.0 → no pre-switching;
- a cooled primary does not wake the standby: 429 with `retry-after`, as now;
- `tests/test_anthropic_standby_pick.py` passes unchanged.

Accepted deviations, all opt-in or bug fixes:

- 403 `permission_error` no longer burns every attempt on one key (F8).
- 404 is retried on the next key and, with a model-shaped message, marks the model (section 8).
- A `five_hour`/`seven_day` 429 parks the whole key instead of one model (section 7, F17).
- Two *equal-priority* primaries: new sessions prefer the first by `created_at` once it recovers
  instead of following the global cursor (section 5). No production pool has two primaries (F20).

### 12. Observability

- `role_change` events gain `context.standby_for`; a new `priority_change` event.
- Log one INFO line when a session's pin moves between keys (`session=…, from=…, to=…, why=…`),
  throttled per session, so a subscription switch is visible in the log without an alert (alerts
  are out of scope).
- Unknown representative claims log at WARNING with the value (section 7).
- Stage-1 `model_unavailable_candidate` events (section 8).

## Out of scope (unchanged)

Weekly-quota balancing between subscriptions; persisting pins or window state across restart;
an alert on subscription switch; mid-stream retry (separate spike on the real CLI first);
automatic demotion after a promotion; automatic adoption of standbys by a re-authorized row.

## Open questions for the user

- **Q1. Pin idle TTL.** Design: ~6h. Reviewer: 1h, equal to the cache TTL the rationale depends
  on (section 4); a longer TTL keeps idle sessions draining B with no cache benefit. Which, and
  should it be a setting?
- **Q2. Slot model.** `standby_for` (this spec: explicit link, fails safe when unset, needs the
  sibling re-pointing and dangling-reference rules) versus "a standby is eligible when no primary
  at its own `priority` is alive" (one column, no re-pointing, no dangling ids, but a standby
  left at the default priority 100 would engage whenever every primary is cooled and get
  promoted, which is the forbidden failover). The reviewer kept `standby_for` for the fail-safe
  default; say if you prefer the simpler model with a validation rule instead.
- **Q3. 403 scope.** Should a 403 `permission_error` ever set the 24h model mark, or only
  `exclude` for the request (this spec) until stage 1 shows what those messages look like?
- **Q4. Re-authorization runbook.** After a bricked primary is re-authorized as a new row, the
  standby still points at the dead row and the new row sits at priority 100 until the operator
  fixes both (section 9). Acceptable as a runbook item, or should the OAuth callback offer
  "replace key <id>" (copy `priority`, re-point its standbys, keep the old row inactive)?
- **Q5. Whole-key park for `seven_day_overage_included`.** Six events on one day, one model.
  Grouped with the account-wide claims here by name; if you know that claim to be model-scoped on
  your plan, move it to the per-model row.
- **Q6. `_index`.** Remove it (this spec; one fallback test is rewritten) or keep it as a dead
  attribute so no test outside the standby file changes?
- **Q7. Threshold source.** Use both channels with "freshest wins" (this spec) or headers only, as
  the design implied? Headers-only never refreshes an idle key, so a key that crossed the
  threshold and then went idle stays "over" until its reset epoch passes.
- **Q8. Pin key for OpenAI-compat.** All OpenAI-compat callers on one sp-key share a single pin
  `(sp-key, "")`. Fine as accepted, or should the compat layer synthesize a per-conversation id?

## Testing (TDD)

Pool:
1. Two primaries, priorities 0/1: unpinned pick → A; A cooled → B; A recovers → unpinned pick →
   A; a session pinned to B stays on B while B is a candidate; moves when B is cooled or excluded.
2. Threshold: A over 5h threshold, B under → new session → B; both over → A (soft); stale
   observation (reset epoch passed) counts as 0; unit conversion covered explicitly.
3. Step-3 redirect with a pinned api_key primary and a recovered oauth primary → oauth.
4. Per-slot standby: A inactive in memory → S_A eligible; A absent after reload with DB status
   `inactive` → eligible; A cooled → not eligible; A deleted/dangling → global rule + warning;
   B alive does not block S_A; empty `standby_for` keeps the global rule (the existing tests).
5. Sibling re-pointing on promotion; no extra DB call when there are no siblings.
6. `next_available_in` ignores unsupported marks; `model_unsupported_everywhere` drives a 404.
7. Unsupported mark: 24h, survives reload, cleared by `clear_cooldowns`, shown in
   `cooldown_snapshot` with a kind.
8. `pick()` never writes a pin; the 429 probe and fallback picks never write a pin.

Classifier / handler:
9. `five_hour` claim → whole-key cooldown; empty or unknown claim → `(key, model)`.
10. 403 `permission_error` → key excluded for the request, event recorded, second key tried.
11. 404 with model in message → next key tried, mark set; out of keys → original 404 body;
    404 without model in message → verbatim, no mark, event recorded.
12. Plain 5xx still retries on the same key (pinned and unpinned).

Schema / surfaces:
13. Migrations add both columns on SQLite and Postgres; snapshot export/import round-trips them
    and fills defaults for older snapshots; `insert_anthropic_key` accepts both.
14. Role endpoint validates `standby_for`; priority endpoint; CLI commands; `keys` payload.

Contract: `tests/test_anthropic_standby_pick.py` unchanged and green; full suite green; SPA builds.

## Deployment note

Additive `ADD COLUMN` migrations with literal defaults — no writer stop, no PK lock (the
`usage_daily` dance in memory does not apply). Order: migrate → deploy → set `priority` on the
two primaries and `standby_for` on the standby from the dashboard → `/_reload`. Until priorities
are set, two primaries at 100 serve in `created_at` order, which is today's behaviour.
