"""The dashboard's admin credential is separate from a caller's proxy key.

An ``sp-`` key is handed to whoever should be able to *send requests*. Until
now the same key also administered the pool: attach or delete OAuth accounts,
change roles, clear other keys' spend limits. This splits the two --
``ANTHROPIC_PROXY_DASHBOARD_SECRET`` gates every mutation, while reads stay on
the proxy key so a consumer can still see what it spent.

The failure responses are deliberately chatty about *configuration* (never
about the secret's value): an operator who has not set the variable has no
other way to discover that, short of reading the source.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request

from smart_proxy import dashboard_api

SP = "sp-team"
SECRET = "correct-horse-battery-staple"


def _pool():
    pool = MagicMock()
    pool.check_auth.side_effect = lambda t: t == SP
    pool.is_proxy_key.side_effect = lambda t: t == SP
    pool.reload = AsyncMock()
    pool.available = 0
    return pool


def _app(secret: str = "", pool=None):
    return {"anthropic_pool": pool or _pool(), "dashboard_secret": secret}


def _body(resp) -> dict:
    return json.loads(resp.body)


class MutationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_key_cannot_mutate_when_secret_configured(self) -> None:
        app = _app(SECRET)
        req = make_mocked_request(
            "POST", "/api/reload", app=app, headers={"Authorization": f"Bearer {SP}"}
        )
        resp = await dashboard_api._api_reload(req)
        self.assertEqual(resp.status, 403)
        self.assertTrue(_body(resp)["admin_secret_configured"])
        app["anthropic_pool"].reload.assert_not_awaited()

    async def test_secret_can_mutate(self) -> None:
        app = _app(SECRET)
        app["key_limiter"] = None
        app["db"] = MagicMock()
        req = make_mocked_request(
            "POST", "/api/reload", app=app, headers={"Authorization": f"Bearer {SECRET}"}
        )
        resp = await dashboard_api._api_reload(req)
        self.assertEqual(resp.status, 200)
        app["anthropic_pool"].reload.assert_awaited()

    async def test_unconfigured_secret_denies_and_names_the_variable(self) -> None:
        app = _app("")
        req = make_mocked_request(
            "POST", "/api/reload", app=app, headers={"Authorization": f"Bearer {SP}"}
        )
        resp = await dashboard_api._api_reload(req)
        self.assertEqual(resp.status, 403)
        body = _body(resp)
        self.assertFalse(body["admin_secret_configured"])
        self.assertIn("ANTHROPIC_PROXY_DASHBOARD_SECRET", body["hint"])
        app["anthropic_pool"].reload.assert_not_awaited()

    async def test_secret_is_never_taken_from_the_query_string(self) -> None:
        # A URL lands in browser history, Referer headers and the access log.
        req = make_mocked_request(
            "POST", f"/api/reload?key={SECRET}", app=_app(SECRET)
        )
        self.assertEqual((await dashboard_api._api_reload(req)).status, 403)


class ReadGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_key_still_reads_when_secret_configured(self) -> None:
        db = MagicMock()
        db.list_proxy_keys = AsyncMock(return_value=[])
        app = _app(SECRET)
        app["db"] = db
        req = make_mocked_request(
            "GET", "/api/keys", app=app, headers={"Authorization": f"Bearer {SP}"}
        )
        self.assertEqual((await dashboard_api._api_keys(req)).status, 200)

    async def test_secret_also_reads(self) -> None:
        db = MagicMock()
        db.list_proxy_keys = AsyncMock(return_value=[])
        app = _app(SECRET)
        app["db"] = db
        req = make_mocked_request(
            "GET", "/api/keys", app=app, headers={"Authorization": f"Bearer {SECRET}"}
        )
        self.assertEqual((await dashboard_api._api_keys(req)).status, 200)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def _login(self, token: str, secret: str):
        req = make_mocked_request("POST", "/api/session", app=_app(secret))
        req.json = AsyncMock(return_value={"token": token})
        return await dashboard_api._api_session(req)

    async def test_secret_logs_in(self) -> None:
        resp = await self._login(SECRET, SECRET)
        self.assertEqual(resp.status, 200)
        self.assertTrue(_body(resp)["admin"])

    async def test_proxy_key_logs_in_without_admin(self) -> None:
        resp = await self._login(SP, SECRET)
        self.assertEqual(resp.status, 200)
        self.assertFalse(_body(resp)["admin"])

    async def test_bad_token_reports_that_no_secret_is_configured(self) -> None:
        resp = await self._login("whatever", "")
        self.assertEqual(resp.status, 401)
        self.assertFalse(_body(resp)["admin_secret_configured"])

    async def test_bad_token_reports_a_configured_secret(self) -> None:
        resp = await self._login("whatever", SECRET)
        self.assertEqual(resp.status, 401)
        self.assertTrue(_body(resp)["admin_secret_configured"])

    async def test_non_ascii_token_does_not_raise(self) -> None:
        # secrets.compare_digest() rejects non-ASCII str; comparing bytes is
        # what keeps a pasted "é" from becoming a 500 and a Telegram alert.
        resp = await self._login("é", SECRET)
        self.assertEqual(resp.status, 401)


if __name__ == "__main__":
    unittest.main()
