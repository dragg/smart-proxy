# Dashboard SPA (Svelte + Vite) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-written `/_usage` HTML with a maintainable Svelte SPA served from the existing aiohttp proxy, exposing usage/OAuth/compat metrics plus operator actions (reload, key enable/disable) over a JSON `/api/*` layer.

**Architecture:** aiohttp stays the only backend. A new `dashboard_api.py` registers `/api/*` JSON endpoints and serves a compiled Svelte bundle under `/_app/`, both before the catch-all proxy route. The SPA authenticates by posting an `sp-*` token to `POST /api/session`, which sets an httpOnly cookie re-validated on every request via `AnthropicKeyPool.check_auth`. The proxy core (SSE, OAuth, cache-TTL) is untouched.

**Tech Stack:** Backend — Python 3, aiohttp, existing `Database`. Frontend — Svelte 5 (runes) + Vite + TypeScript + Chart.js, built with npm.

## Global Constraints

- Python: every new module starts with `from __future__ import annotations`; match the existing aiohttp/handler style.
- Tests: `unittest.TestCase` / `unittest.IsolatedAsyncioTestCase`, run with `python -m pytest <path> -v` (the repo's suite; `python -m unittest` also works). New backend behavior is TDD — failing test first.
- Never `git add -A` (the working tree contains secrets: `oauth.json`, `smart-proxy.db.bak`, etc.). Stage exact paths only.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Frontend build output goes to `src/smart_proxy/static/app/` and is **gitignored** — it ships via deploy, never via git.
- SPA lives under `/_app/`; JSON under `/api/*`; both registered **before** `add_route("*", "/", ...)` and `add_route("*", "/{path:.+}", ...)` in `create_app`.
- Auth mechanism (deviation from spec, intentional): the cookie holds the raw `sp-*` token and is **re-validated against `pool.check_auth` on every request**, which makes signing unnecessary — a forged cookie simply fails `check_auth`, exactly like a forged header. Cookie is `httponly=True, samesite="Lax"`. (`secure=True` is a later hardening step once TLS termination is confirmed; leaving it off keeps http://localhost dev working.)
- Keep `/_usage` (in `usage_dashboard.py`) live and unchanged through this whole plan. Its removal is a separate follow-up once the SPA Usage view reaches parity.

---

### Task 1: `db.set_proxy_key_active` — enable/disable a proxy key by prefix

**Files:**
- Modify: `src/smart_proxy/db.py` (add method next to `revoke_proxy_key`, ~line 834)
- Test: `tests/test_proxy_key_active.py` (create)

**Interfaces:**
- Consumes: existing `Database.add_proxy_key(key, name)`, `Database.list_proxy_keys()`.
- Produces: `async Database.set_proxy_key_active(key_prefix: str, active: bool) -> str | None` — matches exactly one key whose value starts with `key_prefix` (case-insensitive), sets its `active` flag to 1/0, returns the full key, or `None` when zero or multiple keys match. Mirrors `revoke_proxy_key` but without the `active = 1` filter (so a disabled key can be re-enabled).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_proxy_key_active.py
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import build_database_from_config


class SetProxyKeyActiveTests(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        return db

    async def asyncTearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    async def test_disable_then_enable_by_prefix(self) -> None:
        db = await self._db()
        await db.add_proxy_key("sp-alpha-123", "Alpha")

        full = await db.set_proxy_key_active("sp-alpha", False)
        self.assertEqual(full, "sp-alpha-123")
        row = (await db.list_proxy_keys())[0]
        self.assertEqual(int(row["active"]), 0)

        full = await db.set_proxy_key_active("sp-alpha", True)
        self.assertEqual(full, "sp-alpha-123")
        row = (await db.list_proxy_keys())[0]
        self.assertEqual(int(row["active"]), 1)

    async def test_ambiguous_prefix_returns_none(self) -> None:
        db = await self._db()
        await db.add_proxy_key("sp-a-1", "A1")
        await db.add_proxy_key("sp-a-2", "A2")
        self.assertIsNone(await db.set_proxy_key_active("sp-a", False))

    async def test_unknown_prefix_returns_none(self) -> None:
        db = await self._db()
        self.assertIsNone(await db.set_proxy_key_active("sp-missing", True))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_proxy_key_active.py -v`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'set_proxy_key_active'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/smart_proxy/db.py` immediately after `revoke_proxy_key` (after line 850):

```python
    async def set_proxy_key_active(self, key_prefix: str, active: bool) -> str | None:
        """Enable/disable a proxy key matching the prefix. Returns the full key
        or None when zero or multiple keys match."""
        cur = await self.db.execute(
            "SELECT key FROM proxy_api_keys WHERE LOWER(key) LIKE LOWER(?)",
            (key_prefix + "%",),
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        full_key = rows[0]["key"]
        await self.db.execute(
            "UPDATE proxy_api_keys SET active = ? WHERE key = ?",
            (1 if active else 0, full_key),
        )
        await self.db.commit()
        return full_key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_proxy_key_active.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/db.py tests/test_proxy_key_active.py
git commit -m "feat(db): set_proxy_key_active to toggle a proxy key by prefix

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `build_usage_cost_json` — JSON shape of the usage dashboard data

**Files:**
- Modify: `src/smart_proxy/usage_dashboard.py` (add function after `_build_usage_cost_groups`, ~line 182)
- Test: `tests/test_usage_dashboard.py` (append a test class)

**Interfaces:**
- Consumes: existing `_build_usage_cost_groups(rows: list[dict], prices: dict) -> list[dict]`.
- Produces: `build_usage_cost_json(start: str, end: str, rows: list[dict], prices: dict) -> dict` returning
  `{"start": str, "end": str, "total_known_cost": float, "partial": bool, "unknown": bool, "groups": list[dict]}`.
  Each group is the dict already produced by `_build_usage_cost_groups` (JSON-serializable: label, proxy_key, integer counters, float `known_cost`/`base_cost`/`cache_cost`, bool `partial`/`unknown`, and a `models` list).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_usage_dashboard.py` (before the `if __name__` block):

```python
class BuildUsageCostJsonTests(unittest.TestCase):
    def test_shape_and_totals(self) -> None:
        from smart_proxy.usage import build_price_lookup
        from smart_proxy.usage_dashboard import build_usage_cost_json

        rows = [
            {
                "proxy_key": "sp-team", "group_name": None, "key_name": "Team",
                "provider": "anthropic", "model": "claude-sonnet-4-6",
                "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0,
                "web_search_requests": 0, "requests": 2,
            }
        ]
        prices = build_price_lookup([
            {
                "model_prefix": "claude-sonnet-4-6", "provider": "anthropic",
                "input_price": 3.00, "output_price": 15.00,
                "cache_read_price": 0.30, "cache_write_5m_price": 3.75,
                "cache_write_1h_price": 6.00, "updated_at": "2026-04-01T00:00:00Z",
            }
        ])

        out = build_usage_cost_json("2026-04-01", "2026-04-02", rows, prices)

        self.assertEqual(out["start"], "2026-04-01")
        self.assertEqual(out["end"], "2026-04-02")
        self.assertEqual(len(out["groups"]), 1)
        self.assertEqual(out["groups"][0]["label"], "Team")
        # 1M input @ $3/M + 1M output @ $15/M = $18.00
        self.assertEqual(out["total_known_cost"], 18.00)
        self.assertFalse(out["unknown"])

    def test_empty_rows(self) -> None:
        from smart_proxy.usage_dashboard import build_usage_cost_json
        out = build_usage_cost_json("2026-04-01", "2026-04-02", [], {})
        self.assertEqual(out["groups"], [])
        self.assertEqual(out["total_known_cost"], 0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_usage_dashboard.py::BuildUsageCostJsonTests -v`
Expected: FAIL — `ImportError: cannot import name 'build_usage_cost_json'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/smart_proxy/usage_dashboard.py` after `_build_usage_cost_groups` (line 182):

```python
def build_usage_cost_json(
    start: str, end: str, rows: list[dict], prices: dict
) -> dict:
    """JSON-serializable version of the usage dashboard data (for /api/usage)."""
    groups = _build_usage_cost_groups(rows, prices)
    total_known = sum(float(g["known_cost"]) for g in groups)
    return {
        "start": start,
        "end": end,
        "total_known_cost": round(total_known, 2),
        "partial": any(bool(g["partial"]) for g in groups),
        "unknown": any(bool(g["unknown"]) for g in groups),
        "groups": groups,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_usage_dashboard.py -v`
Expected: PASS (all existing tests + 2 new)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/usage_dashboard.py tests/test_usage_dashboard.py
git commit -m "feat(usage): build_usage_cost_json for the JSON dashboard API

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Extract `build_oauth_usage_history` from the handler (reuse for /api)

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` (extract body of `_oauth_usage_history_handler`, ~lines 2296-2385)
- Test: `tests/test_oauth_usage_history_builder.py` (create)

**Interfaces:**
- Consumes: existing module helpers `build_price_lookup`, `_span_days_between`, `_hours_between`, `_window_usage_block`, `WINDOW_USAGE_COUNTERS`, and `Database.list_anthropic_keys/list_oauth_window_log/list_oauth_window_usage/list_oauth_window_drops/list_oauth_window_usage_pending`.
- Produces: `async build_oauth_usage_history(db: Database, *, kind_filter: str | None, per_kind_limit: int) -> list[dict]` returning the `keys_out` list the handler used to build inline. `_oauth_usage_history_handler` now calls it. `/api/oauth/usage/history` (Task 5) will call it too.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_usage_history_builder.py
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import build_oauth_usage_history


class BuildOAuthUsageHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_oauth_keys_returns_empty(self) -> None:
        db = MagicMock()
        db.get_all_model_prices = AsyncMock(return_value=[])
        db.list_anthropic_keys = AsyncMock(return_value=[{"id": 1, "key_type": "api_key"}])
        out = await build_oauth_usage_history(db, kind_filter=None, per_kind_limit=50)
        self.assertEqual(out, [])

    async def test_oauth_key_without_windows(self) -> None:
        db = MagicMock()
        db.get_all_model_prices = AsyncMock(return_value=[])
        db.list_anthropic_keys = AsyncMock(
            return_value=[{"id": 7, "key_type": "oauth", "name": "acc", "status": "active"}]
        )
        db.list_oauth_window_log = AsyncMock(return_value=[])
        db.list_oauth_window_usage = AsyncMock(return_value=[])
        db.list_oauth_window_drops = AsyncMock(return_value=[])
        db.list_oauth_window_usage_pending = AsyncMock(return_value=[])

        out = await build_oauth_usage_history(db, kind_filter=None, per_kind_limit=50)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "7")
        self.assertEqual(out[0]["name"], "acc")
        self.assertEqual(out[0]["windows"], {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_oauth_usage_history_builder.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_oauth_usage_history'`

- [ ] **Step 3: Write minimal implementation**

In `src/smart_proxy/anthropic_proxy.py`, add this function immediately **above** `_oauth_usage_history_handler` (before line 2278). It is the current handler body, verbatim, minus the request/auth/response lines:

```python
async def build_oauth_usage_history(
    db: Database, *, kind_filter: str | None, per_kind_limit: int
) -> list[dict]:
    """Build the per-OAuth-key rate-limit window history (see oauth_window_log)."""
    prices = build_price_lookup(await db.get_all_model_prices())

    rows = await db.list_anthropic_keys()
    keys_out: list[dict] = []
    for row in rows:
        if row.get("key_type") != "oauth":
            continue
        key_id = str(row.get("id", ""))
        log_rows = await db.list_oauth_window_log(key_id)
        usage_rows = await db.list_oauth_window_usage(key_id)
        usage_by_window: dict[int, dict[str, dict]] = {}
        for u in usage_rows:
            usage_by_window.setdefault(u["window_id"], {})[u["model"]] = u
        windows: dict[str, list[dict]] = {}
        for log_row in log_rows:  # ordered window_kind ASC, resets_at ASC
            kind = log_row["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            bucket = windows.setdefault(kind, [])
            span = None
            if bucket:
                span = _span_days_between(
                    bucket[-1]["resets_at"], log_row["resets_at"]
                )
            bucket.append({
                "resets_at": log_row["resets_at"],
                "first_seen_at": log_row["first_seen_at"],
                "first_active_at": log_row["first_active_at"],
                "last_seen_at": log_row["last_seen_at"],
                "observations": log_row["observations"],
                "last_utilization": log_row["last_utilization"],
                "max_utilization": log_row["max_utilization"],
                "max_utilization_at": log_row["max_utilization_at"],
                "span_days_since_prev": span,
                "usage": _window_usage_block(
                    usage_by_window.get(log_row["id"]), prices),
            })
        for bucket in windows.values():
            bucket.reverse()  # newest first
            del bucket[per_kind_limit:]
        drop_rows = await db.list_oauth_window_drops(key_id)
        drops: dict[str, list[dict]] = {}
        for drop_row in drop_rows:  # ordered window_kind ASC, dropped_at ASC
            kind = drop_row["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            drops.setdefault(kind, []).append({
                "dropped_at": drop_row["dropped_at"],
                "prev_seen_at": drop_row["prev_seen_at"],
                "from_utilization": drop_row["from_utilization"],
                "to_utilization": drop_row["to_utilization"],
                "resets_at_claimed": drop_row["resets_at"],
                "hours_before_claimed_reset": _hours_between(
                    drop_row["dropped_at"], drop_row["resets_at"]
                ),
            })
        for bucket in drops.values():
            bucket.reverse()  # newest first
            del bucket[per_kind_limit:]
        pending_rows = await db.list_oauth_window_usage_pending(key_id)
        pending: dict[str, dict] = {}
        for p in pending_rows:
            kind = p["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            block = pending.setdefault(
                kind, {"models": {}, "updated_at": p["updated_at"]})
            block["models"][p["model"]] = {
                c: p[c] for c in WINDOW_USAGE_COUNTERS}
            if p["updated_at"] > block["updated_at"]:
                block["updated_at"] = p["updated_at"]
        keys_out.append({
            "id": key_id,
            "name": row.get("name"),
            "status": row.get("status"),
            "windows": windows,
            "drops": drops,
            "pending": pending,
        })
    return keys_out
```

Then replace the body of `_oauth_usage_history_handler` from `prices = build_price_lookup(...)` (line 2302) through the `for row in rows:` loop end (line 2380) with a single call. The handler becomes:

```python
async def _oauth_usage_history_handler(request: web.Request) -> web.Response:
    """GET observed rate-limit window history for OAuth keys.  (docstring unchanged)"""
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    db: Database = request.app["db"]

    if request.app.get("oauth_usage_require_auth") and not pool.check_auth(
        _extract_client_token(request)
    ):
        return web.json_response({"error": "unauthorized"}, status=401)

    kind_filter = request.query.get("kind") or None
    try:
        per_kind_limit = max(1, int(request.query.get("limit", "50")))
    except ValueError:
        per_kind_limit = 50

    keys_out = await build_oauth_usage_history(
        db, kind_filter=kind_filter, per_kind_limit=per_kind_limit
    )
    return web.json_response({
        "generated_at": _utc_now_iso(),
        "keys": keys_out,
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_oauth_usage_history_builder.py -v && python -m pytest tests/ -q`
Expected: new tests PASS; full suite still green (no behavior change to the endpoint).

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_oauth_usage_history_builder.py
git commit -m "refactor(oauth): extract build_oauth_usage_history for reuse by /api

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `dashboard_api.py` — auth, session, `/api/usage`, `/api/keys`

**Files:**
- Create: `src/smart_proxy/dashboard_api.py`
- Test: `tests/test_dashboard_api.py` (create)

**Interfaces:**
- Consumes: `usage_dashboard._usage_date_range`, `usage_dashboard.build_usage_cost_json`, `usage.build_price_lookup`; app keys `anthropic_pool`, `db`; `Database.query_usage_by_key_model`, `Database.get_all_model_prices`, `Database.list_proxy_keys`.
- Produces:
  - `_dashboard_token(request) -> str` — header (`Bearer`/`x-api-key`) → cookie `dash_token` → `?key=`.
  - `_dashboard_authorized(request) -> bool` — `pool.check_auth(_dashboard_token(request))`.
  - `register_dashboard_api(app, *, static_dir=None) -> None` — registers all `/api/*` routes (static serving is added in Task 6). Route handlers `_api_session`, `_api_usage`, `_api_keys` are registered here; Task 5 adds the rest inside the same function.
  - Cookie contract: `POST /api/session {"token": "sp-…"}` on success sets `dash_token` (httpOnly, samesite=Lax) and returns `{"ok": true}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dashboard_api.py
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request

from smart_proxy import dashboard_api


def _pool(valid: str = "sp-team"):
    pool = MagicMock()
    pool.check_auth.side_effect = lambda t: t == valid
    return pool


class DashboardTokenTests(unittest.TestCase):
    def test_prefers_header_then_cookie_then_query(self) -> None:
        pool = _pool()
        hdr = make_mocked_request("GET", "/api/keys", app={"anthropic_pool": pool},
                                  headers={"Authorization": "Bearer sp-team"})
        self.assertTrue(dashboard_api._dashboard_authorized(hdr))

        cookie = make_mocked_request("GET", "/api/keys", app={"anthropic_pool": pool},
                                     headers={"Cookie": "dash_token=sp-team"})
        self.assertTrue(dashboard_api._dashboard_authorized(cookie))

        query = make_mocked_request("GET", "/api/keys?key=sp-team", app={"anthropic_pool": pool})
        self.assertTrue(dashboard_api._dashboard_authorized(query))

        anon = make_mocked_request("GET", "/api/keys", app={"anthropic_pool": pool})
        self.assertFalse(dashboard_api._dashboard_authorized(anon))


class ApiSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_token_sets_cookie(self) -> None:
        req = make_mocked_request("POST", "/api/session", app={"anthropic_pool": _pool()})
        req.json = AsyncMock(return_value={"token": "sp-team"})
        resp = await dashboard_api._api_session(req)
        self.assertEqual(resp.status, 200)
        self.assertIn("dash_token=sp-team", resp.headers.get("Set-Cookie", ""))

    async def test_invalid_token_401(self) -> None:
        req = make_mocked_request("POST", "/api/session", app={"anthropic_pool": _pool()})
        req.json = AsyncMock(return_value={"token": "nope"})
        resp = await dashboard_api._api_session(req)
        self.assertEqual(resp.status, 401)


class ApiUsageKeysTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_unauthorized(self) -> None:
        req = make_mocked_request("GET", "/api/usage", app={"anthropic_pool": _pool(), "db": MagicMock()})
        resp = await dashboard_api._api_usage(req)
        self.assertEqual(resp.status, 401)

    async def test_usage_returns_groups(self) -> None:
        db = MagicMock()
        db.query_usage_by_key_model = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/usage?start=2026-04-01&end=2026-04-02",
            app={"anthropic_pool": _pool(), "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_usage(req)
        self.assertEqual(resp.status, 200)
        import json
        payload = json.loads(resp.body)
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["start"], "2026-04-01")

    async def test_keys_redacts_to_prefix(self) -> None:
        db = MagicMock()
        db.list_proxy_keys = AsyncMock(return_value=[
            {"key": "sp-team-secret-123", "name": "Team", "active": 1, "created_at": "2026-04-01"},
        ])
        req = make_mocked_request("GET", "/api/keys",
                                  app={"anthropic_pool": _pool(), "db": db},
                                  headers={"Authorization": "Bearer sp-team"})
        resp = await dashboard_api._api_keys(req)
        import json
        payload = json.loads(resp.body)
        self.assertEqual(payload["keys"][0]["key_prefix"], "sp-team-secr")
        self.assertTrue(payload["keys"][0]["active"])
        self.assertNotIn("key", payload["keys"][0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboard_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'smart_proxy.dashboard_api'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/smart_proxy/dashboard_api.py
"""Dashboard JSON API (``/api/*``) and SPA static serving (``/_app/``).

Registered on the Anthropic proxy app before the catch-all proxy route. Auth
is a single ``sp-*`` token carried by header, the ``dash_token`` cookie, or a
``?key=`` query param, and re-validated on every request via
``AnthropicKeyPool.check_auth`` — so the cookie needs no signing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web

from smart_proxy.db import Database
from smart_proxy.usage import build_price_lookup
from smart_proxy.usage_dashboard import _usage_date_range, build_usage_cost_json

_STATIC_APP_DIR = Path(__file__).resolve().parent / "static" / "app"
_COOKIE_NAME = "dash_token"


def _dashboard_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        header_tok = auth[7:].strip()
    else:
        header_tok = request.headers.get("x-api-key", "").strip()
    return (
        header_tok
        or request.cookies.get(_COOKIE_NAME, "").strip()
        or request.query.get("key", "").strip()
    )


def _dashboard_authorized(request: web.Request) -> bool:
    pool = request.app["anthropic_pool"]
    return bool(pool.check_auth(_dashboard_token(request)))


def _unauthorized() -> web.Response:
    return web.json_response({"error": "unauthorized"}, status=401)


async def _api_session(request: web.Request) -> web.Response:
    pool = request.app["anthropic_pool"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = str(body.get("token", "")).strip()
    if not token or not pool.check_auth(token):
        return _unauthorized()
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        _COOKIE_NAME, token, httponly=True, samesite="Lax", max_age=30 * 24 * 3600
    )
    return resp


async def _api_usage(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    date_range = _usage_date_range(request)
    if isinstance(date_range, web.Response):
        return date_range
    start, end = date_range
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "usage database unavailable"}, status=500)
    rows = await db.query_usage_by_key_model(start, end)
    prices = build_price_lookup(await db.get_all_model_prices())
    return web.json_response(build_usage_cost_json(start, end, rows, prices))


async def _api_keys(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    rows = await db.list_proxy_keys()
    keys = [
        {
            "key_prefix": str(r["key"])[:12],
            "name": r.get("name") or "",
            "active": bool(r["active"]),
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]
    return web.json_response({"keys": keys})


def register_dashboard_api(
    app: web.Application, *, static_dir: Path | None = None
) -> None:
    """Register /api/* routes (and, from Task 6, /_app/ static serving)."""
    app.router.add_post("/api/session", _api_session)
    app.router.add_get("/api/usage", _api_usage)
    app.router.add_get("/api/keys", _api_keys)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dashboard_api.py -v`
Expected: PASS (all classes)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_api.py
git commit -m "feat(dashboard): /api session, usage, keys with cookie auth

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `dashboard_api.py` — oauth/compat reads + reload/key-toggle actions

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py`
- Test: `tests/test_dashboard_api.py` (append)

**Interfaces:**
- Consumes: `anthropic_proxy._build_oauth_usage_payload(pool, client, db, include_inactive_oauth=...) -> tuple[list, object|None]` and `anthropic_proxy.build_oauth_usage_history(db, *, kind_filter, per_kind_limit)` (Task 3), imported **lazily inside handlers** to avoid a circular import (anthropic_proxy imports this module in Task 6); app keys `http_client`, `openai_compat_stats`; `Database.set_proxy_key_active` (Task 1); `AnthropicKeyPool.reload()`, `.available`.
- Produces new routes registered in `register_dashboard_api`: `GET /api/oauth/usage`, `GET /api/oauth/usage/history`, `GET /api/openai-compat/stats`, `POST /api/reload`, `POST /api/keys/{prefix}/active`.
- `POST /api/keys/{prefix}/active` body `{"active": bool}` → toggles then `await pool.reload()` so the change takes effect immediately; returns `{"ok": true, "key_prefix": str, "active": bool}` or 404.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard_api.py`:

```python
class ApiActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_requires_auth(self) -> None:
        req = make_mocked_request("POST", "/api/reload", app={"anthropic_pool": _pool()})
        resp = await dashboard_api._api_reload(req)
        self.assertEqual(resp.status, 401)

    async def test_reload_calls_pool(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        pool.available = 4
        req = make_mocked_request("POST", "/api/reload", app={"anthropic_pool": pool},
                                  headers={"Authorization": "Bearer sp-team"})
        resp = await dashboard_api._api_reload(req)
        import json
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["active"], 4)
        pool.reload.assert_awaited_once()

    async def test_key_toggle_unknown_prefix_404(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        db = MagicMock()
        db.set_proxy_key_active = AsyncMock(return_value=None)
        req = make_mocked_request(
            "POST", "/api/keys/sp-x/active",
            app={"anthropic_pool": pool, "db": db},
            headers={"Authorization": "Bearer sp-team"},
            match_info={"prefix": "sp-x"},
        )
        req.json = AsyncMock(return_value={"active": False})
        resp = await dashboard_api._api_key_active(req)
        self.assertEqual(resp.status, 404)

    async def test_key_toggle_ok_reloads(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        db = MagicMock()
        db.set_proxy_key_active = AsyncMock(return_value="sp-team-secret-123")
        req = make_mocked_request(
            "POST", "/api/keys/sp-team-secr/active",
            app={"anthropic_pool": pool, "db": db},
            headers={"Authorization": "Bearer sp-team"},
            match_info={"prefix": "sp-team-secr"},
        )
        req.json = AsyncMock(return_value={"active": True})
        resp = await dashboard_api._api_key_active(req)
        import json
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.body)["active"])
        db.set_proxy_key_active.assert_awaited_once_with("sp-team-secr", True)
        pool.reload.assert_awaited_once()

    async def test_compat_stats_snapshot(self) -> None:
        stats = MagicMock()
        stats.snapshot.return_value = {"requests": 3}
        req = make_mocked_request(
            "GET", "/api/openai-compat/stats",
            app={"anthropic_pool": _pool(), "openai_compat_stats": stats},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_compat_stats(req)
        import json
        self.assertEqual(json.loads(resp.body)["requests"], 3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboard_api.py::ApiActionsTests -v`
Expected: FAIL — `AttributeError: module 'smart_proxy.dashboard_api' has no attribute '_api_reload'`

- [ ] **Step 3: Write minimal implementation**

Add these handlers to `src/smart_proxy/dashboard_api.py` (before `register_dashboard_api`):

```python
def _truthy(request: web.Request, name: str) -> bool:
    return request.query.get(name, "").strip().lower() in ("1", "true", "yes")


async def _api_oauth_usage(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    from smart_proxy.anthropic_proxy import _build_oauth_usage_payload

    pool = request.app["anthropic_pool"]
    client = request.app["http_client"]
    db = request.app["db"]
    keys, last_failure = await _build_oauth_usage_payload(
        pool, client, db, include_inactive_oauth=_truthy(request, "include_inactive")
    )
    payload: dict = {"keys": keys}
    if last_failure is not None:
        payload["last_failure"] = last_failure
    return web.json_response(payload)


async def _api_oauth_history(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    from smart_proxy.anthropic_proxy import build_oauth_usage_history

    db = request.app["db"]
    kind_filter = request.query.get("kind") or None
    try:
        per_kind_limit = max(1, int(request.query.get("limit", "50")))
    except ValueError:
        per_kind_limit = 50
    keys_out = await build_oauth_usage_history(
        db, kind_filter=kind_filter, per_kind_limit=per_kind_limit
    )
    return web.json_response({"keys": keys_out})


async def _api_compat_stats(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    stats = request.app.get("openai_compat_stats")
    if stats is None:
        return web.json_response({"error": "openai-compat disabled"}, status=404)
    return web.json_response(stats.snapshot())


async def _api_reload(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    pool = request.app["anthropic_pool"]
    await pool.reload()
    return web.json_response({"status": "reloaded", "active": pool.available})


async def _api_key_active(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    prefix = request.match_info.get("prefix", "").strip()
    try:
        body = await request.json()
    except Exception:
        body = {}
    active = bool(body.get("active"))
    full = await db.set_proxy_key_active(prefix, active)
    if full is None:
        return web.json_response({"error": "key not found or ambiguous"}, status=404)
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "key_prefix": prefix, "active": active})
```

Then extend `register_dashboard_api` — add these lines to its body:

```python
    app.router.add_get("/api/oauth/usage", _api_oauth_usage)
    app.router.add_get("/api/oauth/usage/history", _api_oauth_history)
    app.router.add_get("/api/openai-compat/stats", _api_compat_stats)
    app.router.add_post("/api/reload", _api_reload)
    app.router.add_post("/api/keys/{prefix}/active", _api_key_active)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_dashboard_api.py -v`
Expected: PASS (Task 4 + Task 5 classes)

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py tests/test_dashboard_api.py
git commit -m "feat(dashboard): /api oauth+compat reads and reload/key-toggle actions

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: SPA static serving + wire into `create_app`

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` (add `/_app/` serving to `register_dashboard_api`)
- Modify: `src/smart_proxy/anthropic_proxy.py` (import + call `register_dashboard_api` in `create_app`, ~line 3139)
- Test: `tests/test_dashboard_api.py` (append static-serving tests)

**Interfaces:**
- Consumes: `register_dashboard_api(app, *, static_dir=None)` from Task 4/5.
- Produces: `GET /_app/` and `GET /_app/{tail:.*}` serve files from `static_dir` (default `_STATIC_APP_DIR`); unknown sub-paths fall back to `index.html` (client routing); a missing build returns 503. `create_app` calls `register_dashboard_api(app)` before the catch-all routes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dashboard_api.py`:

```python
class SpaStaticTests(unittest.IsolatedAsyncioTestCase):
    def _app_with_static(self, tmp: Path):
        import warnings
        from aiohttp import web
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="It is recommended to use web.AppKey")
            app = web.Application()
            dashboard_api.register_dashboard_api(app, static_dir=tmp)
        return app

    async def test_index_served_and_fallback(self) -> None:
        import tempfile
        from aiohttp.test_utils import TestClient, TestServer
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "index.html").write_text("<!doctype html><title>SPA</title>")
            (tmp / "app.js").write_text("console.log(1)")
            app = self._app_with_static(tmp)
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/_app/")
                self.assertEqual(r.status, 200)
                self.assertIn("SPA", await r.text())
                r = await client.get("/_app/app.js")
                self.assertEqual(r.status, 200)
                self.assertIn("console.log", await r.text())
                # unknown path → index fallback (client-side routing)
                r = await client.get("/_app/anything/deep")
                self.assertEqual(r.status, 200)
                self.assertIn("SPA", await r.text())

    async def test_missing_build_returns_503(self) -> None:
        import tempfile
        from aiohttp.test_utils import TestClient, TestServer
        with tempfile.TemporaryDirectory() as d:
            app = self._app_with_static(Path(d))  # empty dir, no index.html
            async with TestClient(TestServer(app)) as client:
                r = await client.get("/_app/")
                self.assertEqual(r.status, 503)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_dashboard_api.py::SpaStaticTests -v`
Expected: FAIL — 404 for `/_app/` (route not registered yet).

- [ ] **Step 3: Write minimal implementation**

In `src/smart_proxy/dashboard_api.py`, add a handler factory and register it. Change `register_dashboard_api` to store the dir and add the `/_app/` routes:

```python
def _make_spa_handler(static_dir: Path) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def _spa(request: web.Request) -> web.StreamResponse:
        index = static_dir / "index.html"
        tail = request.match_info.get("tail", "")
        if tail:
            candidate = (static_dir / tail).resolve()
            if candidate.is_file() and static_dir.resolve() in candidate.parents:
                return web.FileResponse(candidate)
        if not index.is_file():
            return web.Response(status=503, text="dashboard not built")
        return web.FileResponse(index)

    return _spa
```

And append to `register_dashboard_api` (after the `/api/*` routes):

```python
    spa_dir = static_dir or _STATIC_APP_DIR
    spa = _make_spa_handler(spa_dir)
    app.router.add_get("/_app/", spa)
    app.router.add_get("/_app/{tail:.*}", spa)
```

In `src/smart_proxy/anthropic_proxy.py`, add the import near the other dashboard import (top of file, next to `from smart_proxy.usage_dashboard import ...`):

```python
from smart_proxy.dashboard_api import register_dashboard_api
```

And in `create_app`, right after the existing `register_usage_dashboard(...)` block (line 3139) and **before** `app.router.add_route("*", "/", _root_handler)`:

```python
    register_dashboard_api(app)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_dashboard_api.py -v && python -m pytest tests/ -q`
Expected: SPA tests PASS; full suite green (proxy app still builds — a quick guard: `python -c "from smart_proxy.anthropic_proxy import create_app; create_app('x.db')"` prints nothing and exits 0).

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/dashboard_api.py src/smart_proxy/anthropic_proxy.py tests/test_dashboard_api.py
git commit -m "feat(dashboard): serve SPA under /_app/ and wire into create_app

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Frontend scaffold — `web/` project, shell, login gate, API client

**Files (all Create):**
- `web/package.json`, `web/svelte.config.js`, `web/vite.config.ts`, `web/tsconfig.json`, `web/index.html`
- `web/src/main.ts`, `web/src/app.css`, `web/src/App.svelte`, `web/src/lib/api.ts`
- `web/src/vite-env.d.ts`

**Interfaces:**
- Produces: a Vite project that builds to `../src/smart_proxy/static/app` with `base: '/_app/'`. `lib/api.ts` exports `apiGet<T>`, `apiPost<T>`, `login`, and `class Unauthorized`. `App.svelte` gates on auth via `GET /api/keys`, shows a login form posting to `/api/session`, then renders tab views (imported in Tasks 8-9).

- [ ] **Step 1: Create project config**

`web/package.json`:
```json
{
  "name": "smart-proxy-dashboard",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "chart.js": "^4.4.0"
  },
  "devDependencies": {
    "@sveltejs/vite-plugin-svelte": "^4.0.0",
    "svelte": "^5.0.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0"
  }
}
```

`web/svelte.config.js`:
```js
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte'
export default { preprocess: vitePreprocess() }
```

`web/vite.config.ts`:
```ts
import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

export default defineConfig({
  plugins: [svelte()],
  base: '/_app/',
  build: {
    outDir: '../src/smart_proxy/static/app',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8090',
    },
  },
})
```

`web/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ESNext",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "verbatimModuleSyntax": true
  },
  "include": ["src/**/*.ts", "src/**/*.svelte", "src/vite-env.d.ts"]
}
```

`web/src/vite-env.d.ts`:
```ts
/// <reference types="svelte" />
/// <reference types="vite/client" />
```

`web/index.html`:
```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Keys AI — Dashboard</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
```

- [ ] **Step 2: Create the app entry, styles, API client**

`web/src/main.ts`:
```ts
import { mount } from 'svelte'
import App from './App.svelte'
import './app.css'

export default mount(App, { target: document.getElementById('app')! })
```

`web/src/app.css` (palette carried from the current `/_usage`):
```css
:root {
  --ink: #111827; --muted: #6b7280; --line: #e5e7eb; --bg: #f9fafb;
  --accent: #111827;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  margin: 0; color: var(--ink); }
header { display: flex; align-items: center; gap: 24px;
  padding: 14px 24px; border-bottom: 1px solid var(--line); }
nav button { border: 0; background: none; padding: 8px 10px; cursor: pointer;
  color: var(--muted); font-size: 14px; }
nav button.active { color: var(--ink); font-weight: 600; }
main { padding: 24px; }
.pad { padding: 24px; }
.err { color: #b91c1c; }
.login { max-width: 320px; margin: 80px auto; display: grid; gap: 12px; }
input { padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 6px; }
button.primary, .login button { padding: 9px 14px; border: 0; border-radius: 6px;
  background: var(--accent); color: #fff; cursor: pointer; }
table { border-collapse: collapse; width: 100%; margin-top: 16px; }
th, td { border-bottom: 1px solid var(--line); padding: 8px 10px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
th { background: var(--bg); font-size: 12px; text-transform: uppercase; color: var(--muted); }
tr.model td:first-child { padding-left: 28px; color: var(--muted); }
pre { background: var(--bg); padding: 12px; border-radius: 6px; overflow: auto; font-size: 12px; }
```

`web/src/lib/api.ts`:
```ts
export class Unauthorized extends Error {}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { credentials: 'include', ...init })
  if (r.status === 401) throw new Unauthorized()
  if (!r.ok) throw new Error(`${path}: ${r.status}`)
  return (await r.json()) as T
}

export function apiGet<T>(path: string): Promise<T> {
  return req<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return req<T>(path, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
}

export function login(token: string): Promise<{ ok: boolean }> {
  return apiPost('/api/session', { token })
}
```

- [ ] **Step 3: Create the shell with login gate + tabs**

`web/src/App.svelte`:
```svelte
<script lang="ts">
  import { apiGet, login } from './lib/api'
  import UsageView from './views/UsageView.svelte'
  import OAuthView from './views/OAuthView.svelte'
  import CompatView from './views/CompatView.svelte'
  import KeysView from './views/KeysView.svelte'

  let checking = $state(true)
  let authed = $state(false)
  let token = $state('')
  let loginError = $state('')
  let tab = $state<'usage' | 'oauth' | 'compat' | 'keys'>('usage')

  async function probe() {
    try { await apiGet('/api/keys'); authed = true }
    catch { authed = false }
    finally { checking = false }
  }
  probe()

  async function doLogin(e: Event) {
    e.preventDefault()
    loginError = ''
    try { await login(token.trim()); authed = true; token = '' }
    catch { loginError = 'Invalid token' }
  }
</script>

{#if checking}
  <p class="pad">Loading…</p>
{:else if !authed}
  <form class="login" onsubmit={doLogin}>
    <h1>Keys AI</h1>
    <input type="password" placeholder="sp- token" bind:value={token} />
    <button type="submit">Sign in</button>
    {#if loginError}<p class="err">{loginError}</p>{/if}
  </form>
{:else}
  <header>
    <strong>Keys AI</strong>
    <nav>
      <button class:active={tab === 'usage'} onclick={() => (tab = 'usage')}>Usage</button>
      <button class:active={tab === 'oauth'} onclick={() => (tab = 'oauth')}>OAuth</button>
      <button class:active={tab === 'compat'} onclick={() => (tab = 'compat')}>Compat</button>
      <button class:active={tab === 'keys'} onclick={() => (tab = 'keys')}>Keys</button>
    </nav>
  </header>
  <main>
    {#if tab === 'usage'}<UsageView />
    {:else if tab === 'oauth'}<OAuthView />
    {:else if tab === 'compat'}<CompatView />
    {:else}<KeysView />{/if}
  </main>
{/if}
```

- [ ] **Step 4: Create placeholder views so the build compiles**

Create four minimal stubs (Tasks 8-9 fill them in). Each: `web/src/views/UsageView.svelte`, `OAuthView.svelte`, `CompatView.svelte`, `KeysView.svelte` with:
```svelte
<p class="pad">…</p>
```

- [ ] **Step 5: Install, build, verify output lands in static/app**

Run:
```bash
cd web && npm install && npm run build
ls ../src/smart_proxy/static/app/index.html
```
Expected: `npm run build` succeeds; `index.html` exists under `src/smart_proxy/static/app/`. (Return to repo root afterward: `cd ..`.)

- [ ] **Step 6: Commit (source only — build output is gitignored in Task 10)**

```bash
git add web/package.json web/package-lock.json web/svelte.config.js web/vite.config.ts \
        web/tsconfig.json web/index.html web/src/main.ts web/src/app.css \
        web/src/vite-env.d.ts web/src/App.svelte web/src/lib/api.ts web/src/views
git commit -m "feat(web): Svelte+Vite scaffold, login gate, API client

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Usage view — table + Chart.js cost-by-key bar

**Files:**
- Modify: `web/src/views/UsageView.svelte`

**Interfaces:**
- Consumes: `apiGet` from `lib/api`; `GET /api/usage?start&end` returning `build_usage_cost_json` shape (`start`, `end`, `total_known_cost`, `groups[]` where each group has `label`, `requests`, `input_tokens`, `output_tokens`, `known_cost`, `models[]` with `provider`, `model`, `requests`, `input_tokens`, `output_tokens`, `cost`).
- Note: the chart is **cost-by-key** (data present in `groups`), not a per-day time series — a per-day series would need a new DB query and is out of scope for this cut.

- [ ] **Step 1: Implement the view**

`web/src/views/UsageView.svelte`:
```svelte
<script lang="ts">
  import { onMount } from 'svelte'
  import Chart from 'chart.js/auto'
  import { apiGet } from '../lib/api'

  type Model = { provider: string; model: string; requests: number;
    input_tokens: number; output_tokens: number; cost: number | null }
  type Group = { label: string; requests: number; input_tokens: number;
    output_tokens: number; known_cost: number; unknown: boolean; models: Model[] }
  type Usage = { start: string; end: string; total_known_cost: number; groups: Group[] }

  const iso = (ms: number) => new Date(ms).toISOString().slice(0, 10)
  let start = $state(iso(Date.now() - 6 * 864e5))
  let end = $state(iso(Date.now()))
  let data = $state<Usage | null>(null)
  let error = $state('')
  let canvas: HTMLCanvasElement
  let chart: Chart | undefined

  async function load() {
    error = ''
    try {
      data = await apiGet<Usage>(`/api/usage?start=${start}&end=${end}`)
      draw()
    } catch (e) {
      error = String(e)
    }
  }

  function draw() {
    if (!data || !canvas) return
    chart?.destroy()
    chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: data.groups.map((g) => g.label),
        datasets: [{ label: 'Cost $', data: data.groups.map((g) => Number(g.known_cost.toFixed(2))) }],
      },
      options: { responsive: true, plugins: { legend: { display: false } } },
    })
  }

  onMount(load)
</script>

<form onsubmit={(e) => { e.preventDefault(); load() }}>
  <input type="date" bind:value={start} />
  <input type="date" bind:value={end} />
  <button class="primary" type="submit">Update</button>
</form>

{#if error}<p class="err">{error}</p>{/if}
<canvas bind:this={canvas} height="110"></canvas>

{#if data}
  <p class="pad">Total known cost: <strong>${data.total_known_cost.toFixed(2)}</strong></p>
  <table>
    <thead><tr><th>Key / Model</th><th>Req</th><th>In</th><th>Out</th><th>Cost</th></tr></thead>
    <tbody>
      {#each data.groups as g}
        <tr>
          <td><strong>{g.label}</strong></td>
          <td>{g.requests.toLocaleString()}</td>
          <td>{g.input_tokens.toLocaleString()}</td>
          <td>{g.output_tokens.toLocaleString()}</td>
          <td>{g.unknown ? '?' : '$' + g.known_cost.toFixed(2)}</td>
        </tr>
        {#each g.models as m}
          <tr class="model">
            <td>{m.provider} / {m.model}</td>
            <td>{m.requests.toLocaleString()}</td>
            <td>{m.input_tokens.toLocaleString()}</td>
            <td>{m.output_tokens.toLocaleString()}</td>
            <td>{m.cost == null ? '?' : '$' + m.cost.toFixed(2)}</td>
          </tr>
        {/each}
      {/each}
    </tbody>
  </table>
{/if}
```

- [ ] **Step 2: Build to verify it compiles**

Run: `cd web && npm run build && cd ..`
Expected: build succeeds, no TypeScript/Svelte errors.

- [ ] **Step 3: Commit**

```bash
git add web/src/views/UsageView.svelte
git commit -m "feat(web): usage view with cost table and cost-by-key chart

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: OAuth, Compat, and Keys views (with actions)

**Files:**
- Modify: `web/src/views/OAuthView.svelte`, `web/src/views/CompatView.svelte`, `web/src/views/KeysView.svelte`

**Interfaces:**
- Consumes: `apiGet`, `apiPost`; `GET /api/oauth/usage` (`{keys:[...]}`), `GET /api/openai-compat/stats` (arbitrary object), `GET /api/keys` (`{keys:[{key_prefix,name,active,created_at}]}`), `POST /api/keys/{prefix}/active`, `POST /api/reload`.
- Note: the OAuth payload item shape comes from `_build_oauth_usage_payload`. The reliable render is the JSON dump; the utilization chart is best-effort and reads `seven_day.utilization` guardedly — when implementing, confirm the field path against a live `/api/oauth/usage` response and adjust the accessor if needed.

- [ ] **Step 1: Implement KeysView (actions)**

`web/src/views/KeysView.svelte`:
```svelte
<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet, apiPost } from '../lib/api'

  type Key = { key_prefix: string; name: string; active: boolean; created_at: string | null }
  let keys = $state<Key[]>([])
  let reloadMsg = $state('')
  let error = $state('')

  async function load() {
    try { keys = (await apiGet<{ keys: Key[] }>('/api/keys')).keys }
    catch (e) { error = String(e) }
  }
  async function toggle(k: Key) {
    error = ''
    try {
      await apiPost(`/api/keys/${encodeURIComponent(k.key_prefix)}/active`, { active: !k.active })
      await load()
    } catch (e) { error = String(e) }
  }
  async function reload() {
    error = ''
    try {
      const r = await apiPost<{ status: string; active: number }>('/api/reload')
      reloadMsg = `${r.status} — ${r.active} active`
    } catch (e) { error = String(e) }
  }
  onMount(load)
</script>

<button class="primary" onclick={reload}>Reload proxy</button>
<span class="pad">{reloadMsg}</span>
{#if error}<p class="err">{error}</p>{/if}

<table>
  <thead><tr><th>Key</th><th>Name</th><th>Active</th><th></th></tr></thead>
  <tbody>
    {#each keys as k}
      <tr>
        <td>{k.key_prefix}…</td>
        <td>{k.name}</td>
        <td>{k.active ? 'yes' : 'no'}</td>
        <td><button onclick={() => toggle(k)}>{k.active ? 'Disable' : 'Enable'}</button></td>
      </tr>
    {/each}
  </tbody>
</table>
```

- [ ] **Step 2: Implement CompatView**

`web/src/views/CompatView.svelte`:
```svelte
<script lang="ts">
  import { onMount } from 'svelte'
  import { apiGet } from '../lib/api'

  let stats = $state<Record<string, unknown> | null>(null)
  let error = $state('')
  onMount(async () => {
    try { stats = await apiGet('/api/openai-compat/stats') }
    catch (e) { error = String(e) }
  })
</script>

{#if error}<p class="err">{error}</p>{/if}
{#if stats}<pre>{JSON.stringify(stats, null, 2)}</pre>{/if}
```

- [ ] **Step 3: Implement OAuthView**

`web/src/views/OAuthView.svelte`:
```svelte
<script lang="ts">
  import { onMount } from 'svelte'
  import Chart from 'chart.js/auto'
  import { apiGet } from '../lib/api'

  type KeyUsage = { name?: string; error?: string; seven_day?: { utilization?: number } }
  let payload = $state<{ keys: KeyUsage[] } | null>(null)
  let error = $state('')
  let canvas: HTMLCanvasElement
  let chart: Chart | undefined

  async function load() {
    try {
      payload = await apiGet<{ keys: KeyUsage[] }>('/api/oauth/usage')
      const rows = (payload.keys ?? []).filter((k) => !k.error)
      chart?.destroy()
      chart = new Chart(canvas, {
        type: 'bar',
        data: {
          labels: rows.map((k) => k.name ?? '?'),
          datasets: [{ label: '7d util %', data: rows.map((k) => Math.round((k.seven_day?.utilization ?? 0) * 100)) }],
        },
        options: { plugins: { legend: { display: false } }, scales: { y: { max: 100 } } },
      })
    } catch (e) { error = String(e) }
  }
  onMount(load)
</script>

{#if error}<p class="err">{error}</p>{/if}
<canvas bind:this={canvas} height="110"></canvas>
{#if payload}<pre>{JSON.stringify(payload, null, 2)}</pre>{/if}
```

- [ ] **Step 4: Build to verify all views compile**

Run: `cd web && npm run build && cd ..`
Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
git add web/src/views/OAuthView.svelte web/src/views/CompatView.svelte web/src/views/KeysView.svelte
git commit -m "feat(web): oauth, compat, and keys (with reload/toggle) views

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Ignore build output, deploy build step, docs

**Files:**
- Modify/Create: `.gitignore`
- Modify: `deploy.sh`
- Modify: `README.md`, `.env.example`

**Interfaces:**
- Produces: git ignores the Vite output and node deps; `deploy.sh` builds the frontend on the deploying machine before shipping source; docs mention the `/_app/` dashboard.

- [ ] **Step 1: Ignore build output and node deps**

Append to `.gitignore` (create it if absent):
```
# Frontend
web/node_modules/
web/dist/
src/smart_proxy/static/app/
```

- [ ] **Step 2: Verify the build output is not tracked**

Run: `git status --porcelain src/smart_proxy/static/app web/node_modules`
Expected: no output (nothing staged/tracked under those paths).

- [ ] **Step 3: Add the frontend build to `deploy.sh`**

Read `deploy.sh`, then insert this block **before** the step that copies/rsyncs the source tree to the host (so the freshly built assets ship with it):

```bash
# --- Build the dashboard SPA (needs node locally; prod host stays node-free) ---
if [ -d web ]; then
  echo "Building dashboard SPA..."
  ( cd web && npm ci && npm run build )
fi
```

- [ ] **Step 4: Document the dashboard**

In `README.md`, add a short line under the Anthropic proxy section:
```
The operator dashboard (usage, OAuth quotas, compat stats, key toggles) is a
Svelte SPA served at `/_app/`. Sign in with an `sp-*` proxy key. It is built
from `web/` into `src/smart_proxy/static/app/` at deploy time (`npm run build`).
```

In `.env.example`, add a comment near the Anthropic proxy block:
```
# The /_app/ dashboard authenticates with an sp-* proxy key (POST /api/session).
```

- [ ] **Step 5: Commit**

```bash
git add .gitignore deploy.sh README.md .env.example
git commit -m "chore: gitignore SPA build, add deploy build step, document /_app/

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Run the whole backend suite: `python -m pytest tests/ -q` → all green.
- [ ] Build the frontend: `cd web && npm run build && cd ..` → succeeds, `src/smart_proxy/static/app/index.html` present.
- [ ] Smoke the app factory: `python -c "from smart_proxy.anthropic_proxy import create_app; create_app('x.db')"` → exits 0.
- [ ] Manual: run the proxy, open `/_app/`, sign in with a valid `sp-*` key, confirm the four tabs load and Reload / key toggle work. `/_usage` still renders (kept until parity).

## Self-Review notes

- **Spec coverage:** repo layout (Task 7), aiohttp `/api/*` + static (Tasks 4-6), cookie auth (Task 4, re-validated per request), usage/oauth/compat/keys reads (Tasks 4-5), reload + key-toggle actions (Task 5), Svelte+Vite+Chart.js+CSS-tokens frontend (Tasks 7-9), `/_app/` coexisting with `/_usage` (Task 6 + Global Constraints), deploy build step (Task 10), backend tests mirroring `test_usage_dashboard.py` (Tasks 1-6). OAuth **history** endpoint is wired (`/api/oauth/usage/history`, Tasks 3+5) though the first-cut OAuth view charts current utilization, not history — history rendering is available to add without new backend work.
- **Deviation from spec (documented):** cookie is unsigned but re-validated every request via `pool.check_auth`, which is equivalent security to a signed cookie here and avoids a crypto dependency (the spec left the exact mechanism to the plan).
- **Type consistency:** `set_proxy_key_active(prefix, active)->str|None` (Task 1) is consumed identically in Task 5; `build_usage_cost_json(start,end,rows,prices)` (Task 2) consumed in Task 4; `build_oauth_usage_history(db,*,kind_filter,per_kind_limit)` (Task 3) consumed in Task 5; the `/api/usage` JSON field names used in `UsageView` (Task 8) match `build_usage_cost_json` / `_build_usage_cost_groups` output.
