# Anthropic Keys Tab + OAuth Login Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "Anthropic" tab to the dashboard SPA that lists `anthropic_keys` with full management (add OAuth session, enable/disable, rename, soft-delete, force refresh), backed by new `/api/anthropic/*` JSON endpoints that share the OAuth exchange core with the legacy `/_oauth/login` flow.

**Architecture:** Extract the code-for-token exchange from `_oauth_run_code_exchange` in `anthropic_proxy.py` into a shared helper raising a typed error; the legacy HTML flow becomes a thin wrapper over it. New JSON endpoints live in `dashboard_api.py` (lazy-importing from `anthropic_proxy` to avoid the existing circular-import, same as `_api_oauth_usage` does). Frontend is a new Svelte 5 view wired as a tab in `App.svelte`.

**Tech Stack:** Python 3 / aiohttp / httpx, unittest.IsolatedAsyncioTestCase + `aiohttp.test_utils.make_mocked_request`, Svelte 5 (runes) + Vite.

**Spec:** `docs/superpowers/specs/2026-07-18-anthropic-keys-tab-design.md`

## Global Constraints

- Work in the git worktree `<repo>/.claude/worktrees/anthropic-keys-tab` on branch `worktree-anthropic-keys-tab`. Never touch the main checkout at `<repo>` (another session works there).
- Run Python tests with the main checkout's venv (it has all deps; test files prepend their own `src/` to `sys.path`, so worktree code is what gets imported):
  `<repo>/.venv/bin/python -m pytest tests/<file> -q`
- API responses must NEVER include token material: `access_token`, `refresh_token`, `api_key` values.
- Read endpoints guard with `_dashboard_authorized`; all mutating endpoints guard with `_action_authorized` (both already exist in `dashboard_api.py`).
- `dashboard_api.py` must not import `smart_proxy.anthropic_proxy` at module level (circular import — `anthropic_proxy` imports `register_dashboard_api`). Import it lazily inside handlers, as `_api_oauth_usage` already does. Importing `smart_proxy.anthropic_oauth` at module level is fine.
- Soft delete only: `status='deleted'`. No DB schema migration exists or is needed (the pool already loads only `status IN ('active','low_balance')`).
- Legacy flow (`/_oauth/login`, `/_oauth/submit`, `/callback`) must keep working: same routes, same status codes, HTML success page still includes the BroadcastChannel notify script.
- Every commit message ends with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: DB method `set_anthropic_key_name`

**Files:**
- Modify: `src/smart_proxy/db.py` (insert after `set_anthropic_key_status`, which ends near line 1407, right before `list_anthropic_keys`)
- Test: `tests/test_anthropic_key_rename_db.py` (new)

**Interfaces:**
- Consumes: existing `Database.get_anthropic_key(key_id) -> dict | None`, `Database.insert_anthropic_key(...)`.
- Produces: `async def set_anthropic_key_name(self, key_id: str, name: str) -> bool` — `True` if the row existed and was renamed, `False` if unknown id. Task 5's rename endpoint calls this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anthropic_key_rename_db.py`:

```python
# tests/test_anthropic_key_rename_db.py
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.db_test_utils import connect_test_database  # noqa: E402


class SetAnthropicKeyNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = await connect_test_database(
            sqlite_fallback_path=str(Path(self._tmp.name) / "smart-proxy.db")
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_rename_updates_name_and_updated_at(self) -> None:
        await self.db.insert_anthropic_key(
            id="k-rename-1", key_type="oauth",
            access_token="at", refresh_token="rt", name="old-name",
        )
        before = await self.db.get_anthropic_key("k-rename-1")
        ok = await self.db.set_anthropic_key_name("k-rename-1", "new-name")
        self.assertTrue(ok)
        row = await self.db.get_anthropic_key("k-rename-1")
        self.assertEqual(row["name"], "new-name")
        self.assertGreaterEqual(row["updated_at"], before["updated_at"])

    async def test_rename_unknown_id_returns_false(self) -> None:
        ok = await self.db.set_anthropic_key_name("missing-id", "x")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `<repo>/.venv/bin/python -m pytest tests/test_anthropic_key_rename_db.py -q`
Expected: FAIL / ERROR with `AttributeError: 'Database' object has no attribute 'set_anthropic_key_name'`

- [ ] **Step 3: Write minimal implementation**

In `src/smart_proxy/db.py`, directly after the `set_anthropic_key_status` method (before `list_anthropic_keys`), add:

```python
    async def set_anthropic_key_name(self, key_id: str, name: str) -> bool:
        """Rename an Anthropic key. Returns False when the id does not exist."""
        if await self.get_anthropic_key(key_id) is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE anthropic_keys SET name = ?, updated_at = ? WHERE id = ?",
            (name, now, key_id),
        )
        await self.db.commit()
        return True
```

(`datetime` / `timezone` are already imported at the top of `db.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `<repo>/.venv/bin/python -m pytest tests/test_anthropic_key_rename_db.py -q`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_anthropic_key_rename_db.py
git commit -m "feat(db): set_anthropic_key_name for dashboard rename

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Shared OAuth exchange core in `anthropic_proxy.py`

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — replace `_oauth_run_code_exchange` (currently lines ~2620–2718) with `OAuthExchangeError` + `_oauth_exchange_and_store` + a thin wrapper.
- Test: `tests/test_oauth_login_shared_core.py` (new)

**Interfaces:**
- Consumes: existing module globals `TOKEN_URL`, `CLAUDE_OAUTH_CLIENT_ID`, `exchange_authorization_code`, `_oauth_broadcast_to_manual_tab_script`, `_OAUTH_LOGIN_SESSION_TTL_SEC`; app keys `_oauth_login_sessions`, `http_client`, `db`, `anthropic_pool`.
- Produces (Task 4 depends on these exact names):
  - `class OAuthExchangeError(Exception)` with attributes `status: int`, `message: str`.
  - `async def _oauth_exchange_and_store(app, *, code: str, state: str) -> dict` returning `{"key_id": str, "name": str}`.
  - `_oauth_run_code_exchange(request, code, state)` keeps its existing signature/behavior (legacy HTML flow).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_oauth_login_shared_core.py`:

```python
# tests/test_oauth_login_shared_core.py
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from smart_proxy import anthropic_proxy  # noqa: E402


def _app(sessions: dict | None = None) -> dict:
    db = MagicMock()
    db.insert_anthropic_key = AsyncMock()
    pool = MagicMock()
    pool.reload = AsyncMock()
    return {
        "_oauth_login_sessions": {} if sessions is None else sessions,
        "http_client": MagicMock(),
        "db": db,
        "anthropic_pool": pool,
    }


def _session() -> dict:
    return {
        "st": {
            "verifier": "ver", "name": "work-acct",
            "redirect_uri": "http://localhost:8090/callback", "ts": 0,
        }
    }


_TOKEN_DATA = {
    "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
    "scope": "user:inference user:profile",
    "organization": {"organization_type": "claude_max", "rate_limit_tier": "max_20x"},
}


class ExchangeAndStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_code_raises_400(self) -> None:
        with self.assertRaises(anthropic_proxy.OAuthExchangeError) as ctx:
            await anthropic_proxy._oauth_exchange_and_store(_app(), code="", state="st")
        self.assertEqual(ctx.exception.status, 400)

    async def test_unknown_state_raises_400(self) -> None:
        with self.assertRaises(anthropic_proxy.OAuthExchangeError) as ctx:
            await anthropic_proxy._oauth_exchange_and_store(_app(), code="c", state="nope")
        self.assertEqual(ctx.exception.status, 400)

    async def test_happy_path_inserts_row_and_reloads_pool(self) -> None:
        sessions = _session()
        app = _app(sessions)
        with patch.object(
            anthropic_proxy, "exchange_authorization_code",
            AsyncMock(return_value=dict(_TOKEN_DATA)),
        ):
            result = await anthropic_proxy._oauth_exchange_and_store(
                app, code="c", state="st"
            )
        self.assertTrue(result["key_id"])
        self.assertEqual(result["name"], "work-acct")
        self.assertNotIn("st", sessions)  # session consumed
        kwargs = app["db"].insert_anthropic_key.await_args.kwargs
        self.assertEqual(kwargs["key_type"], "oauth")
        self.assertEqual(kwargs["access_token"], "at-1")
        self.assertEqual(kwargs["refresh_token"], "rt-1")
        self.assertEqual(kwargs["subscription_type"], "claude_max")
        self.assertEqual(kwargs["rate_limit_tier"], "max_20x")
        self.assertEqual(kwargs["name"], "work-acct")
        self.assertIsNotNone(kwargs["expires_at"])
        app["anthropic_pool"].reload.assert_awaited_once()

    async def test_exchange_failure_raises_502(self) -> None:
        app = _app(_session())
        with patch.object(
            anthropic_proxy, "exchange_authorization_code",
            AsyncMock(side_effect=RuntimeError("token exchange HTTP 400: bad")),
        ):
            with self.assertRaises(anthropic_proxy.OAuthExchangeError) as ctx:
                await anthropic_proxy._oauth_exchange_and_store(app, code="c", state="st")
        self.assertEqual(ctx.exception.status, 502)
        app["db"].insert_anthropic_key.assert_not_awaited()

    async def test_no_access_token_raises_502(self) -> None:
        app = _app(_session())
        with patch.object(
            anthropic_proxy, "exchange_authorization_code",
            AsyncMock(return_value={"error": "denied"}),
        ):
            with self.assertRaises(anthropic_proxy.OAuthExchangeError) as ctx:
                await anthropic_proxy._oauth_exchange_and_store(app, code="c", state="st")
        self.assertEqual(ctx.exception.status, 502)


class LegacyWrapperTests(unittest.IsolatedAsyncioTestCase):
    """/_oauth/submit and /callback keep their plain-text / HTML behavior."""

    async def test_unknown_state_returns_400_text(self) -> None:
        req = make_mocked_request("GET", "/callback?code=c&state=x", app=_app())
        resp = await anthropic_proxy._oauth_run_code_exchange(req, "c", "x")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.content_type, "text/plain")

    async def test_success_returns_html_with_broadcast(self) -> None:
        req = make_mocked_request("GET", "/callback?code=c&state=st", app=_app(_session()))
        with patch.object(
            anthropic_proxy, "exchange_authorization_code",
            AsyncMock(return_value=dict(_TOKEN_DATA)),
        ):
            resp = await anthropic_proxy._oauth_run_code_exchange(req, "c", "st")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "text/html")
        self.assertIn("OAuth saved", resp.text)
        self.assertIn("BroadcastChannel", resp.text)  # manual tab notify kept


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<repo>/.venv/bin/python -m pytest tests/test_oauth_login_shared_core.py -q`
Expected: FAIL with `AttributeError: module 'smart_proxy.anthropic_proxy' has no attribute 'OAuthExchangeError'`

- [ ] **Step 3: Implement the shared core**

In `src/smart_proxy/anthropic_proxy.py`, replace the entire body of `_oauth_run_code_exchange` (keep its `async def _oauth_run_code_exchange(request: web.Request, code: str, state: str) -> web.Response:` definition) with the following three definitions. Put `OAuthExchangeError` and `_oauth_exchange_and_store` immediately ABOVE `_oauth_run_code_exchange`:

```python
class OAuthExchangeError(Exception):
    """Code-for-token exchange failure; ``status`` is the HTTP status to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _oauth_exchange_and_store(
    app: web.Application, *, code: str, state: str
) -> dict:
    """Pop the PKCE session, exchange the code, insert the key, reload the pool.

    Shared by the legacy HTML flow and the dashboard JSON API.
    Returns ``{"key_id": ..., "name": ...}``; raises OAuthExchangeError.
    """
    if not code or not state:
        raise OAuthExchangeError(400, "missing code or state")

    sessions: dict[str, dict] = app["_oauth_login_sessions"]
    sess = sessions.pop(state, None)
    if not sess:
        raise OAuthExchangeError(
            400,
            "unknown or expired OAuth state — start the login again "
            f"(sessions expire after {_OAUTH_LOGIN_SESSION_TTL_SEC // 60} minutes).",
        )

    client: httpx.AsyncClient = app["http_client"]
    try:
        data = await exchange_authorization_code(
            client,
            token_url=TOKEN_URL,
            code=code,
            verifier=sess["verifier"],
            state=state,
            redirect_uri=sess["redirect_uri"],
            client_id=CLAUDE_OAUTH_CLIENT_ID,
        )
    except RuntimeError as exc:
        logger.warning("OAuth code exchange failed: %s", exc)
        raise OAuthExchangeError(502, str(exc)) from exc

    access = data.get("access_token", "")
    if not access:
        raise OAuthExchangeError(502, json.dumps(data, indent=2)[:2000])

    expires_at = data.get("expires_at")
    if expires_at is None and data.get("expires_in"):
        expires_at = int((time.time() + float(data["expires_in"])) * 1000)

    scope_val = data.get("scope", "")
    scopes = scope_val.split() if isinstance(scope_val, str) else scope_val

    org = data.get("organization", {})

    db: Database = app["db"]
    key_id = str(uuid4())
    await db.insert_anthropic_key(
        id=key_id,
        key_type="oauth",
        access_token=access,
        refresh_token=data.get("refresh_token", ""),
        expires_at=int(expires_at) if expires_at is not None else None,
        scopes=json.dumps(scopes),
        subscription_type=org.get("organization_type", ""),
        rate_limit_tier=org.get("rate_limit_tier", ""),
        name=sess["name"],
    )

    pool: AnthropicKeyPool = app["anthropic_pool"]
    await pool.reload()

    logger.info("OAuth login stored new key %s..", key_id[:12])
    return {"key_id": key_id, "name": sess["name"]}
```

Then the wrapper (replacing the old body of `_oauth_run_code_exchange`):

```python
async def _oauth_run_code_exchange(
    request: web.Request,
    code: str,
    state: str,
) -> web.Response:
    """Legacy HTML flow: shared exchange core rendered as text/HTML responses."""
    try:
        result = await _oauth_exchange_and_store(request.app, code=code, state=state)
    except OAuthExchangeError as exc:
        return web.Response(
            status=exc.status,
            text=exc.message + "\n",
            content_type="text/plain",
            charset="utf-8",
        )

    notify = _oauth_broadcast_to_manual_tab_script(state, result["key_id"])
    html = (
        f"<!DOCTYPE html><html><body><h2>OAuth saved</h2>"
        f"<p>Key id: <code>{html_lib.escape(result['key_id'])}</code></p>"
        f"<p>The proxy has reloaded keys from the database. You can close this tab.</p>"
        f"{notify}"
        f"</body></html>"
    )
    return web.Response(text=html, content_type="text/html", charset="utf-8")
```

- [ ] **Step 4: Run the new tests and the neighbors**

Run: `<repo>/.venv/bin/python -m pytest tests/test_oauth_login_shared_core.py tests/test_dashboard_api.py tests/test_anthropic_proxy_oauth_messages.py -q`
Expected: all pass (new file: 7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_oauth_login_shared_core.py
git commit -m "refactor(oauth): extract shared exchange core from legacy login flow

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `GET /api/anthropic/keys`

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (new handler after `_api_keys`; new route in `register_dashboard_api`)
- Test: `tests/test_dashboard_anthropic_keys_api.py` (new — Tasks 4–6 append to this file)

**Interfaces:**
- Consumes: `db.list_anthropic_keys() -> list[dict]` (rows have all `anthropic_keys` columns).
- Produces: `GET /api/anthropic/keys` → `{"keys": [{id, key_type, status, name, subscription_type, rate_limit_tier, created_at, updated_at, expires_at, has_refresh_token}]}` — excludes `status='deleted'`, never includes token material. Frontend (Task 7) consumes this shape.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_dashboard_anthropic_keys_api.py`:

```python
# tests/test_dashboard_anthropic_keys_api.py
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from smart_proxy import dashboard_api  # noqa: E402

AUTH = {"Authorization": "Bearer sp-team"}


def _pool(valid: str = "sp-team"):
    pool = MagicMock()
    pool.check_auth.side_effect = lambda t: t == valid
    pool.is_proxy_key.side_effect = lambda t: t == valid
    pool.reload = AsyncMock()
    return pool


def _key_row(**over) -> dict:
    row = {
        "id": "key-1", "key_type": "oauth", "status": "active",
        "api_key": None, "access_token": "SECRET-AT", "refresh_token": "SECRET-RT",
        "client_id": "cid", "expires_at": 1790000000000,
        "scopes": '["user:inference"]', "subscription_type": "claude_max",
        "rate_limit_tier": "max_20x", "name": "acct",
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-02T00:00:00+00:00",
    }
    row.update(over)
    return row


class AnthropicKeysListTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_401(self) -> None:
        req = make_mocked_request(
            "GET", "/api/anthropic/keys",
            app={"anthropic_pool": _pool(), "db": MagicMock()},
        )
        resp = await dashboard_api._api_anthropic_keys(req)
        self.assertEqual(resp.status, 401)

    async def test_lists_keys_without_token_material(self) -> None:
        db = MagicMock()
        db.list_anthropic_keys = AsyncMock(return_value=[_key_row()])
        req = make_mocked_request(
            "GET", "/api/anthropic/keys",
            app={"anthropic_pool": _pool(), "db": db}, headers=AUTH,
        )
        resp = await dashboard_api._api_anthropic_keys(req)
        self.assertEqual(resp.status, 200)
        body = resp.body.decode()
        self.assertNotIn("SECRET-AT", body)
        self.assertNotIn("SECRET-RT", body)
        key = json.loads(body)["keys"][0]
        self.assertEqual(key["id"], "key-1")
        self.assertEqual(key["key_type"], "oauth")
        self.assertEqual(key["status"], "active")
        self.assertEqual(key["name"], "acct")
        self.assertEqual(key["subscription_type"], "claude_max")
        self.assertEqual(key["rate_limit_tier"], "max_20x")
        self.assertEqual(key["expires_at"], 1790000000000)
        self.assertTrue(key["has_refresh_token"])
        for forbidden in ("api_key", "access_token", "refresh_token"):
            self.assertNotIn(forbidden, key)

    async def test_deleted_rows_excluded(self) -> None:
        db = MagicMock()
        db.list_anthropic_keys = AsyncMock(return_value=[
            _key_row(),
            _key_row(id="key-2", status="deleted"),
        ])
        req = make_mocked_request(
            "GET", "/api/anthropic/keys",
            app={"anthropic_pool": _pool(), "db": db}, headers=AUTH,
        )
        resp = await dashboard_api._api_anthropic_keys(req)
        keys = json.loads(resp.body)["keys"]
        self.assertEqual([k["id"] for k in keys], ["key-1"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py -q`
Expected: FAIL with `AttributeError: module 'smart_proxy.dashboard_api' has no attribute '_api_anthropic_keys'`

- [ ] **Step 3: Implement the handler and route**

In `src/smart_proxy/dashboard_api.py`, after `_api_keys`, add:

```python
async def _api_anthropic_keys(request: web.Request) -> web.Response:
    """List Anthropic pool keys (no token material; soft-deleted rows hidden)."""
    if not _dashboard_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    rows = await db.list_anthropic_keys()
    keys = [
        {
            "id": r["id"],
            "key_type": r["key_type"],
            "status": r["status"],
            "name": r.get("name") or "",
            "subscription_type": r.get("subscription_type") or "",
            "rate_limit_tier": r.get("rate_limit_tier") or "",
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "expires_at": r.get("expires_at"),
            "has_refresh_token": bool(r.get("refresh_token")),
        }
        for r in rows
        if r.get("status") != "deleted"
    ]
    return web.json_response({"keys": keys})
```

In `register_dashboard_api`, after the existing `app.router.add_post("/api/keys", _api_key_create)` line, add:

```python
    app.router.add_get("/api/anthropic/keys", _api_anthropic_keys)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py tests/test_dashboard_api.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_anthropic_keys_api.py
git commit -m "feat(dashboard): GET /api/anthropic/keys listing endpoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `POST /api/anthropic/oauth/start` and `/api/anthropic/oauth/submit`

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (two handlers + two routes + one top-level import)
- Test: `tests/test_dashboard_anthropic_keys_api.py` (append)

**Interfaces:**
- Consumes (from Task 2): `smart_proxy.anthropic_proxy.OAuthExchangeError`, `_oauth_exchange_and_store(app, *, code, state) -> dict`; existing `_normalize_pasted_auth_code`, `_oauth_redirect_uri`, `_prune_oauth_login_sessions` (all lazy-imported); `smart_proxy.anthropic_oauth.build_claude_authorize_url`, `generate_pkce_pair` (top-level import — no cycle).
- Produces: `POST /api/anthropic/oauth/start` `{name}` → `{state, authorize_url, redirect_uri}`; `POST /api/anthropic/oauth/submit` `{state, code}` → `{ok: true, key_id}` (errors: `{"error": ...}` with 400/502). Frontend (Task 7) consumes both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard_anthropic_keys_api.py`:

```python
from unittest.mock import patch  # add to the imports at the top of the file

from smart_proxy import anthropic_proxy  # add below the dashboard_api import


class AnthropicOauthStartTests(unittest.IsolatedAsyncioTestCase):
    def _app(self) -> dict:
        return {"anthropic_pool": _pool(), "_oauth_login_sessions": {}}

    async def test_requires_action_auth(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.return_value = False
        req = make_mocked_request(
            "POST", "/api/anthropic/oauth/start",
            app={"anthropic_pool": pool, "_oauth_login_sessions": {}},
            headers={"Authorization": "Bearer sk-ant-x"},
        )
        req.json = AsyncMock(return_value={"name": "n"})
        resp = await dashboard_api._api_anthropic_oauth_start(req)
        self.assertEqual(resp.status, 401)

    async def test_start_creates_session_and_authorize_url(self) -> None:
        app = self._app()
        req = make_mocked_request(
            "POST", "/api/anthropic/oauth/start", app=app, headers=AUTH,
        )
        req.json = AsyncMock(return_value={"name": "  work-acct  "})
        resp = await dashboard_api._api_anthropic_oauth_start(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        state = body["state"]
        self.assertIn(state, app["_oauth_login_sessions"])
        sess = app["_oauth_login_sessions"][state]
        self.assertEqual(sess["name"], "work-acct")
        self.assertEqual(sess["redirect_uri"], body["redirect_uri"])
        self.assertTrue(body["redirect_uri"].startswith("http://localhost:"))
        self.assertTrue(body["redirect_uri"].endswith("/callback"))
        self.assertIn("claude.ai/oauth/authorize", body["authorize_url"])
        self.assertIn(state, body["authorize_url"])

    async def test_start_defaults_name(self) -> None:
        app = self._app()
        req = make_mocked_request(
            "POST", "/api/anthropic/oauth/start", app=app, headers=AUTH,
        )
        req.json = AsyncMock(return_value={})
        resp = await dashboard_api._api_anthropic_oauth_start(req)
        state = json.loads(resp.body)["state"]
        self.assertEqual(app["_oauth_login_sessions"][state]["name"], "oauth-login")


class AnthropicOauthSubmitTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, sessions: dict) -> dict:
        db = MagicMock()
        db.insert_anthropic_key = AsyncMock()
        return {
            "anthropic_pool": _pool(),
            "_oauth_login_sessions": sessions,
            "http_client": MagicMock(),
            "db": db,
        }

    async def test_submit_accepts_pasted_callback_url(self) -> None:
        sessions = {"st1": {
            "verifier": "v", "name": "n",
            "redirect_uri": "http://localhost:8090/callback", "ts": 0,
        }}
        app = self._app(sessions)
        req = make_mocked_request(
            "POST", "/api/anthropic/oauth/submit", app=app, headers=AUTH,
        )
        req.json = AsyncMock(return_value={
            "state": "st1",
            "code": "http://localhost:8090/callback?code=the-code&state=st1",
        })
        exchange = AsyncMock(return_value={
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
            "scope": "user:inference", "organization": {},
        })
        with patch.object(anthropic_proxy, "exchange_authorization_code", exchange):
            resp = await dashboard_api._api_anthropic_oauth_submit(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["key_id"])
        self.assertEqual(exchange.await_args.kwargs["code"], "the-code")
        app["db"].insert_anthropic_key.assert_awaited_once()

    async def test_submit_unknown_state_400_json(self) -> None:
        app = self._app({})
        req = make_mocked_request(
            "POST", "/api/anthropic/oauth/submit", app=app, headers=AUTH,
        )
        req.json = AsyncMock(return_value={"state": "nope", "code": "c"})
        resp = await dashboard_api._api_anthropic_oauth_submit(req)
        self.assertEqual(resp.status, 400)
        self.assertIn("error", json.loads(resp.body))

    async def test_submit_requires_action_auth(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.return_value = False
        req = make_mocked_request(
            "POST", "/api/anthropic/oauth/submit",
            app={"anthropic_pool": pool},
            headers={"Authorization": "Bearer sk-ant-x"},
        )
        req.json = AsyncMock(return_value={"state": "s", "code": "c"})
        resp = await dashboard_api._api_anthropic_oauth_submit(req)
        self.assertEqual(resp.status, 401)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py -q`
Expected: new tests FAIL with `AttributeError: ... has no attribute '_api_anthropic_oauth_start'`; Task 3 tests still pass.

- [ ] **Step 3: Implement handlers and routes**

In `src/smart_proxy/dashboard_api.py`:

1. Extend the top-of-file imports: add `import time` next to `import secrets`, and below the existing `from smart_proxy.db import Database` add:

```python
from smart_proxy.anthropic_oauth import build_claude_authorize_url, generate_pkce_pair
```

2. After `_api_anthropic_keys`, add:

```python
async def _api_anthropic_oauth_start(request: web.Request) -> web.Response:
    """Begin a PKCE login for a new Anthropic OAuth key (dashboard flow)."""
    if not _action_authorized(request):
        return _unauthorized()
    # Lazy import: anthropic_proxy imports this module at startup.
    from smart_proxy.anthropic_proxy import (
        _oauth_redirect_uri,
        _prune_oauth_login_sessions,
    )

    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()[:200] or "oauth-login"

    _prune_oauth_login_sessions(request.app)
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    redirect_uri = _oauth_redirect_uri(request.app)
    request.app["_oauth_login_sessions"][state] = {
        "verifier": verifier,
        "name": name,
        "redirect_uri": redirect_uri,
        "ts": time.time(),
    }
    authorize_url = build_claude_authorize_url(
        challenge=challenge, state=state, redirect_uri=redirect_uri
    )
    return web.json_response({
        "state": state,
        "authorize_url": authorize_url,
        "redirect_uri": redirect_uri,
    })


async def _api_anthropic_oauth_submit(request: web.Request) -> web.Response:
    """Finish the PKCE login with a pasted callback URL or bare code."""
    if not _action_authorized(request):
        return _unauthorized()
    from smart_proxy.anthropic_proxy import (
        OAuthExchangeError,
        _normalize_pasted_auth_code,
        _oauth_exchange_and_store,
    )

    try:
        body = await request.json()
    except Exception:
        body = {}
    state = str(body.get("state", "")).strip()
    code = _normalize_pasted_auth_code(str(body.get("code", "")))
    try:
        result = await _oauth_exchange_and_store(request.app, code=code, state=state)
    except OAuthExchangeError as exc:
        return web.json_response({"error": exc.message}, status=exc.status)
    return web.json_response({"ok": True, "key_id": result["key_id"]})
```

3. In `register_dashboard_api`, after the `/api/anthropic/keys` route:

```python
    app.router.add_post("/api/anthropic/oauth/start", _api_anthropic_oauth_start)
    app.router.add_post("/api/anthropic/oauth/submit", _api_anthropic_oauth_submit)
```

Note: `_prune_oauth_login_sessions` and `_oauth_redirect_uri` take the app object and use only `.get(...)` / `[...]` access, so plain-dict apps in tests work. `_oauth_exchange_and_store` pops the session and does exchange+insert+reload (Task 2). No `oauth_login_secret` check here — the dashboard action gate replaces it. Works with `ANTHROPIC_OAUTH_LOGIN_BASE_URL` unset: `_oauth_redirect_uri` falls back to `http://localhost:<PROXY_PORT>/callback`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py tests/test_dashboard_api.py tests/test_oauth_login_shared_core.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_anthropic_keys_api.py
git commit -m "feat(dashboard): OAuth login start/submit JSON endpoints

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `POST /api/anthropic/keys/status`, `/rename`, `/delete`

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (three handlers + three routes)
- Test: `tests/test_dashboard_anthropic_keys_api.py` (append)

**Interfaces:**
- Consumes: `db.get_anthropic_key(id) -> dict | None`, `db.set_anthropic_key_status(id, status, **audit)`, `db.set_anthropic_key_name(id, name) -> bool` (Task 1).
- Produces: `POST /api/anthropic/keys/status` `{id, active: bool}` → `{ok, status}`; `POST /api/anthropic/keys/rename` `{id, name}` → `{ok, name}`; `POST /api/anthropic/keys/delete` `{id}` → `{ok}`. All 400 on missing fields, 404 on unknown id, and reload the pool on success. Frontend (Task 7) consumes these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard_anthropic_keys_api.py`:

```python
def _mgmt_app(row: dict | None = None):
    """(app, db, pool) for the status/rename/delete/refresh endpoint tests."""
    pool = _pool()
    db = MagicMock()
    db.get_anthropic_key = AsyncMock(return_value=row)
    db.set_anthropic_key_status = AsyncMock()
    db.set_anthropic_key_name = AsyncMock(return_value=row is not None)
    return {"anthropic_pool": pool, "db": db}, db, pool


class AnthropicKeyStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_id_400(self) -> None:
        app, db, _ = _mgmt_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/status", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"active": False})
        resp = await dashboard_api._api_anthropic_key_status(req)
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_status.assert_not_awaited()

    async def test_unknown_id_404(self) -> None:
        app, db, _ = _mgmt_app(None)
        req = make_mocked_request("POST", "/api/anthropic/keys/status", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "nope", "active": False})
        resp = await dashboard_api._api_anthropic_key_status(req)
        self.assertEqual(resp.status, 404)

    async def test_disable_sets_inactive_and_reloads(self) -> None:
        app, db, pool = _mgmt_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/status", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "active": False})
        resp = await dashboard_api._api_anthropic_key_status(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["status"], "inactive")
        args = db.set_anthropic_key_status.await_args
        self.assertEqual(args.args, ("key-1", "inactive"))
        self.assertEqual(args.kwargs.get("audit_source"), "dashboard")
        pool.reload.assert_awaited_once()

    async def test_enable_sets_active(self) -> None:
        app, db, pool = _mgmt_app(_key_row(status="inactive"))
        req = make_mocked_request("POST", "/api/anthropic/keys/status", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "active": True})
        resp = await dashboard_api._api_anthropic_key_status(req)
        self.assertEqual(json.loads(resp.body)["status"], "active")
        self.assertEqual(db.set_anthropic_key_status.await_args.args, ("key-1", "active"))


class AnthropicKeyRenameTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_name_400(self) -> None:
        app, db, _ = _mgmt_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/rename", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "name": "   "})
        resp = await dashboard_api._api_anthropic_key_rename(req)
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_name.assert_not_awaited()

    async def test_unknown_id_404(self) -> None:
        app, db, _ = _mgmt_app(None)
        req = make_mocked_request("POST", "/api/anthropic/keys/rename", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "nope", "name": "x"})
        resp = await dashboard_api._api_anthropic_key_rename(req)
        self.assertEqual(resp.status, 404)

    async def test_rename_ok_reloads(self) -> None:
        app, db, pool = _mgmt_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/rename", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "name": "  fresh  "})
        resp = await dashboard_api._api_anthropic_key_rename(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["name"], "fresh")
        db.set_anthropic_key_name.assert_awaited_once_with("key-1", "fresh")
        pool.reload.assert_awaited_once()


class AnthropicKeyDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_id_404(self) -> None:
        app, db, _ = _mgmt_app(None)
        req = make_mocked_request("POST", "/api/anthropic/keys/delete", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "nope"})
        resp = await dashboard_api._api_anthropic_key_delete(req)
        self.assertEqual(resp.status, 404)

    async def test_soft_delete_sets_status_deleted(self) -> None:
        app, db, pool = _mgmt_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/delete", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        resp = await dashboard_api._api_anthropic_key_delete(req)
        self.assertEqual(resp.status, 200)
        args = db.set_anthropic_key_status.await_args
        self.assertEqual(args.args, ("key-1", "deleted"))
        self.assertEqual(args.kwargs.get("audit_source"), "dashboard")
        pool.reload.assert_awaited_once()

    async def test_requires_action_auth(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.return_value = False
        db = MagicMock()
        db.set_anthropic_key_status = AsyncMock()
        req = make_mocked_request(
            "POST", "/api/anthropic/keys/delete",
            app={"anthropic_pool": pool, "db": db},
            headers={"Authorization": "Bearer sk-ant-x"},
        )
        req.json = AsyncMock(return_value={"id": "key-1"})
        resp = await dashboard_api._api_anthropic_key_delete(req)
        self.assertEqual(resp.status, 401)
        db.set_anthropic_key_status.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py -q`
Expected: new tests FAIL with `AttributeError: ... '_api_anthropic_key_status'`; earlier tests pass.

- [ ] **Step 3: Implement handlers and routes**

In `src/smart_proxy/dashboard_api.py`, after `_api_anthropic_oauth_submit`, add:

```python
async def _anthropic_key_from_body(
    request: web.Request,
) -> tuple[dict | None, dict, web.Response | None]:
    """Common body parsing for key-management endpoints: (row, body, error)."""
    db: Database | None = request.app.get("db")
    if db is None:
        return None, {}, web.json_response(
            {"error": "database unavailable"}, status=500
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    key_id = str(body.get("id", "")).strip()
    if not key_id:
        return None, body, web.json_response({"error": "id required"}, status=400)
    row = await db.get_anthropic_key(key_id)
    if row is None:
        return None, body, web.json_response({"error": "key not found"}, status=404)
    return row, body, None


async def _api_anthropic_key_status(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _unauthorized()
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    status = "active" if bool(body.get("active")) else "inactive"
    await request.app["db"].set_anthropic_key_status(
        row["id"], status,
        audit_source="dashboard",
        audit_event_type="dashboard_status_change",
        audit_decision=status,
        audit_error_type="manual_action",
        audit_error_message=f"Set {status} via dashboard",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "status": status})


async def _api_anthropic_key_rename(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _unauthorized()
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    ok = await request.app["db"].set_anthropic_key_name(row["id"], name)
    if not ok:
        return web.json_response({"error": "key not found"}, status=404)
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "name": name})


async def _api_anthropic_key_delete(request: web.Request) -> web.Response:
    """Soft delete: history keeps referencing the key_id; row recoverable via SQL."""
    if not _action_authorized(request):
        return _unauthorized()
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    await request.app["db"].set_anthropic_key_status(
        row["id"], "deleted",
        audit_source="dashboard",
        audit_event_type="dashboard_delete",
        audit_decision="soft_delete",
        audit_error_type="manual_action",
        audit_error_message="Soft-deleted via dashboard",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True})
```

Note: `_api_anthropic_key_rename` checks the empty name BEFORE `set_anthropic_key_name` but AFTER `_anthropic_key_from_body` — the rename test with empty name uses a known id, so order matters only for the 400. Routes, after the oauth ones in `register_dashboard_api`:

```python
    app.router.add_post("/api/anthropic/keys/status", _api_anthropic_key_status)
    app.router.add_post("/api/anthropic/keys/rename", _api_anthropic_key_rename)
    app.router.add_post("/api/anthropic/keys/delete", _api_anthropic_key_delete)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py tests/test_dashboard_api.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_anthropic_keys_api.py
git commit -m "feat(dashboard): anthropic key status/rename/delete endpoints

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: `POST /api/anthropic/keys/refresh`

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (one handler + one route)
- Test: `tests/test_dashboard_anthropic_keys_api.py` (append)

**Interfaces:**
- Consumes: `_anthropic_key_from_body` (Task 5); `smart_proxy.anthropic_oauth.refresh_oauth_token`, `activate_oauth_access_token`, `normalize_scope` (lazy import — patched in tests at `smart_proxy.anthropic_oauth.<name>`); `smart_proxy.anthropic_proxy.TOKEN_URL`, `UPSTREAM_BASE` (lazy import); `db.update_anthropic_oauth_tokens(key_id, access_token, expires_at, refresh_token, **audit)`.
- Produces: `POST /api/anthropic/keys/refresh` `{id}` → `{ok: true, expires_at}`; 400 for non-oauth / no refresh token; 502 when refresh or activation fails (tokens NOT saved then — mirrors the pool's order: refresh → activate → persist). Frontend (Task 7) consumes this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard_anthropic_keys_api.py`:

```python
class AnthropicKeyRefreshTests(unittest.IsolatedAsyncioTestCase):
    def _refresh_app(self, row: dict | None):
        app, db, pool = _mgmt_app(row)
        db.update_anthropic_oauth_tokens = AsyncMock()
        app["http_client"] = MagicMock()
        return app, db, pool

    async def test_api_key_type_400(self) -> None:
        app, db, _ = self._refresh_app(
            _key_row(key_type="api_key", refresh_token=None, api_key="sk-ant-x"))
        req = make_mocked_request("POST", "/api/anthropic/keys/refresh", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        resp = await dashboard_api._api_anthropic_key_refresh(req)
        self.assertEqual(resp.status, 400)
        db.update_anthropic_oauth_tokens.assert_not_awaited()

    async def test_refresh_failure_502_nothing_saved(self) -> None:
        app, db, pool = self._refresh_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/refresh", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        import smart_proxy.anthropic_oauth as ao
        with patch.object(ao, "refresh_oauth_token",
                          AsyncMock(side_effect=RuntimeError("HTTP 400"))):
            resp = await dashboard_api._api_anthropic_key_refresh(req)
        self.assertEqual(resp.status, 502)
        db.update_anthropic_oauth_tokens.assert_not_awaited()
        pool.reload.assert_not_awaited()

    async def test_refresh_ok_saves_and_reloads(self) -> None:
        app, db, pool = self._refresh_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/refresh", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        import smart_proxy.anthropic_oauth as ao
        with patch.object(ao, "refresh_oauth_token",
                          AsyncMock(return_value=("new-at", 1795000000000, "new-rt"))), \
             patch.object(ao, "activate_oauth_access_token", AsyncMock(return_value=[])):
            resp = await dashboard_api._api_anthropic_key_refresh(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["expires_at"], 1795000000000)
        args = db.update_anthropic_oauth_tokens.await_args
        self.assertEqual(args.args, ("key-1", "new-at", 1795000000000, "new-rt"))
        self.assertEqual(args.kwargs.get("audit_source"), "dashboard")
        pool.reload.assert_awaited_once()

    async def test_activation_failure_502_nothing_saved(self) -> None:
        app, db, pool = self._refresh_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/refresh", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        import smart_proxy.anthropic_oauth as ao
        with patch.object(ao, "refresh_oauth_token",
                          AsyncMock(return_value=("new-at", 1795000000000, None))), \
             patch.object(ao, "activate_oauth_access_token",
                          AsyncMock(side_effect=RuntimeError("activation 403"))):
            resp = await dashboard_api._api_anthropic_key_refresh(req)
        self.assertEqual(resp.status, 502)
        db.update_anthropic_oauth_tokens.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py -q`
Expected: new tests FAIL with `AttributeError: ... '_api_anthropic_key_refresh'`

- [ ] **Step 3: Implement handler and route**

In `src/smart_proxy/dashboard_api.py`, after `_api_anthropic_key_delete`:

```python
async def _api_anthropic_key_refresh(request: web.Request) -> web.Response:
    """Force an OAuth token refresh (+ activation), mirroring the pool's order:
    refresh -> activate -> persist. Nothing is saved when either step fails."""
    if not _action_authorized(request):
        return _unauthorized()
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    if row.get("key_type") != "oauth" or not row.get("refresh_token"):
        return web.json_response(
            {"error": "not an OAuth key with a refresh token"}, status=400
        )

    # Lazy imports: anthropic_proxy imports this module; anthropic_oauth is
    # imported lazily too so tests can patch smart_proxy.anthropic_oauth.<fn>.
    import smart_proxy.anthropic_oauth as anthropic_oauth
    from smart_proxy.anthropic_proxy import TOKEN_URL, UPSTREAM_BASE

    client = request.app["http_client"]
    try:
        new_token, new_expires, rotated_refresh = await anthropic_oauth.refresh_oauth_token(
            client,
            token_url=TOKEN_URL,
            refresh_token=row["refresh_token"],
            client_id=row["client_id"],
            scope=anthropic_oauth.normalize_scope(row.get("scopes")),
        )
    except Exception as exc:  # RuntimeError or httpx.HTTPStatusError (429)
        return web.json_response({"error": f"refresh failed: {exc}"}, status=502)
    try:
        await anthropic_oauth.activate_oauth_access_token(
            client, access_token=new_token, base_url=UPSTREAM_BASE
        )
    except RuntimeError as exc:
        return web.json_response({"error": f"activation failed: {exc}"}, status=502)

    await request.app["db"].update_anthropic_oauth_tokens(
        row["id"], new_token, new_expires, rotated_refresh,
        audit_source="dashboard",
        audit_event_type="refresh_succeeded",
        audit_decision="update_tokens",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "expires_at": new_expires})
```

Route in `register_dashboard_api`, after the delete route:

```python
    app.router.add_post("/api/anthropic/keys/refresh", _api_anthropic_key_refresh)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `<repo>/.venv/bin/python -m pytest tests/test_dashboard_anthropic_keys_api.py tests/test_dashboard_api.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_anthropic_keys_api.py
git commit -m "feat(dashboard): force OAuth token refresh endpoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Frontend — Anthropic tab

**Files:**
- Create: `web/src/views/AnthropicView.svelte`
- Modify: `web/src/App.svelte` (tab type, TABS list, nav button, view branch)
- Build check: `cd web && npm ci && npm run build`

**Interfaces:**
- Consumes: all `/api/anthropic/*` endpoints from Tasks 3–6 (shapes as defined there); `apiGet`/`apiPost` from `web/src/lib/api.ts`; global CSS classes from `web/src/app.css` (`.create`, `.toolbar`, `.created`, `.err`, `.primary`, table styles); the `/callback` page's completion broadcast: `BroadcastChannel('smart_proxy_oauth')` messages `{ok, state, key_id}` and `localStorage` key `'smart_proxy_oauth_' + state`.
- Produces: `anthropic` tab in the SPA.

- [ ] **Step 1: Create `web/src/views/AnthropicView.svelte`**

```svelte
<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { apiGet, apiPost } from '../lib/api'

  type AKey = {
    id: string; key_type: string; status: string; name: string
    subscription_type: string; rate_limit_tier: string
    created_at: string | null; updated_at: string | null
    expires_at: number | null; has_refresh_token: boolean
  }

  let keys = $state<AKey[]>([])
  let error = $state('')
  let busy = $state(false)

  // Add-OAuth wizard state
  let newName = $state('')
  let oauthState = $state('')       // non-empty → wizard step 2 (paste code)
  let authorizeUrl = $state('')
  let redirectUri = $state('')
  let pasted = $state('')
  let successKeyId = $state('')
  let bc: BroadcastChannel | null = null
  let storageHandler: ((ev: StorageEvent) => void) | null = null

  // Inline rename state
  let renameId = $state('')
  let renameValue = $state('')

  async function load() {
    try { keys = (await apiGet<{ keys: AKey[] }>('/api/anthropic/keys')).keys }
    catch (e) { error = String(e) }
  }

  function isOn(k: AKey): boolean {
    return k.status === 'active' || k.status === 'low_balance'
  }
  function fmtExpiry(ms: number | null): string {
    if (!ms) return '—'
    const d = ms - Date.now()
    if (d <= 0) return 'expired'
    const h = Math.floor(d / 3600_000)
    const m = Math.floor((d % 3600_000) / 60_000)
    return h > 0 ? `in ${h}h ${m}m` : `in ${m}m`
  }
  function fmtDate(s: string | null): string {
    return s ? s.slice(0, 10) : '—'
  }

  function stopListening() {
    bc?.close(); bc = null
    if (storageHandler) { window.removeEventListener('storage', storageHandler); storageHandler = null }
  }
  function finish(keyId: string) {
    successKeyId = keyId
    oauthState = ''; authorizeUrl = ''; pasted = ''; newName = ''
    stopListening()
    load()
  }
  // The /callback page (same origin, e.g. via SSH -L forward) broadcasts
  // {ok, state, key_id} when it completes the exchange server-side.
  function listenForCallback(state: string) {
    stopListening()
    try {
      bc = new BroadcastChannel('smart_proxy_oauth')
      bc.onmessage = (ev) => {
        if (ev.data && ev.data.state === state && ev.data.ok) finish(ev.data.key_id)
      }
    } catch { /* BroadcastChannel unavailable — storage event still works */ }
    storageHandler = (ev: StorageEvent) => {
      if (ev.key === 'smart_proxy_oauth_' + state && ev.newValue) {
        try {
          const p = JSON.parse(ev.newValue)
          if (p.ok) finish(p.key_id)
        } catch { /* ignore malformed */ }
      }
    }
    window.addEventListener('storage', storageHandler)
  }
  onDestroy(stopListening)

  async function startOauth(e: Event) {
    e.preventDefault()
    error = ''; successKeyId = ''
    busy = true
    try {
      const r = await apiPost<{ state: string; authorize_url: string; redirect_uri: string }>(
        '/api/anthropic/oauth/start', { name: newName.trim() })
      oauthState = r.state
      authorizeUrl = r.authorize_url
      redirectUri = r.redirect_uri
      listenForCallback(r.state)
      window.open(r.authorize_url, '_blank', 'noopener,noreferrer')
    } catch (e) { error = String(e) }
    finally { busy = false }
  }
  async function submitCode(e: Event) {
    e.preventDefault()
    if (!pasted.trim()) return
    error = ''
    busy = true
    try {
      const r = await apiPost<{ ok: boolean; key_id: string }>(
        '/api/anthropic/oauth/submit', { state: oauthState, code: pasted.trim() })
      finish(r.key_id)
    } catch (e) { error = String(e) }
    finally { busy = false }
  }
  function cancelOauth() {
    oauthState = ''; authorizeUrl = ''; pasted = ''
    stopListening()
  }

  async function act(fn: () => Promise<unknown>) {
    error = ''
    busy = true
    try { await fn(); await load() }
    catch (e) { error = String(e) }
    finally { busy = false }
  }
  const toggle = (k: AKey) =>
    act(() => apiPost('/api/anthropic/keys/status', { id: k.id, active: !isOn(k) }))
  const refresh = (k: AKey) =>
    act(() => apiPost('/api/anthropic/keys/refresh', { id: k.id }))
  const del = (k: AKey) => {
    if (!confirm(`Delete key "${k.name || k.id}"? Usage history is kept; the key stops being used.`)) return
    return act(() => apiPost('/api/anthropic/keys/delete', { id: k.id }))
  }
  function startRename(k: AKey) { renameId = k.id; renameValue = k.name }
  const saveRename = () => {
    const name = renameValue.trim()
    if (!name) return
    return act(async () => {
      await apiPost('/api/anthropic/keys/rename', { id: renameId, name })
      renameId = ''
    })
  }

  onMount(load)
</script>

{#if !oauthState}
  <form class="create" onsubmit={startOauth}>
    <input placeholder="new OAuth session name" bind:value={newName} disabled={busy} />
    <button class="primary" type="submit" disabled={busy}>Add OAuth session</button>
  </form>
{:else}
  <form class="create" onsubmit={submitCode}>
    <input placeholder="paste callback URL or code" bind:value={pasted} disabled={busy} />
    <button class="primary" type="submit" disabled={busy || !pasted.trim()}>Save OAuth key</button>
    <button type="button" onclick={cancelOauth} disabled={busy}>Cancel</button>
  </form>
  <p>
    Authorize in the opened tab (popup blocked? <a href={authorizeUrl} target="_blank" rel="noopener noreferrer">open manually</a>).
    Anthropic redirects to <code>{redirectUri}</code> on <em>your</em> machine — with an SSH
    <code>-L</code> forward it completes automatically; otherwise copy the callback URL
    from the failed tab's address bar and paste it above.
  </p>
{/if}

{#if successKeyId}
  <div class="created">
    <span>OAuth key saved and active:</span>
    <code>{successKeyId}</code>
    <button type="button" onclick={() => { successKeyId = '' }}>Dismiss</button>
  </div>
{/if}

{#if error}<p class="err">{error}</p>{/if}

<table>
  <thead>
    <tr><th>Name</th><th>Type</th><th>Status</th><th>Subscription</th><th>Token expires</th><th>Created</th><th></th></tr>
  </thead>
  <tbody>
    {#each keys as k (k.id)}
      <tr>
        <td>
          {#if renameId === k.id}
            <input bind:value={renameValue} disabled={busy} />
            <button type="button" onclick={saveRename} disabled={busy || !renameValue.trim()}>Save</button>
            <button type="button" onclick={() => { renameId = '' }} disabled={busy}>Cancel</button>
          {:else}
            {k.name || '—'}
            <button type="button" onclick={() => startRename(k)} disabled={busy}>Rename</button>
          {/if}
        </td>
        <td>{k.key_type}</td>
        <td>{k.status}</td>
        <td>{k.subscription_type}{k.rate_limit_tier ? ` / ${k.rate_limit_tier}` : ''}</td>
        <td>{k.key_type === 'oauth' ? fmtExpiry(k.expires_at) : '—'}</td>
        <td>{fmtDate(k.created_at)}</td>
        <td>
          <button type="button" onclick={() => toggle(k)} disabled={busy}>{isOn(k) ? 'Disable' : 'Enable'}</button>
          {#if k.key_type === 'oauth' && k.has_refresh_token}
            <button type="button" onclick={() => refresh(k)} disabled={busy}>Refresh</button>
          {/if}
          <button type="button" onclick={() => del(k)} disabled={busy}>Delete</button>
        </td>
      </tr>
    {/each}
  </tbody>
</table>
```

- [ ] **Step 2: Wire the tab in `web/src/App.svelte`**

Apply these exact edits:

1. Import (after the `KeysView` import):
```ts
  import AnthropicView from './views/AnthropicView.svelte'
```
2. Tab type + list:
```ts
  type Tab = 'usage' | 'windows' | 'anthropic' | 'compat' | 'keys'
  const TABS: Tab[] = ['usage', 'windows', 'anthropic', 'compat', 'keys']
```
3. Nav button (between Windows and Compat buttons):
```svelte
      <button class:active={tab === 'anthropic'} onclick={() => setTab('anthropic')}>Anthropic</button>
```
4. View branch (between the windows and compat branches):
```svelte
    {:else if tab === 'anthropic'}<AnthropicView />
```

- [ ] **Step 3: Build**

Run: `cd web && npm ci && npm run build`
(The worktree has no `node_modules` yet — `npm ci` is required once.)
Expected: vite build succeeds, output written to `../src/smart_proxy/static/app/` (gitignored — do not commit it).

- [ ] **Step 4: Commit**

```bash
git add web/src/views/AnthropicView.svelte web/src/App.svelte
git commit -m "feat(dashboard): Anthropic tab — key list, OAuth add, manage actions

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: README + full verification

**Files:**
- Modify: `README.md` (dashboard blurb)
- Verify: whole test suite + build

- [ ] **Step 1: Update README**

In `README.md`, replace the operator-dashboard paragraph:

```markdown
The operator dashboard (usage, OAuth quotas, compat stats, key toggles) is a
Svelte SPA served at `/_app/`. Sign in with an `sp-*` proxy key. It is built
from `web/` into `src/smart_proxy/static/app/` (`npm run build`) — see
[Deployment](#deployment).
```

with:

```markdown
The operator dashboard (usage, OAuth quotas, compat stats, key toggles, and
Anthropic account keys) is a Svelte SPA served at `/_app/`. Sign in with an
`sp-*` proxy key. The **Anthropic** tab lists the `anthropic_keys` pool and
manages it: add a new OAuth session (browser PKCE login — paste the callback
URL, or let an SSH `-L` forward complete it automatically), enable/disable,
rename, soft-delete, and force a token refresh. The legacy
`/_oauth/login?manual=true&name=...` page still works as a fallback. The SPA
is built from `web/` into `src/smart_proxy/static/app/` (`npm run build`) — see
[Deployment](#deployment).
```

- [ ] **Step 2: Run the full Python suite**

Run: `<repo>/.venv/bin/python -m pytest tests/ -q`
Expected: everything passes (some DB tests may skip if PostgreSQL env is not set — same as before this change; compare against a clean run if unsure).

- [ ] **Step 3: Confirm the SPA build is current**

Run: `cd web && npm run build`
Expected: success.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: describe Anthropic tab in the dashboard blurb

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
