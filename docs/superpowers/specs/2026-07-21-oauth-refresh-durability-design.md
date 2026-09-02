# OAuth refresh durability — don't lose a rotated refresh token

**Date:** 2026-07-21
**Component:** `src/smart_proxy/anthropic_proxy.py` (`AnthropicKeyPool.ensure_valid_token`),
`src/smart_proxy/anthropic_oauth.py` (`refresh_oauth_token`), `src/smart_proxy/dashboard_api.py`
(`_api_anthropic_key_refresh`)
**Status:** design (revised after Fable review)

## Problem

Anthropic OAuth **rotates the refresh token on every `/token` call** (single-use: every
`refresh_succeeded` in prod carries `rotated_refresh_token: true`). The old refresh token is
invalidated server-side as a *side effect* of the call; the replacement is returned **only in the
response body**. If we lose or discard that replacement, the stored refresh token is permanently
dead — every later refresh returns `HTTP 400 invalid_grant "Refresh token not found or invalid"`,
and the key is unrefreshable until a human re-authenticates.

This is **not a concurrency race.** The proxy and the `oauth_usage` poller run in one process and
share one `asyncio.Lock` (`_refresh_lock`); prod history shows 538 refresh attempts with 536
recorded outcomes — no simultaneous collisions. The failure is a **durability/atomicity** defect in
the single-refresh sequence. Two distinct occurrences in prod history, each of which bricked a key:

1. **Lost response (the 2026-07-20 incident, key `67d91934`).** Exactly one "orphan" refresh
   attempt in all of history: `refresh_attempt` logged with no outcome. The `/token` call reached
   Anthropic and rotated the token, but the response was lost to an **uncaught exception** — a
   network read error/reset, or the inbound client request being cancelled mid-refresh. Only
   `httpx.HTTPStatusError` (429) and `RuntimeError` are caught in `ensure_valid_token`; everything
   else (httpx transport/timeout errors, `asyncio.CancelledError`) escapes uncaught, releasing the
   lock with the new token never received. DB keeps the dead token → next refresh `invalid_grant` →
   the key was deactivated ~4 min *before* its access token actually expired.

2. **Discarded response (key `275f…cc`, 2026-04-17).** Exactly one `activation_failed` in history.
   The `/token` call **succeeded** (token rotated, replacement in hand), but the follow-up
   `activate_oauth_access_token` call failed. The `except` handler ran `cooldown()` and returned
   `_REFRESH_BLOCKED` **before** the persist step (`update_anthropic_oauth_tokens` runs *after*
   activation in the current code, `anthropic_proxy.py:481-529`) — so the rotated token was
   discarded. DB kept the dead token → every subsequent refresh `invalid_grant` (212 in a row).
   The **manual dashboard Refresh** handler (`dashboard_api.py:329-372`) has the identical
   activate-before-persist ordering ("Nothing is saved when either step fails"), so it can brick a
   key the same way, human-triggered.

`activate_oauth_access_token` only fires auxiliary "warmup" requests captured from Claude traffic
(`GET /api/...`); it has **no bearing** on whether the access or refresh tokens are valid — those
are already issued by `/token`. So sequencing activation *before* persistence is simply wrong.

## Goals

Make a single refresh **durable and crash-safe** so a key is not bricked by a transient failure:

1. **Persist rotated tokens the instant `/token` returns them, before activation** — in both the
   pool path (`ensure_valid_token`) and the manual dashboard path. Activation failure must never
   discard a rotated token. (Fixes defect 2 in both paths.)
2. **Shield the pool refresh from cancellation** so an inbound client disconnect cannot abort a
   rotation half-done. (Fixes the cancellation flavor of defect 1.)
3. **Catch network/timeout on `/token` explicitly** and classify failures precisely so we (a) never
   deactivate on a transient error, and (b) still deactivate a genuinely dead key so it surfaces for
   re-auth. (Handles the network flavor of defect 1 without regressing dead-key detection.)
4. **Keep a key alive while its access token is still usably valid** — safety net so a single
   unrecoverable loss does not *also* trigger an early deactivation inside the 5-min pre-expiry
   window.
5. **Re-read the DB token once before deactivating on `invalid_grant`** — guards the stale
   in-memory-object hazard (a `reload()`, which the manual Refresh triggers, swaps key objects; an
   in-flight handler holding the old object would otherwise deactivate a key whose DB tokens are
   good).

## Non-goals (deferred — "the rest comes later")

- **Serializing** the manual dashboard Refresh through `_refresh_lock` (its persist-order defect is
  fixed here; its lack of serialization vs the pool is a separate, lower-risk follow-up).
- A single-writer / background pre-expiry refresher.
- Stopping the `oauth_usage` poller from refreshing independently.
- Any cross-process DB advisory lock (unnecessary — single process).
- The upstream-401 path (`anthropic_proxy.py:~1398-1445`) that deactivates on a request-time 401;
  the ≥30s validity floor (below) mitigates the interaction, full treatment is a follow-up.

## Design

Changes are in `AnthropicKeyPool` plus a typed exception in `anthropic_oauth` and a small reorder in
`dashboard_api`. Callers of `ensure_valid_token` are unchanged: it still returns `token: str` (use
it), `None` (proxy deactivates; poller records `no_valid_oauth_token` **without** deactivating), or
`_REFRESH_BLOCKED` (try next key / soft failure).

### New shared exception (`anthropic_oauth.py`)

```
class OAuthRefreshError(RuntimeError):        # subclass → existing `except RuntimeError` still catches
    def __init__(self, message, *, status_code, error_code=None):
        super().__init__(message); self.status_code = status_code; self.error_code = error_code
```

`refresh_oauth_token` raises `OAuthRefreshError` (with `status_code` and the parsed OAuth `error`
code when present) instead of bare `RuntimeError` for `status_code >= 400` (non-429) and for the
malformed-body case (`status_code=200, error_code=None`). 429 still raises `httpx.HTTPStatusError`.
Backward compatible: dashboard and tests that catch `RuntimeError`/`Exception` still work.

### New pool state

- `self._refresh_backoff: dict[str, float]` — `key_id → monotonic deadline`. After a refresh fails
  while the token is still valid, park *refresh* (not the key) until this deadline. Pruned in
  `reload()` exactly like `_cooldowns` (drop past deadlines / absent keys).
- `self._transient_refresh_fails: dict[str, int]` — consecutive transient failures **while truly
  expired**; reset on success or on any keep-alive defer. Escape valve against an infinite
  cooldown→retry loop on a persistent 5xx/network fault.
- `self._refresh_tasks: set[asyncio.Task]` — strong refs to shielded refresh tasks (avoid GC /
  "task exception never retrieved").
- Constants: `_REFRESH_RETRY_BACKOFF_SECONDS = 60`, `_MAX_TRANSIENT_REFRESH_FAILS = 5`,
  `_TOKEN_VALID_FLOOR_MS = 30_000`.

### Two notions of "expired"

- `key.is_expired()` (unchanged; 5-min `_REFRESH_BUFFER_MS`) = *should proactively refresh*.
- `token_valid = bool(key.access_token) and key.expires_at is not None and now_ms < key.expires_at -
  _TOKEN_VALID_FLOOR_MS` = *the access token still safely works right now* (30s floor so we never
  serve a token about to expire mid-request and hit the unconditional 401-deactivate path).

### Control flow

`ensure_valid_token` shields the locked refresh, holding a strong ref and logging task exceptions:

```
async def ensure_valid_token(self, key, client, *, audit...):
    if key.key_type == "api_key":
        return key.api_key
    if not key.is_expired():
        return key.access_token
    task = asyncio.ensure_future(self._refresh_locked(key, client, audit...))
    self._refresh_tasks.add(task)
    task.add_done_callback(self._on_refresh_task_done)     # discard + logger.error on unretrieved exc
    return await asyncio.shield(task)
```

`asyncio.shield` runs `_refresh_locked` to completion even if the awaiting request is cancelled;
because the whole `async with self._refresh_lock` block lives *inside* the shielded coroutine, the
lock is always released normally (no "lock freed while refresh still running" hazard, no deadlock).
A cancelled caller raises `CancelledError` to its own handler; the shielded refresh completes and
persists in the background. Note: the shield now also covers the activation warmup (bounded,
uncancellable). Caveat: a caller cancelled mid-refresh loses *its* return value — if that refresh
ultimately yields `None`/`_REFRESH_BLOCKED`, no deactivation/cooldown happens for that request; the
next request re-evaluates against the persisted state.

`_refresh_locked` holds the double-checked lock:

```
async def _refresh_locked(self, key, client, *, audit...):
    async with self._refresh_lock:
        if not key.is_expired():
            return key.access_token
        now_mono = time.monotonic(); now_ms = int(time.time() * 1000)
        token_valid = bool(key.access_token) and key.expires_at is not None \
                      and now_ms < key.expires_at - _TOKEN_VALID_FLOOR_MS

        if token_valid and now_mono < self._refresh_backoff.get(key.key_id, 0.0):
            return key.access_token                         # serve valid token, don't hammer /token

        record refresh_attempt
        if not key.refresh_token:
            if token_valid:
                self._defer(key, now_mono); record refresh_deferred(reuse_valid_token, no_refresh_token)
                return key.access_token
            record refresh_failed(deactivate, missing_refresh_token); return None

        try:
            new_token, new_expires, rotated = await _refresh_oauth_token(
                client, key.refresh_token, key.client_id, scope=normalize_scope(key.scopes))
        except httpx.HTTPStatusError as exc:                 # 429
            retry_after = _parse_retry_after(exc, default=_REFRESH_RETRY_BACKOFF_SECONDS)  # defensive
            if token_valid:
                self._defer(key, now_mono, seconds=max(retry_after, _REFRESH_RETRY_BACKOFF_SECONDS))
                record refresh_deferred(reuse_valid_token, rate_limited); return key.access_token
            record refresh_rate_limited(cooldown); self.cooldown(key, retry_after)
            return self._REFRESH_BLOCKED
        except Exception as exc:                             # OAuthRefreshError | httpx transport/timeout | ...
            logger.exception("refresh failed for key %s", key.key_id[:12])   # don't launder bugs
            if token_valid:
                self._defer(key, now_mono); record refresh_deferred(reuse_valid_token, ...)
                return key.access_token
            # truly expired: decide fatal vs transient
            if _is_auth_fatal(exc):                          # OAuthRefreshError with 4xx status
                fresh = await self._reread_token_if_changed(key)   # stale-object guard
                if fresh:
                    return self._REFRESH_BLOCKED             # DB had a newer token; retry next cycle
                record refresh_failed(deactivate, exc); return None
            n = self._transient_refresh_fails.get(key.key_id, 0) + 1
            self._transient_refresh_fails[key.key_id] = n
            if n >= _MAX_TRANSIENT_REFRESH_FAILS:
                record refresh_failed(deactivate, transient_exhausted); return None   # escape valve
            record refresh_failed(cooldown, transient); self.cooldown(key, _REFRESH_RETRY_BACKOFF_SECONDS)
            return self._REFRESH_BLOCKED

        # --- got tokens: PERSIST FIRST (durability), then best-effort activation ---
        key.access_token = new_token; prev = key.expires_at; key.expires_at = new_expires
        if rotated: key.refresh_token = rotated
        try:
            await self._db.update_anthropic_oauth_tokens(
                key.key_id, new_token, new_expires, rotated,
                audit_event_type="refresh_succeeded", audit_decision="update_tokens", audit...)
        except Exception:
            logger.critical("rotated token not persisted for key %s; retrying once", key.key_id[:12])
            try: await self._db.update_anthropic_oauth_tokens(...same...)
            except Exception: logger.critical("persist retry failed; DB token is stale until next refresh")
        self._refresh_backoff.pop(key.key_id, None); self._transient_refresh_fails.pop(key.key_id, None)

        try:
            await activate_oauth_access_token(client, access_token=new_token, base_url=UPSTREAM_BASE)
        except Exception:
            record activation_failed(decision=note)          # token already persisted; do NOT discard
        return new_token
```

`self._defer(key, now_mono, seconds=_REFRESH_RETRY_BACKOFF_SECONDS)` sets
`self._refresh_backoff[key.key_id] = now_mono + seconds` and clears
`self._transient_refresh_fails[key.key_id]`.

`self._reread_token_if_changed(key)` re-reads the key row from the DB; if `refresh_token`/
`expires_at`/`access_token` differ from the in-memory object (another path rotated it), it updates
the in-memory fields and returns `True`; otherwise `False`.

### Manual dashboard refresh (`dashboard_api.py:329-372`)

Reorder to persist-before-activate so an activation failure cannot discard a rotated token:
`refresh_oauth_token` → `update_anthropic_oauth_tokens` (persist) → `pool.reload()` → best-effort
`activate_oauth_access_token` (log on failure, still return success — token is persisted and valid).
Serialization through the pool lock stays deferred.

### Behavior table (pool refresh, attempted inside the 5-min buffer)

| Refresh outcome | `token_valid`? | New behavior | Today |
|---|---|---|---|
| success | — | persist tokens, clear backoff/fail-count, best-effort activate, return new token | persist *after* activate; activation failure discards token |
| 429 | yes | keep serving, backoff `max(retry_after,60)s`, `refresh_deferred` | cooldown whole key |
| 429 | no | cooldown(retry_after), `_REFRESH_BLOCKED` | same |
| any failure (auth/network/timeout) | yes | keep serving, 60s backoff, `refresh_deferred`; **no deactivate** | invalid_grant→deactivate; network→uncaught escape |
| 4xx OAuth error (invalid_grant, invalid_client, 401/403…) | no | re-read DB; if unchanged → `refresh_failed`→ `None` → deactivate (re-auth); if DB newer → `_REFRESH_BLOCKED` | only literal invalid_grant deactivated; others uncaught |
| network/timeout/5xx | no | count transient; ≥5 → deactivate; else cooldown + `_REFRESH_BLOCKED`; **never** deactivate on a single transient | deactivate / uncaught escape |
| within refresh-backoff | yes | return access_token, no `/token` call | refreshes every request |

### Observability

- New event `refresh_deferred` (decision `reuse_valid_token`) — refresh failed but the key kept
  serving its still-valid access token.
- `activation_failed` decision changes `cooldown` → `note`: non-fatal (token already persisted and
  usable). This is the one previously-blocking path that becomes non-blocking — low risk, since
  activation is auxiliary warmup and the bearer token works without it.
- `refresh_failed` gains `transient_exhausted` (escape-valve deactivation) as an error variant.

## Error handling / safety

- `except Exception` catches `OAuthRefreshError`, other `RuntimeError`, and httpx transport/timeout
  errors that previously escaped uncaught; `logger.exception` ensures programming errors
  (Type/Attribute) are visible, not silently laundered as transient. `asyncio.CancelledError` (a
  `BaseException`, not `Exception`) is intentionally not swallowed; `shield` prevents it from
  aborting the rotation.
- Fatal vs transient is decided **only when truly expired**; while `token_valid` every failure
  defers (keep serving). Fatal = `OAuthRefreshError` with 4xx status (credentials rejected).
  Transient = 5xx / malformed-body / transport / timeout, bounded by the escape valve.
- Fundamental caveat: a *truly* lost `/token` response (Anthropic committed the rotation, the reply
  never arrived and was not merely cancelled) is unrecoverable — the replacement cannot be
  re-fetched. The fixes shrink that window to near-zero and remove both reproducible brick
  mechanisms; a genuine loss still ends the key at its natural expiry (re-auth), not early.
- Pre-existing, out of scope: an oauth key with `expires_at is None` never refreshes
  (`is_expired()` → False) and serves its access_token forever — unchanged by this work.

## Testing (TDD — write failing tests first)

Extend `tests/test_anthropic_oauth_refresh.py` (mock `_refresh_oauth_token` /
`activate_oauth_access_token`); mandatory unless noted:

1. **invalid_grant while token_valid** → returns `access_token`; not deactivated; backoff set;
   `refresh_deferred` recorded.
2. **network error (`httpx.ConnectError`) while token_valid** → returns `access_token`; not
   deactivated.
3. **4xx OAuth error while truly expired, DB unchanged** → returns `None` (deactivate).
4. **network/timeout while truly expired, single occurrence** → `_REFRESH_BLOCKED`, not `None`.
5. **transient while expired repeated `_MAX_TRANSIENT_REFRESH_FAILS` times** → deactivates (escape
   valve).
6. **within backoff + token_valid** → second call does not invoke `_refresh_oauth_token`; returns
   `access_token`.
7. **success** → `update_anthropic_oauth_tokens` called with rotated token; backoff + fail-count
   cleared; returns new token (regression).
8. **defect-2 regression (pool): `/token` succeeds, `activate_oauth_access_token` raises** → tokens
   **persisted** (`update_anthropic_oauth_tokens` called with rotated token); new token returned; no
   cooldown set (key still pickable); key not bricked.
9. **defect-2 regression (dashboard): activation raises** → `update_anthropic_oauth_tokens` was
   called (token persisted) and the endpoint reports success.
10. **cancellation (mandatory): caller cancelled while `_refresh_oauth_token` is in flight** → the
    refresh completes and persists (shield); assert `update_anthropic_oauth_tokens` called and the
    lock is released afterward (a subsequent `ensure_valid_token` proceeds).
11. **missing refresh_token while token_valid** → returns `access_token`, not deactivated (blocking
    #3); **while expired** → `None`.
12. **stale-object: invalid_grant while expired, but DB has a newer refresh_token** → returns
    `_REFRESH_BLOCKED` (re-read guard), not `None`.
13. **429 while token_valid** → returns `access_token`, key not fully cooled down.

Full suite (`pytest`) must stay green — especially `test_anthropic_oauth_refresh.py`,
`test_anthropic_proxy_oauth_usage_endpoint.py`, `test_oauth_window_tracking_e2e.py`,
`test_oauth_refresh_cli.py`, and any dashboard refresh test.
