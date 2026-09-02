# OAuth Refresh Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a single OAuth token refresh durable and crash-safe so a transient failure (lost/discarded rotated token, network error, cancellation) never bricks or prematurely deactivates a still-valid Anthropic key.

**Architecture:** Anthropic rotates the refresh token on every `/token` call (single-use; replacement only in the response body). The fix persists rotated tokens *before* the best-effort activation warmup, shields the pool refresh from cancellation, classifies refresh failures precisely, and keeps a key alive while its access token is still valid. All changes are local to `AnthropicKeyPool`, a new typed exception in `anthropic_oauth`, and a small reorder in the dashboard manual-refresh handler. Single process; the existing `asyncio.Lock` already serializes refreshers.

**Tech Stack:** Python 3.11+, aiohttp, httpx, asyncio; tests are `unittest` + `asyncio.run` / `IsolatedAsyncioTestCase` with the sqlite fallback DB (`connect_test_database`).

## Global Constraints

- Public contract of `AnthropicKeyPool.ensure_valid_token` is unchanged: returns `token: str` (use it), `None` (proxy deactivates; `oauth_usage` poller records `no_valid_oauth_token` without deactivating), or `pool._REFRESH_BLOCKED` (try next key / soft failure).
- No new third-party dependencies.
- `expires_at` is epoch **milliseconds** throughout.
- The full suite must stay green: `cd <repo> && python -m pytest -q`.
- Follow existing patterns: audit events via `_record_anthropic_event(db, key_id=..., event_type=..., ...)`; token persistence via `db.update_anthropic_oauth_tokens(key_id, access_token, expires_at, refresh_token, audit_...=...)`.
- Spec: `docs/superpowers/specs/2026-07-21-oauth-refresh-durability-design.md`.

---

### Task 1: Typed `OAuthRefreshError` in `anthropic_oauth`

**Files:**
- Modify: `src/smart_proxy/anthropic_oauth.py` (add class ~after imports; change `refresh_oauth_token` ~lines 176-182)
- Modify: `src/smart_proxy/anthropic_proxy.py` (add `OAuthRefreshError` to the existing `from smart_proxy.anthropic_oauth import (...)` block)
- Test: `tests/test_anthropic_oauth_refresh.py`

**Interfaces:**
- Produces: `class OAuthRefreshError(RuntimeError)` with attributes `status_code: int`, `error_code: str | None`. Raised by `refresh_oauth_token` (and therefore `anthropic_proxy._refresh_oauth_token`) on any non-2xx-non-429 response and on a 2xx with a malformed body (missing `access_token`). 429 still raises `httpx.HTTPStatusError`.

- [ ] **Step 1: Write the failing test** — append to `AnthropicOAuthRefreshTests` in `tests/test_anthropic_oauth_refresh.py`:

```python
    def test_refresh_raises_typed_error_on_4xx(self) -> None:
        from smart_proxy.anthropic_oauth import OAuthRefreshError

        async def run() -> None:
            client = _FakeAsyncClient(
                {"error": "invalid_grant", "error_description": "bad"},
                status_code=400,
            )
            with self.assertRaises(OAuthRefreshError) as ctx:
                await _refresh_oauth_token(client, refresh_token="r", client_id="c")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.error_code, "invalid_grant")
            self.assertIn("HTTP 400", str(ctx.exception))
            self.assertIsInstance(ctx.exception, RuntimeError)  # backward compat

        asyncio.run(run())

    def test_refresh_raises_typed_error_on_malformed_body(self) -> None:
        from smart_proxy.anthropic_oauth import OAuthRefreshError

        async def run() -> None:
            client = _FakeAsyncClient({"no_token": True}, status_code=200)
            with self.assertRaises(OAuthRefreshError) as ctx:
                await _refresh_oauth_token(client, refresh_token="r", client_id="c")
            self.assertEqual(ctx.exception.status_code, 200)

        asyncio.run(run())
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -k typed_error -v`
Expected: FAIL with `ImportError: cannot import name 'OAuthRefreshError'`.

- [ ] **Step 3: Add the exception class** in `src/smart_proxy/anthropic_oauth.py`, immediately after the imports / before `build_refresh_payload`:

```python
class OAuthRefreshError(RuntimeError):
    """Raised when the ``/token`` refresh endpoint returns a non-2xx (non-429)
    response, or a 2xx with a malformed body. Subclasses ``RuntimeError`` so
    existing ``except RuntimeError`` callers keep working. ``status_code`` is the
    HTTP status (200 for a 2xx-with-bad-body); ``error_code`` is the parsed OAuth
    ``error`` field when present."""

    def __init__(self, message: str, *, status_code: int, error_code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
```

- [ ] **Step 4: Raise it in `refresh_oauth_token`** — replace the current `if r.status_code >= 400:` / `if not access_token:` blocks (`src/smart_proxy/anthropic_oauth.py:176-182`) with:

```python
    if r.status_code >= 400:
        try:
            error_code = r.json().get("error")
        except Exception:
            error_code = None
        raise OAuthRefreshError(
            f"OAuth refresh failed HTTP {r.status_code}: {r.text[:300]}",
            status_code=r.status_code,
            error_code=error_code,
        )

    data = r.json()
    access_token = data.get("access_token", "")
    if not access_token:
        raise OAuthRefreshError(
            f"No access_token in refresh response: {data}",
            status_code=r.status_code,
            error_code=data.get("error"),
        )
```

- [ ] **Step 5: Export it into `anthropic_proxy`** — add `OAuthRefreshError` to the existing `from smart_proxy.anthropic_oauth import (` block in `src/smart_proxy/anthropic_proxy.py` (the block that already imports `activate_oauth_access_token`, `refresh_oauth_token`, `normalize_scope`).

- [ ] **Step 6: Run tests, verify pass**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -v`
Expected: PASS (new typed-error tests pass; existing pass — the 400 body already contained `invalid_grant`, and the success/activation tests are unaffected).

- [ ] **Step 7: Commit**

```bash
git add src/smart_proxy/anthropic_oauth.py src/smart_proxy/anthropic_proxy.py tests/test_anthropic_oauth_refresh.py
git commit -m "feat(oauth): typed OAuthRefreshError with status_code/error_code"
```

---

### Task 2: Dashboard manual refresh — persist before activation

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (add module logger near top; reorder `_api_anthropic_key_refresh`, ~lines 329-372)
- Test: `tests/test_dashboard_anthropic_keys_api.py` (update two existing tests)

**Interfaces:**
- Consumes: `db.update_anthropic_oauth_tokens(...)`, `anthropic_oauth.refresh_oauth_token`, `anthropic_oauth.activate_oauth_access_token`.
- Produces: manual refresh now persists the rotated token before activation; activation failure is logged and returns HTTP 200 (token saved), not 502.

- [ ] **Step 1: Update the two failing tests** in `tests/test_dashboard_anthropic_keys_api.py`. Replace `test_activation_failure_502_nothing_saved` (lines 368-379) and `test_activation_transport_error_502_nothing_saved` (lines 381-393) with the new expected behavior:

```python
    async def test_activation_failure_still_saves_token_and_reloads(self) -> None:
        app, db, pool = self._refresh_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/refresh", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        import smart_proxy.anthropic_oauth as ao
        with patch.object(ao, "refresh_oauth_token",
                          AsyncMock(return_value=("new-at", 1795000000000, "new-rt"))), \
             patch.object(ao, "activate_oauth_access_token",
                          AsyncMock(side_effect=RuntimeError("activation 403"))):
            resp = await dashboard_api._api_anthropic_key_refresh(req)
        # Activation is best-effort warmup; the rotated token must be persisted.
        self.assertEqual(resp.status, 200)
        args = db.update_anthropic_oauth_tokens.await_args
        self.assertEqual(args.args, ("key-1", "new-at", 1795000000000, "new-rt"))
        pool.reload.assert_awaited_once()

    async def test_activation_transport_error_still_saves_token(self) -> None:
        app, db, pool = self._refresh_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/refresh", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        import httpx
        import smart_proxy.anthropic_oauth as ao
        with patch.object(ao, "refresh_oauth_token",
                          AsyncMock(return_value=("new-at", 1795000000000, None))), \
             patch.object(ao, "activate_oauth_access_token",
                          AsyncMock(side_effect=httpx.ConnectError("connection refused"))):
            resp = await dashboard_api._api_anthropic_key_refresh(req)
        self.assertEqual(resp.status, 200)
        db.update_anthropic_oauth_tokens.assert_awaited_once()
```

Note: `test_refresh_failure_502_nothing_saved` (the `/token` call itself failing → 502, nothing saved) is unchanged and must still pass.

- [ ] **Step 2: Run, verify failure**

Run: `cd <repo> && python -m pytest tests/test_dashboard_anthropic_keys_api.py -k "activation" -v`
Expected: FAIL (current handler returns 502 and does not save on activation failure).

- [ ] **Step 3: Add a module logger** to `src/smart_proxy/dashboard_api.py`. After `from __future__ import annotations`, add:

```python
import logging
```

and after the imports block (near `_STATIC_APP_DIR = ...`):

```python
logger = logging.getLogger(__name__)
```

- [ ] **Step 4: Reorder the handler.** In `_api_anthropic_key_refresh`, replace the activation-then-persist tail (`src/smart_proxy/dashboard_api.py:358-372`) so persistence happens immediately after a successful `/token` call and activation is best-effort:

```python
    # Persist the rotated token immediately — activation is best-effort warmup and
    # must never cause a rotated (single-use) refresh token to be discarded.
    await request.app["db"].update_anthropic_oauth_tokens(
        row["id"], new_token, new_expires, rotated_refresh,
        audit_source="dashboard",
        audit_event_type="refresh_succeeded",
        audit_decision="update_tokens",
    )
    await request.app["anthropic_pool"].reload()

    try:
        await anthropic_oauth.activate_oauth_access_token(
            client, access_token=new_token, base_url=UPSTREAM_BASE
        )
    except Exception as exc:  # warmup only; token is already saved and valid
        logger.warning(
            "OAuth activation after manual refresh failed for %s: %s", row["id"][:12], exc
        )

    return web.json_response({"ok": True, "expires_at": new_expires})
```

(The `except Exception` around `refresh_oauth_token` returning 502 stays as-is — a failed `/token` call still means nothing to save.)

- [ ] **Step 5: Run tests, verify pass**

Run: `cd <repo> && python -m pytest tests/test_dashboard_anthropic_keys_api.py -v`
Expected: PASS (including `test_refresh_ok_saves_and_reloads` and `test_refresh_failure_502_nothing_saved`).

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_anthropic_keys_api.py
git commit -m "fix(dashboard): persist rotated OAuth token before best-effort activation"
```

---

### Task 3: Pool refresh — persist before activation, activation non-fatal

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — the success/activation tail of `ensure_valid_token` (~lines 480-534)
- Test: `tests/test_anthropic_oauth_refresh.py` (rewrite `test_refresh_blocks_key_when_activation_requests_fail`)

**Interfaces:**
- Consumes: `db.update_anthropic_oauth_tokens`, `activate_oauth_access_token`, `_record_anthropic_event`.
- Produces: after a successful `/token` call, the pool persists tokens first, then runs activation as best-effort; activation failure records an `activation_failed` event with `decision="note"` and returns the new token (no cooldown, no `_REFRESH_BLOCKED`).

- [ ] **Step 1: Rewrite the existing activation test** in `tests/test_anthropic_oauth_refresh.py`. Replace `test_refresh_blocks_key_when_activation_requests_fail` (lines 120-162) with:

```python
    def test_activation_failure_persists_rotated_token_and_returns_it(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    await db.insert_anthropic_key(
                        id="key-2", key_type="oauth",
                        access_token="old-access", refresh_token="old-refresh",
                        client_id="client-id-123", expires_at=0,
                        scopes='["user:profile","user:inference"]', name="test-key-fail",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()
                    key = pool.pick()
                    assert key is not None

                    client = _FakeAsyncClientActivationFail(
                        {"access_token": "new-access", "refresh_token": "new-refresh",
                         "expires_in": 28800})
                    token = await pool.ensure_valid_token(key, client)

                    # Activation warmup failed, but the rotated token is durable.
                    self.assertEqual(token, "new-access")
                    self.assertEqual(key.access_token, "new-access")
                    self.assertEqual(key.refresh_token, "new-refresh")

                    row = await db.get_anthropic_key("key-2")
                    assert row is not None
                    self.assertEqual(row["access_token"], "new-access")
                    self.assertEqual(row["refresh_token"], "new-refresh")
                    self.assertEqual(row["status"], "active")

                    events = [e["event_type"] for e in await db.list_anthropic_key_events("key-2")]
                    self.assertEqual(events, ["refresh_attempt", "refresh_succeeded", "activation_failed"])
                finally:
                    await db.close()

        asyncio.run(run())
```

- [ ] **Step 2: Run, verify failure**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -k activation_failure_persists -v`
Expected: FAIL (current code returns `_REFRESH_BLOCKED`, DB stays `old-access`).

- [ ] **Step 3: Reorder the success/activation tail** of `ensure_valid_token` in `src/smart_proxy/anthropic_proxy.py`. Replace the block that currently runs activation (with `except RuntimeError` → cooldown → return `_REFRESH_BLOCKED`) *before* persistence (lines ~480-534) with persistence-first:

```python
            # Got new tokens. PERSIST FIRST so a rotated (single-use) refresh token is
            # never lost — activation below is auxiliary warmup and must not discard it.
            previous_expires_at = key.expires_at
            key.access_token = new_token
            key.expires_at = new_expires
            if rotated_refresh:
                key.refresh_token = rotated_refresh
            await self._db.update_anthropic_oauth_tokens(
                key.key_id,
                new_token,
                new_expires,
                rotated_refresh,
                audit_op_id=audit_op_id,
                audit_source=audit_source,
                audit_event_type="refresh_succeeded",
                audit_decision="update_tokens",
                audit_path=audit_path,
                audit_model=audit_model,
                audit_context={
                    "previous_expires_at": previous_expires_at,
                    "new_expires_at": new_expires,
                    "rotated_refresh_token": bool(rotated_refresh),
                },
            )
            logger.info(
                "Refreshed OAuth token for key %s, expires_at=%d",
                key.key_id[:12], new_expires,
            )

            try:
                await activate_oauth_access_token(
                    client, access_token=new_token, base_url=UPSTREAM_BASE,
                )
            except Exception as exc:  # warmup only; token already persisted and valid
                await _record_anthropic_event(
                    self._db,
                    key_id=key.key_id,
                    event_type="activation_failed",
                    op_id=audit_op_id,
                    source=audit_source,
                    decision="note",
                    path=audit_path,
                    model=audit_model,
                    error_type="activation_runtime_error",
                    error_message=str(exc),
                )
            return new_token
```

Delete the old pre-persist activation `try/except` block (the one recording `activation_failed` with `decision="cooldown"` and returning `self._REFRESH_BLOCKED`).

- [ ] **Step 4: Run tests, verify pass**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -v`
Expected: PASS — new activation test passes; `test_refresh_rotation_is_saved_to_same_db_row` still passes (event order `refresh_attempt, refresh_succeeded` unchanged; activation success records nothing).

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_oauth_refresh.py
git commit -m "fix(proxy): persist rotated OAuth token before best-effort activation"
```

---

### Task 4: Pool state, constants, helpers, reload pruning

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — module constants (near `_REFRESH_BUFFER_MS`, line 67); `AnthropicKeyPool.__init__` (lines 162-173); `reload` (lines 175-199); add module-level `_is_auth_fatal`, `_parse_retry_after`; add pool methods `_defer`, `_on_refresh_task_done`, `_reread_token_if_changed`
- Test: `tests/test_anthropic_oauth_refresh.py`

**Interfaces:**
- Produces:
  - Constants `_REFRESH_RETRY_BACKOFF_SECONDS = 60`, `_MAX_TRANSIENT_REFRESH_FAILS = 5`, `_TOKEN_VALID_FLOOR_MS = 30_000`.
  - Pool state `self._refresh_backoff: dict[str, float]`, `self._transient_refresh_fails: dict[str, int]`, `self._refresh_tasks: set[asyncio.Task]`.
  - `def _is_auth_fatal(exc: Exception) -> bool` — True iff `OAuthRefreshError` with 4xx status.
  - `def _parse_retry_after(exc: httpx.HTTPStatusError, default: int) -> int` — defensive int parse.
  - `self._defer(key, now_mono, seconds=_REFRESH_RETRY_BACKOFF_SECONDS)` — set refresh backoff, clear transient count.
  - `self._on_refresh_task_done(task)` — discard from `_refresh_tasks`, log an unretrieved exception.
  - `async self._reread_token_if_changed(key) -> bool` — re-read DB row; if refresh/access token or expiry differ, update in-memory key and return True.

- [ ] **Step 1: Write the failing tests** — new class in `tests/test_anthropic_oauth_refresh.py`:

```python
class RefreshHelperTests(unittest.TestCase):
    def test_is_auth_fatal_classification(self) -> None:
        import httpx
        from smart_proxy.anthropic_oauth import OAuthRefreshError
        from smart_proxy.anthropic_proxy import _is_auth_fatal
        self.assertTrue(_is_auth_fatal(OAuthRefreshError("x", status_code=400, error_code="invalid_grant")))
        self.assertTrue(_is_auth_fatal(OAuthRefreshError("x", status_code=401)))
        self.assertFalse(_is_auth_fatal(OAuthRefreshError("x", status_code=500)))
        self.assertFalse(_is_auth_fatal(RuntimeError("x")))
        self.assertFalse(_is_auth_fatal(httpx.ConnectError("x")))

    def test_parse_retry_after(self) -> None:
        from types import SimpleNamespace
        from smart_proxy.anthropic_proxy import _parse_retry_after
        good = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "120"}))
        junk = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "soon"}))
        missing = SimpleNamespace(response=SimpleNamespace(headers={}))
        self.assertEqual(_parse_retry_after(good, 60), 120)
        self.assertEqual(_parse_retry_after(junk, 60), 60)
        self.assertEqual(_parse_retry_after(missing, 60), 60)

    def test_reload_prunes_refresh_backoff(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    await db.insert_anthropic_key(
                        id="live", key_type="oauth", access_token="a", refresh_token="r",
                        client_id="c", expires_at=9999999999999,
                        scopes='["user:inference"]', name="live")
                    pool = AnthropicKeyPool(db)
                    import time as _t
                    pool._refresh_backoff = {"live": _t.monotonic() + 999, "dead": _t.monotonic() + 999}
                    await pool.reload()
                    self.assertIn("live", pool._refresh_backoff)
                    self.assertNotIn("dead", pool._refresh_backoff)  # not a live key → pruned
                finally:
                    await db.close()
        asyncio.run(run())
```

- [ ] **Step 2: Run, verify failure**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -k "RefreshHelper" -v`
Expected: FAIL with `ImportError`/`AttributeError` (`_is_auth_fatal`, `_parse_retry_after` not defined; backoff not pruned).

- [ ] **Step 3: Add the constants** near `_REFRESH_BUFFER_MS` (`src/smart_proxy/anthropic_proxy.py:67`):

```python
_REFRESH_RETRY_BACKOFF_SECONDS = 60   # after a failed refresh with a still-valid token
_MAX_TRANSIENT_REFRESH_FAILS = 5      # consecutive transient failures while expired → deactivate
_TOKEN_VALID_FLOOR_MS = 30_000        # only "keep serving" a token with >30s of real life left
```

- [ ] **Step 4: Add module-level helpers** (near `_refresh_oauth_token`, after line 103):

```python
def _is_auth_fatal(exc: Exception) -> bool:
    """A refresh failure that means the refresh token itself is rejected (4xx from
    /token) — terminal when the access token is also expired."""
    return isinstance(exc, OAuthRefreshError) and 400 <= exc.status_code < 500


def _parse_retry_after(exc: httpx.HTTPStatusError, default: int) -> int:
    try:
        return int(exc.response.headers.get("retry-after", str(default)))
    except (ValueError, TypeError):
        return default
```

- [ ] **Step 5: Add pool state** in `AnthropicKeyPool.__init__` (after `self._refresh_lock = asyncio.Lock()`, line 173):

```python
        self._refresh_backoff: dict[str, float] = {}       # key_id → monotonic deadline
        self._transient_refresh_fails: dict[str, int] = {}  # key_id → consecutive transient fails (expired)
        self._refresh_tasks: set[asyncio.Task] = set()      # strong refs to shielded refresh tasks
```

- [ ] **Step 6: Prune backoff in `reload`** — inside `reload`, after the `_model_cooldowns` rebuild (around line 192, using the existing `now` and `live_key_ids`):

```python
        self._refresh_backoff = {
            key_id: deadline
            for key_id, deadline in self._refresh_backoff.items()
            if key_id in live_key_ids and deadline > now
        }
        self._transient_refresh_fails = {
            key_id: n
            for key_id, n in self._transient_refresh_fails.items()
            if key_id in live_key_ids
        }
```

- [ ] **Step 7: Add the pool helper methods** to `AnthropicKeyPool` (place near `cooldown`):

```python
    def _defer(self, key: _AnthropicKey, now_mono: float, *, seconds: int = _REFRESH_RETRY_BACKOFF_SECONDS) -> None:
        """Park *refresh* (not the key) briefly after a failure while the access token
        is still valid, so we keep serving it without hammering /token every request."""
        self._refresh_backoff[key.key_id] = now_mono + seconds
        self._transient_refresh_fails.pop(key.key_id, None)

    def _on_refresh_task_done(self, task: asyncio.Task) -> None:
        self._refresh_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("shielded refresh task failed: %r", exc)

    async def _reread_token_if_changed(self, key: _AnthropicKey) -> bool:
        """Guard against stale in-memory key objects (reload() swaps instances): if the
        DB row now holds a different token/expiry, adopt it and report True so the caller
        retries instead of deactivating a key whose stored tokens are actually good."""
        row = await self._db.get_anthropic_key(key.key_id)
        if not row:
            return False
        if (row.get("refresh_token") != key.refresh_token
                or row.get("access_token") != key.access_token
                or row.get("expires_at") != key.expires_at):
            key.access_token = row.get("access_token")
            key.refresh_token = row.get("refresh_token")
            key.expires_at = row.get("expires_at")
            return True
        return False
```

- [ ] **Step 8: Run tests, verify pass**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -k "RefreshHelper" -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_oauth_refresh.py
git commit -m "feat(proxy): refresh backoff/transient state, classification helpers, reload pruning"
```

---

### Task 5: Pool refresh failure path — keep-alive, classification, escape valve, re-read, shield

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — rewrite `ensure_valid_token` into a shield wrapper + `_refresh_locked` (lines ~373-534)
- Test: `tests/test_anthropic_oauth_refresh.py`

**Interfaces:**
- Consumes: Task 1 `OAuthRefreshError`; Task 4 constants, `_is_auth_fatal`, `_parse_retry_after`, `_defer`, `_on_refresh_task_done`, `_reread_token_if_changed`, and pool state.
- Produces: `ensure_valid_token` returns `await asyncio.shield(<task running _refresh_locked>)`; `_refresh_locked` implements the full state machine (keep-alive-while-valid, 60s backoff, fatal-vs-transient classification, 5-strike escape valve, re-read-before-deactivate). Public return contract unchanged.

- [ ] **Step 1: Add test fake clients** at the top of `tests/test_anthropic_oauth_refresh.py` (after `_FakeAsyncClientActivationFail`):

```python
class _FakeAsyncClientRefreshRaises(_FakeAsyncClient):
    """`.post` (the /token call) raises the given exception; activation still 200."""
    def __init__(self, exc: Exception) -> None:
        super().__init__({}, 200)
        self._exc = exc

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        raise self._exc


def _future_ms(minutes: float) -> int:
    import time as _t
    return int(_t.time() * 1000) + int(minutes * 60_000)
```

- [ ] **Step 2: Write the failing behavior tests** — new class in `tests/test_anthropic_oauth_refresh.py`. Uses a helper to build a pool+key with a chosen `expires_at`:

```python
class RefreshFailurePolicyTests(unittest.TestCase):
    async def _pool_with_key(self, db, expires_at: int):
        await db.insert_anthropic_key(
            id="k", key_type="oauth", access_token="valid-access", refresh_token="old-refresh",
            client_id="client-id-123", expires_at=expires_at,
            scopes='["user:inference"]', name="k")
        pool = AnthropicKeyPool(db)
        await pool.reload()
        key = pool.pick()
        assert key is not None
        return pool, key

    def test_invalid_grant_while_valid_keeps_serving(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, _future_ms(2))  # inside 5-min buffer, still valid
                    client = _FakeAsyncClient(
                        {"error": "invalid_grant", "error_description": "bad"}, status_code=400)
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, "valid-access")            # kept serving
                    self.assertIn("k", pool._refresh_backoff)          # backed off
                    row = await db.get_anthropic_key("k")
                    self.assertEqual(row["status"], "active")          # NOT deactivated
                    events = [e["event_type"] for e in await db.list_anthropic_key_events("k")]
                    self.assertIn("refresh_deferred", events)
                finally:
                    await db.close()
        asyncio.run(run())

    def test_network_error_while_valid_keeps_serving(self) -> None:
        async def run() -> None:
            import httpx
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, _future_ms(2))
                    client = _FakeAsyncClientRefreshRaises(httpx.ConnectError("boom"))
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, "valid-access")
                    row = await db.get_anthropic_key("k")
                    self.assertEqual(row["status"], "active")
                finally:
                    await db.close()
        asyncio.run(run())

    def test_invalid_grant_while_expired_returns_none(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)  # truly expired
                    client = _FakeAsyncClient(
                        {"error": "invalid_grant"}, status_code=400)
                    token = await pool.ensure_valid_token(key, client)
                    self.assertIsNone(token)  # caller deactivates
                finally:
                    await db.close()
        asyncio.run(run())

    def test_network_while_expired_blocks_not_none(self) -> None:
        async def run() -> None:
            import httpx
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)
                    client = _FakeAsyncClientRefreshRaises(httpx.ConnectError("boom"))
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, pool._REFRESH_BLOCKED)  # not None → not deactivated
                finally:
                    await db.close()
        asyncio.run(run())

    def test_transient_escape_valve_deactivates_after_max(self) -> None:
        async def run() -> None:
            import httpx
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    from smart_proxy.anthropic_proxy import _MAX_TRANSIENT_REFRESH_FAILS
                    pool, key = await self._pool_with_key(db, 0)
                    client = _FakeAsyncClientRefreshRaises(httpx.ConnectError("boom"))
                    result = None
                    for _ in range(_MAX_TRANSIENT_REFRESH_FAILS):
                        pool._cooldowns.pop("k", None)  # clear the per-attempt cooldown so we can retry
                        result = await pool.ensure_valid_token(key, client)
                    self.assertIsNone(result)  # escape valve → deactivate
                finally:
                    await db.close()
        asyncio.run(run())

    def test_backoff_suppresses_second_refresh(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, _future_ms(2))
                    client = _FakeAsyncClient({"error": "invalid_grant"}, status_code=400)
                    await pool.ensure_valid_token(key, client)   # 1st call → 1 /token call, sets backoff
                    await pool.ensure_valid_token(key, client)   # 2nd call → within backoff, no /token call
                    self.assertEqual(len(client.calls), 1)
                finally:
                    await db.close()
        asyncio.run(run())

    def test_missing_refresh_token_while_valid_keeps_serving(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    await db.insert_anthropic_key(
                        id="k", key_type="oauth", access_token="valid-access", refresh_token=None,
                        client_id="c", expires_at=_future_ms(2), scopes='["user:inference"]', name="k")
                    pool = AnthropicKeyPool(db); await pool.reload(); key = pool.pick()
                    token = await pool.ensure_valid_token(key, _FakeAsyncClient({}))
                    self.assertEqual(token, "valid-access")
                    self.assertEqual((await db.get_anthropic_key("k"))["status"], "active")
                finally:
                    await db.close()
        asyncio.run(run())

    def test_stale_object_invalid_grant_rereads_and_blocks(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)  # expired
                    # Another path already rotated the token in the DB:
                    await db.update_anthropic_oauth_tokens("k", "fresher-access", _future_ms(60), "fresher-refresh")
                    client = _FakeAsyncClient({"error": "invalid_grant"}, status_code=400)
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, pool._REFRESH_BLOCKED)   # re-read found newer token → don't deactivate
                    self.assertEqual(key.refresh_token, "fresher-refresh")
                finally:
                    await db.close()
        asyncio.run(run())

    def test_cancellation_still_persists(self) -> None:
        async def run() -> None:
            import contextlib
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)
                    real_client = _FakeAsyncClient(
                        {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800})

                    import smart_proxy.anthropic_proxy as ap
                    orig = ap._refresh_oauth_token
                    async def slow(*a, **k):
                        await asyncio.sleep(0.05)
                        return await orig(*a, **k)
                    with patch.object(ap, "_refresh_oauth_token", slow):
                        outer = asyncio.ensure_future(pool.ensure_valid_token(key, real_client))
                        await asyncio.sleep(0.01)     # let it enter the shielded refresh
                        outer.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await outer
                        await asyncio.sleep(0.15)     # let the shielded refresh finish
                    row = await db.get_anthropic_key("k")
                    self.assertEqual(row["access_token"], "new-access")  # persisted despite cancel
                    # lock released — a subsequent call proceeds:
                    self.assertFalse(pool._refresh_lock.locked())
                finally:
                    await db.close()
        asyncio.run(run())
```

Add `from unittest.mock import patch` to the imports at the top of the test file if not present.

- [ ] **Step 3: Run, verify failure**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -k "RefreshFailurePolicy" -v`
Expected: FAIL (current code deactivates on invalid_grant regardless of validity; network errors escape uncaught; no backoff/shield).

- [ ] **Step 4: Rewrite `ensure_valid_token` + add `_refresh_locked`.** Replace the whole `ensure_valid_token` method (`src/smart_proxy/anthropic_proxy.py:373-534`) with the shield wrapper plus `_refresh_locked`:

```python
    async def ensure_valid_token(
        self,
        key: _AnthropicKey,
        client: httpx.AsyncClient,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
    ) -> str | None:
        """Return a valid token string, refreshing OAuth if needed.

        Returns ``_REFRESH_BLOCKED`` when refresh is temporarily unavailable
        (rate-limited / transient) — caller should try the next key, NOT deactivate.
        Returns ``None`` only when the key is genuinely unusable (caller deactivates).
        The refresh is shielded from cancellation so an inbound disconnect cannot abort
        a rotation half-done (Anthropic rotates the refresh token as a side effect)."""
        if key.key_type == "api_key":
            return key.api_key
        if not key.is_expired():
            return key.access_token

        task = asyncio.ensure_future(
            self._refresh_locked(
                key, client,
                audit_op_id=audit_op_id, audit_source=audit_source,
                audit_path=audit_path, audit_model=audit_model,
            )
        )
        self._refresh_tasks.add(task)
        task.add_done_callback(self._on_refresh_task_done)
        return await asyncio.shield(task)

    async def _refresh_locked(
        self,
        key: _AnthropicKey,
        client: httpx.AsyncClient,
        *,
        audit_op_id: str,
        audit_source: str,
        audit_path: str,
        audit_model: str | None,
    ) -> str | None:
        async with self._refresh_lock:
            if not key.is_expired():
                return key.access_token

            now_mono = time.monotonic()
            now_ms = int(time.time() * 1000)
            token_valid = (
                bool(key.access_token)
                and key.expires_at is not None
                and now_ms < key.expires_at - _TOKEN_VALID_FLOOR_MS
            )

            if token_valid and now_mono < self._refresh_backoff.get(key.key_id, 0.0):
                return key.access_token

            await _record_anthropic_event(
                self._db, key_id=key.key_id, event_type="refresh_attempt",
                op_id=audit_op_id, source=audit_source, decision="refresh",
                path=audit_path, model=audit_model,
                context={
                    "expires_at": key.expires_at,
                    "has_refresh_token": bool(key.refresh_token),
                    "scopes": normalize_scope(key.scopes),
                },
            )

            if not key.refresh_token:
                if token_valid:
                    self._defer(key, now_mono)
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_deferred",
                        op_id=audit_op_id, source=audit_source, decision="reuse_valid_token",
                        path=audit_path, model=audit_model,
                        error_type="missing_refresh_token",
                        error_message="No refresh_token; serving still-valid access token",
                    )
                    return key.access_token
                await _record_anthropic_event(
                    self._db, key_id=key.key_id, event_type="refresh_failed",
                    op_id=audit_op_id, source=audit_source, decision="deactivate",
                    path=audit_path, model=audit_model,
                    error_type="missing_refresh_token",
                    error_message="OAuth key has no refresh_token",
                )
                return None

            try:
                new_token, new_expires, rotated_refresh = await _refresh_oauth_token(
                    client, key.refresh_token, key.client_id,
                    scope=normalize_scope(key.scopes),
                )
            except httpx.HTTPStatusError as exc:  # 429
                retry_after = _parse_retry_after(exc, _REFRESH_RETRY_BACKOFF_SECONDS)
                if token_valid:
                    self._defer(key, now_mono, seconds=max(retry_after, _REFRESH_RETRY_BACKOFF_SECONDS))
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_deferred",
                        op_id=audit_op_id, source=audit_source, decision="reuse_valid_token",
                        path=audit_path, model=audit_model, http_status=429,
                        error_type="rate_limited", retry_after=retry_after,
                        error_message="Refresh rate-limited; serving still-valid access token",
                    )
                    return key.access_token
                await _record_anthropic_event(
                    self._db, key_id=key.key_id, event_type="refresh_rate_limited",
                    op_id=audit_op_id, source=audit_source, decision="cooldown",
                    path=audit_path, model=audit_model, http_status=429,
                    error_type="rate_limited", retry_after=retry_after,
                    error_message="Refresh rate-limited",
                )
                self.cooldown(key, retry_after)
                return self._REFRESH_BLOCKED
            except Exception as exc:  # OAuthRefreshError | httpx transport/timeout | ...
                logger.exception("OAuth refresh failed for key %s", key.key_id[:12])
                if token_valid:
                    self._defer(key, now_mono)
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_deferred",
                        op_id=audit_op_id, source=audit_source, decision="reuse_valid_token",
                        path=audit_path, model=audit_model,
                        error_type="refresh_runtime_error", error_message=str(exc),
                    )
                    return key.access_token
                # Truly expired: decide fatal vs transient.
                if _is_auth_fatal(exc):
                    if await self._reread_token_if_changed(key):
                        return self._REFRESH_BLOCKED  # DB had a newer token; retry next cycle
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_failed",
                        op_id=audit_op_id, source=audit_source, decision="deactivate",
                        path=audit_path, model=audit_model,
                        error_type="refresh_runtime_error", error_message=str(exc),
                    )
                    return None
                n = self._transient_refresh_fails.get(key.key_id, 0) + 1
                self._transient_refresh_fails[key.key_id] = n
                if n >= _MAX_TRANSIENT_REFRESH_FAILS:
                    self._transient_refresh_fails.pop(key.key_id, None)
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_failed",
                        op_id=audit_op_id, source=audit_source, decision="deactivate",
                        path=audit_path, model=audit_model,
                        error_type="transient_exhausted", error_message=str(exc),
                    )
                    return None
                await _record_anthropic_event(
                    self._db, key_id=key.key_id, event_type="refresh_failed",
                    op_id=audit_op_id, source=audit_source, decision="cooldown",
                    path=audit_path, model=audit_model,
                    error_type="refresh_transient", error_message=str(exc),
                )
                self.cooldown(key, _REFRESH_RETRY_BACKOFF_SECONDS)
                return self._REFRESH_BLOCKED

            # Got new tokens. PERSIST FIRST (durability), then best-effort activation.
            previous_expires_at = key.expires_at
            key.access_token = new_token
            key.expires_at = new_expires
            if rotated_refresh:
                key.refresh_token = rotated_refresh
            try:
                await self._db.update_anthropic_oauth_tokens(
                    key.key_id, new_token, new_expires, rotated_refresh,
                    audit_op_id=audit_op_id, audit_source=audit_source,
                    audit_event_type="refresh_succeeded", audit_decision="update_tokens",
                    audit_path=audit_path, audit_model=audit_model,
                    audit_context={
                        "previous_expires_at": previous_expires_at,
                        "new_expires_at": new_expires,
                        "rotated_refresh_token": bool(rotated_refresh),
                    },
                )
            except Exception:
                logger.critical("rotated token not persisted for key %s; retrying once", key.key_id[:12])
                try:
                    await self._db.update_anthropic_oauth_tokens(
                        key.key_id, new_token, new_expires, rotated_refresh,
                        audit_source=audit_source, audit_event_type="refresh_succeeded",
                        audit_decision="update_tokens",
                    )
                except Exception:
                    logger.critical("persist retry failed for key %s; DB token stale until next refresh", key.key_id[:12])
            self._refresh_backoff.pop(key.key_id, None)
            self._transient_refresh_fails.pop(key.key_id, None)

            try:
                await activate_oauth_access_token(
                    client, access_token=new_token, base_url=UPSTREAM_BASE,
                )
            except Exception as exc:  # warmup only; token already persisted and valid
                await _record_anthropic_event(
                    self._db, key_id=key.key_id, event_type="activation_failed",
                    op_id=audit_op_id, source=audit_source, decision="note",
                    path=audit_path, model=audit_model,
                    error_type="activation_runtime_error", error_message=str(exc),
                )
            logger.info("Refreshed OAuth token for key %s, expires_at=%d", key.key_id[:12], new_expires)
            return new_token
```

This supersedes Task 3's tail edit (Task 3 kept the deactivate-on-invalid_grant path; this version replaces the whole method). The `_REFRESH_BLOCKED` sentinel and `cooldown` / `_record_anthropic_event` usages are unchanged from the existing code.

- [ ] **Step 5: Run the failure-policy tests, verify pass**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py -k "RefreshFailurePolicy" -v`
Expected: PASS (all 9 behaviors, including cancellation).

- [ ] **Step 6: Run the full refresh + related suites**

Run: `cd <repo> && python -m pytest tests/test_anthropic_oauth_refresh.py tests/test_anthropic_proxy_oauth_usage_endpoint.py tests/test_oauth_window_tracking_e2e.py tests/test_oauth_refresh_cli.py -v`
Expected: PASS. (`test_scheduled_smoke_pass_logs_refresh_failure_before_deactivation` still passes: expired + invalid_grant, DB unchanged on re-read → deactivate, events `refresh_attempt, refresh_failed, status_change`.)

- [ ] **Step 7: Run the entire suite**

Run: `cd <repo> && python -m pytest -q`
Expected: PASS (all green).

- [ ] **Step 8: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_oauth_refresh.py
git commit -m "fix(proxy): keep still-valid keys alive on refresh failure; shield refresh from cancellation"
```

---

## Notes for the implementer

- **Deferred (do NOT implement here):** serializing the manual dashboard Refresh through the pool lock; a background pre-expiry refresher; stopping the `oauth_usage` poller from refreshing; the request-time upstream-401 deactivation path.
- **`expires_at` is milliseconds.** `_future_ms(2)` puts a token 2 min out: inside the 5-min `is_expired()` buffer (so a refresh is attempted) yet `token_valid` (>30s of real life). `expires_at=0` is "truly expired".
- **Why Task 3 then Task 5 both touch the same tail:** Task 3 is the minimal, independently-reviewable defect-2 fix (persist-before-activate) that stands alone if Task 5 is deferred; Task 5 supersedes it with the full failure-path state machine. Keep the commits separate so a reviewer can accept the durability reorder independently of the keep-alive policy.
- **Sanity after all tasks:** `python -m pytest -q` green, and `git log --oneline -5` shows the five commits.
```
