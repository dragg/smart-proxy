# tests/test_dashboard_anthropic_keys_api.py
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from smart_proxy import dashboard_api  # noqa: E402
from smart_proxy import anthropic_proxy  # noqa: E402

ADMIN = "test-dashboard-admin-secret"

# Nearly every endpoint in this file mutates the pool, so the shared credential
# is the admin secret. That a plain sp- key can still *read* is covered in
# tests/test_dashboard_admin_secret.py.
AUTH = {"Authorization": f"Bearer {ADMIN}"}


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
        "rate_limit_tier": "max_20x", "name": "acct", "role": "primary",
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-02T00:00:00+00:00",
    }
    row.update(over)
    return row


class AnthropicKeysListTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_401(self) -> None:
        req = make_mocked_request(
            "GET", "/api/anthropic/keys",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": MagicMock()},
        )
        resp = await dashboard_api._api_anthropic_keys(req)
        self.assertEqual(resp.status, 401)

    async def test_lists_keys_without_token_material(self) -> None:
        db = MagicMock()
        db.list_anthropic_keys = AsyncMock(return_value=[_key_row()])
        req = make_mocked_request(
            "GET", "/api/anthropic/keys",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db}, headers=AUTH,
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
        self.assertEqual(key["role"], "primary")
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
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db}, headers=AUTH,
        )
        resp = await dashboard_api._api_anthropic_keys(req)
        keys = json.loads(resp.body)["keys"]
        self.assertEqual([k["id"] for k in keys], ["key-1"])


class AnthropicOauthStartTests(unittest.IsolatedAsyncioTestCase):
    def _app(self) -> dict:
        return {"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "_oauth_login_sessions": {}}

    async def test_requires_action_auth(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.return_value = False
        req = make_mocked_request(
            "POST", "/api/anthropic/oauth/start",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "_oauth_login_sessions": {}},
            headers={"Authorization": "Bearer sp-team"},
        )
        req.json = AsyncMock(return_value={"name": "n"})
        resp = await dashboard_api._api_anthropic_oauth_start(req)
        self.assertEqual(resp.status, 403)

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
            "anthropic_pool": _pool(), "dashboard_secret": ADMIN,
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
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN},
            headers={"Authorization": "Bearer sp-team"},
        )
        req.json = AsyncMock(return_value={"state": "s", "code": "c"})
        resp = await dashboard_api._api_anthropic_oauth_submit(req)
        self.assertEqual(resp.status, 403)


def _mgmt_app(row: dict | None = None):
    """(app, db, pool) for the status/rename/delete/refresh endpoint tests."""
    pool = _pool()
    db = MagicMock()
    db.get_anthropic_key = AsyncMock(return_value=row)
    db.set_anthropic_key_status = AsyncMock()
    db.set_anthropic_key_name = AsyncMock(return_value=row is not None)
    return {"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db}, db, pool


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

    async def test_deleted_key_404(self) -> None:
        app, db, _ = _mgmt_app(_key_row(status="deleted"))
        req = make_mocked_request("POST", "/api/anthropic/keys/status", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "active": True})
        resp = await dashboard_api._api_anthropic_key_status(req)
        self.assertEqual(resp.status, 404)
        db.set_anthropic_key_status.assert_not_awaited()


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
        db.set_anthropic_key_name.assert_awaited_once()
        args = db.set_anthropic_key_name.await_args
        self.assertEqual(args.args, ("key-1", "fresh"))
        self.assertEqual(args.kwargs.get("audit_source"), "dashboard")
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
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        req.json = AsyncMock(return_value={"id": "key-1"})
        resp = await dashboard_api._api_anthropic_key_delete(req)
        self.assertEqual(resp.status, 403)
        db.set_anthropic_key_status.assert_not_awaited()


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

    async def test_standby_refresh_skips_activation(self) -> None:
        app, db, pool = self._refresh_app(_key_row(role="standby"))
        req = make_mocked_request("POST", "/api/anthropic/keys/refresh", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1"})
        import smart_proxy.anthropic_oauth as ao
        with patch.object(ao, "refresh_oauth_token",
                          AsyncMock(return_value=("new-at", 1795000000000, "new-rt"))), \
             patch.object(ao, "activate_oauth_access_token", AsyncMock(return_value=[])) as activate:
            resp = await dashboard_api._api_anthropic_key_refresh(req)
        self.assertEqual(resp.status, 200)
        args = db.update_anthropic_oauth_tokens.await_args
        self.assertEqual(args.args, ("key-1", "new-at", 1795000000000, "new-rt"))
        pool.reload.assert_awaited_once()
        activate.assert_not_awaited()

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


class AnthropicKeyRoleTests(unittest.IsolatedAsyncioTestCase):
    def _role_app(self, row):
        app, db, pool = _mgmt_app(row)
        db.set_anthropic_key_role = AsyncMock(return_value=row is not None)
        db.get_active_anthropic_keys = AsyncMock(return_value=[row] if row else [])
        return app, db, pool

    async def test_set_standby_ok(self):
        app, db, pool = self._role_app(_key_row())
        db.get_active_anthropic_keys = AsyncMock(return_value=[_key_row(), _key_row(id="other")])
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "role": "standby"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(db.set_anthropic_key_role.await_args.args, ("key-1", "standby"))
        pool.reload.assert_awaited_once()

    async def test_invalid_role_400(self):
        app, db, _ = self._role_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "role": "bogus"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()

    async def test_refuse_demote_last_primary(self):
        app, db, _ = self._role_app(_key_row())
        db.get_active_anthropic_keys = AsyncMock(return_value=[_key_row(role="primary")])
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "role": "standby"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()

    async def test_requires_action_auth(self):
        pool = _pool()   # a valid sp- key: authenticated, but not the admin secret
        req = make_mocked_request("POST", "/api/anthropic/keys/role",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": MagicMock()}, headers={"Authorization": "Bearer sp-team"})
        req.json = AsyncMock(return_value={"id": "key-1", "role": "standby"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 403)

    async def test_unknown_id_404(self):
        app, db, _ = self._role_app(None)
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "nope", "role": "standby"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 404)
        db.set_anthropic_key_role.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
