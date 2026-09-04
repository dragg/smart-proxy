# tests/test_dashboard_api.py
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

from aiohttp.test_utils import make_mocked_request

from smart_proxy import dashboard_api

ADMIN = "test-dashboard-admin-secret"


def _pool(valid: str = "sp-team"):
    pool = MagicMock()
    pool.check_auth.side_effect = lambda t: t == valid
    pool.is_proxy_key.side_effect = lambda t: t == valid
    pool.cooldown_snapshot.return_value = []
    return pool


class ActionAuthTests(unittest.IsolatedAsyncioTestCase):
    """A caller's proxy key reads the dashboard but may not administer it."""

    async def test_proxy_key_reads_but_cannot_mutate(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        db = MagicMock()
        db.list_proxy_keys = AsyncMock(return_value=[])
        headers = {"Authorization": "Bearer sp-team"}
        app = {"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db}

        read_req = make_mocked_request("GET", "/api/keys", app=app, headers=headers)
        self.assertEqual((await dashboard_api._api_keys(read_req)).status, 200)

        reload_req = make_mocked_request("POST", "/api/reload", app=app, headers=headers)
        resp = await dashboard_api._api_reload(reload_req)
        self.assertEqual(resp.status, 403)
        pool.reload.assert_not_awaited()

        db.set_proxy_key_active_by_created_at = AsyncMock(return_value="sp-x")
        toggle_req = make_mocked_request("POST", "/api/keys/active", app=app, headers=headers)
        toggle_req.json = AsyncMock(return_value={"created_at": "x", "active": False})
        resp = await dashboard_api._api_key_active(toggle_req)
        self.assertEqual(resp.status, 403)
        db.set_proxy_key_active_by_created_at.assert_not_awaited()


class ApiKeyCreateTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_requires_action_auth(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True     # permissive gate would pass
        pool.is_proxy_key.return_value = False  # but not a real sp-*
        pool.reload = AsyncMock()
        db = MagicMock()
        db.add_proxy_key = AsyncMock()
        req = make_mocked_request(
            "POST", "/api/keys",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sk-ant-x"},
        )
        req.json = AsyncMock(return_value={"name": "x"})
        resp = await dashboard_api._api_key_create(req)
        self.assertEqual(resp.status, 403)
        db.add_proxy_key.assert_not_awaited()

    async def test_create_mints_active_key_and_reloads(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        db = MagicMock()
        db.add_proxy_key = AsyncMock()
        req = make_mocked_request(
            "POST", "/api/keys",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        req.json = AsyncMock(return_value={"name": "ci"})
        resp = await dashboard_api._api_key_create(req)
        import json
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertTrue(body["key"].startswith("sp-"))
        self.assertEqual(len(body["key"]), 3 + 32)  # "sp-" + token_hex(16)
        self.assertEqual(body["name"], "ci")
        db.add_proxy_key.assert_awaited_once_with(body["key"], "ci")
        pool.reload.assert_awaited_once()  # immediately live

    async def test_create_empty_name_400(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        db = MagicMock()
        db.add_proxy_key = AsyncMock()
        req = make_mocked_request(
            "POST", "/api/keys",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        req.json = AsyncMock(return_value={"name": "  "})
        resp = await dashboard_api._api_key_create(req)
        self.assertEqual(resp.status, 400)
        db.add_proxy_key.assert_not_awaited()


class DashboardTokenTests(unittest.TestCase):
    def test_prefers_header_then_cookie_then_query(self) -> None:
        pool = _pool()
        hdr = make_mocked_request("GET", "/api/keys", app={"anthropic_pool": pool, "dashboard_secret": ADMIN},
                                  headers={"Authorization": "Bearer sp-team"})
        self.assertTrue(dashboard_api._dashboard_authorized(hdr))

        cookie = make_mocked_request("GET", "/api/keys", app={"anthropic_pool": pool, "dashboard_secret": ADMIN},
                                     headers={"Cookie": "dash_token=sp-team"})
        self.assertTrue(dashboard_api._dashboard_authorized(cookie))

        query = make_mocked_request("GET", "/api/keys?key=sp-team", app={"anthropic_pool": pool, "dashboard_secret": ADMIN})
        self.assertTrue(dashboard_api._dashboard_authorized(query))

        anon = make_mocked_request("GET", "/api/keys", app={"anthropic_pool": pool, "dashboard_secret": ADMIN})
        self.assertFalse(dashboard_api._dashboard_authorized(anon))


class ApiSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_token_sets_cookie(self) -> None:
        req = make_mocked_request("POST", "/api/session", app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN})
        req.json = AsyncMock(return_value={"token": "sp-team"})
        resp = await dashboard_api._api_session(req)
        self.assertEqual(resp.status, 200)
        set_cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("dash_token=sp-team", set_cookie)
        self.assertIn("httponly", set_cookie.lower())
        self.assertIn("samesite=lax", set_cookie.lower())
        self.assertIn("max-age", set_cookie.lower())

    async def test_invalid_token_401(self) -> None:
        req = make_mocked_request("POST", "/api/session", app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN})
        req.json = AsyncMock(return_value={"token": "nope"})
        resp = await dashboard_api._api_session(req)
        self.assertEqual(resp.status, 401)


class ApiUsageKeysTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_unauthorized(self) -> None:
        req = make_mocked_request("GET", "/api/usage", app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": MagicMock()})
        resp = await dashboard_api._api_usage(req)
        self.assertEqual(resp.status, 401)

    async def test_usage_returns_groups(self) -> None:
        db = MagicMock()
        db.query_usage_by_key_model = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/usage?start=2026-04-01&end=2026-04-02",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_usage(req)
        self.assertEqual(resp.status, 200)
        import json
        payload = json.loads(resp.body)
        self.assertEqual(payload["groups"], [])
        self.assertEqual(payload["start"], "2026-04-01")

    async def test_usage_day_grammar_keeps_the_old_db_calls(self) -> None:
        """The day path must not grow a call: this db is a bare MagicMock, so
        any unstubbed `await db.x()` raises TypeError rather than returning."""
        db = MagicMock()
        db.query_usage_by_key_model = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/usage?start=2026-04-01&end=2026-04-02",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_usage(req)
        import json
        payload = json.loads(resp.body)
        self.assertEqual(payload["granularity"], "day")
        self.assertNotIn("series", payload)
        db.query_usage_by_key_model.assert_awaited_once_with("2026-04-01", "2026-04-02")

    async def test_usage_hour_grammar_uses_the_bucket_queries(self) -> None:
        db = MagicMock()
        db.query_usage_bucket_by_key_model = AsyncMock(return_value=[])
        db.query_usage_bucket_series = AsyncMock(return_value=[])
        db.min_usage_bucket_hour = AsyncMock(return_value="2026-09-04T18")
        db.query_usage_by_key_model = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/usage?start=2026-09-05T10&end=2026-09-05T14",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_usage(req)
        self.assertEqual(resp.status, 200)
        import json
        payload = json.loads(resp.body)

        db.query_usage_bucket_by_key_model.assert_awaited_once_with(
            "2026-09-05T10", "2026-09-05T14")
        db.query_usage_bucket_series.assert_awaited_once_with(
            "2026-09-05T10", "2026-09-05T14")
        db.min_usage_bucket_hour.assert_awaited_once()
        db.query_usage_by_key_model.assert_not_awaited()

        self.assertEqual(payload["granularity"], "hour")
        self.assertEqual(payload["covered_from"], "2026-09-04T18")
        self.assertEqual(len(payload["series"]), 5, "T10..T14 inclusive")

    async def test_usage_mixed_grammar_is_a_400(self) -> None:
        req = make_mocked_request(
            "GET", "/api/usage?start=2026-09-05&end=2026-09-05T14",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": MagicMock()},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_usage(req)
        self.assertEqual(resp.status, 400)

    async def test_kinds_hour_grammar_uses_the_bucket_query(self) -> None:
        db = MagicMock()
        db.query_usage_bucket_by_kind = AsyncMock(return_value=[])
        db.query_usage_by_kind = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/usage/kinds?start=2026-09-05T10&end=2026-09-05T14",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_usage_kinds(req)
        self.assertEqual(resp.status, 200)
        db.query_usage_bucket_by_kind.assert_awaited_once_with(
            "2026-09-05T10", "2026-09-05T14")
        db.query_usage_by_kind.assert_not_awaited()

    async def test_kinds_garbage_range_is_a_400(self) -> None:
        """It used to reach SQL as a bind parameter and quietly match nothing."""
        req = make_mocked_request(
            "GET", "/api/usage/kinds?start=nonsense&end=also-nonsense",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": MagicMock()},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_usage_kinds(req)
        self.assertEqual(resp.status, 400)

    async def test_keys_redacts_to_prefix(self) -> None:
        db = MagicMock()
        db.list_proxy_keys = AsyncMock(return_value=[
            {"key": "sp-team-secret-123", "name": "Team", "active": 1, "created_at": "2026-04-01"},
        ])
        req = make_mocked_request("GET", "/api/keys",
                                  app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
                                  headers={"Authorization": "Bearer sp-team"})
        resp = await dashboard_api._api_keys(req)
        import json
        payload = json.loads(resp.body)
        self.assertEqual(payload["keys"][0]["key_prefix"], "sp-team-secr")
        self.assertTrue(payload["keys"][0]["active"])
        self.assertNotIn("key", payload["keys"][0])


class ApiActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_requires_auth(self) -> None:
        req = make_mocked_request("POST", "/api/reload", app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN})
        resp = await dashboard_api._api_reload(req)
        self.assertEqual(resp.status, 401)

    async def test_reload_calls_pool(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        pool.available = 4
        req = make_mocked_request("POST", "/api/reload", app={"anthropic_pool": pool, "dashboard_secret": ADMIN},
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        resp = await dashboard_api._api_reload(req)
        import json
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["active"], 4)
        pool.reload.assert_awaited_once()

    async def test_key_toggle_unknown_created_at_404(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        db = MagicMock()
        db.set_proxy_key_active_by_created_at = AsyncMock(return_value=None)
        req = make_mocked_request(
            "POST", "/api/keys/active",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        req.json = AsyncMock(
            return_value={"created_at": "2020-01-01T00:00:00+00:00", "active": False})
        resp = await dashboard_api._api_key_active(req)
        self.assertEqual(resp.status, 404)

    async def test_key_toggle_missing_created_at_400(self) -> None:
        pool = _pool()
        db = MagicMock()
        db.set_proxy_key_active_by_created_at = AsyncMock(return_value="x")
        req = make_mocked_request(
            "POST", "/api/keys/active",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        req.json = AsyncMock(return_value={"active": True})  # no created_at
        resp = await dashboard_api._api_key_active(req)
        self.assertEqual(resp.status, 400)
        db.set_proxy_key_active_by_created_at.assert_not_awaited()

    async def test_key_toggle_ok_reloads(self) -> None:
        pool = _pool()
        pool.reload = AsyncMock()
        db = MagicMock()
        db.set_proxy_key_active_by_created_at = AsyncMock(return_value="sp-team-secret-123")
        req = make_mocked_request(
            "POST", "/api/keys/active",
            app={"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        ts = "2020-01-02T00:00:00+00:00"
        req.json = AsyncMock(return_value={"created_at": ts, "active": True})
        resp = await dashboard_api._api_key_active(req)
        import json
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.body)["active"])
        db.set_proxy_key_active_by_created_at.assert_awaited_once_with(ts, True)
        pool.reload.assert_awaited_once()

    async def test_compat_stats_snapshot(self) -> None:
        stats = MagicMock()
        stats.snapshot.return_value = {"requests": 3}
        req = make_mocked_request(
            "GET", "/api/openai-compat/stats",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "openai_compat_stats": stats},
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        resp = await dashboard_api._api_compat_stats(req)
        import json
        self.assertEqual(json.loads(resp.body)["requests"], 3)


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


class ApiKindsSessionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_kinds_unauthorized(self) -> None:
        req = make_mocked_request(
            "GET", "/api/usage/kinds",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": MagicMock()},
        )
        self.assertEqual((await dashboard_api._api_usage_kinds(req)).status, 401)

    async def test_kinds_shape(self) -> None:
        db = MagicMock()
        db.query_usage_by_kind = AsyncMock(return_value=[
            {"proxy_key": "sp-a", "key_name": "Petya", "request_kind": "subagent",
             "provider": "anthropic", "model": "m", "input_tokens": 100, "output_tokens": 0,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 3}])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/usage/kinds?start=2026-07-01&end=2026-07-31",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        import json
        resp = await dashboard_api._api_usage_kinds(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["start"], "2026-07-01")
        self.assertEqual(body["end"], "2026-07-31")
        self.assertEqual(body["kinds"][0]["request_kind"], "subagent")
        self.assertEqual(body["kinds"][0]["key_name"], "Petya")
        self.assertEqual(body["kinds"][0]["proxy_key"], "sp-a")
        self.assertEqual(body["kinds"][0]["input_tokens"], 100)
        self.assertEqual(body["kinds"][0]["requests"], 3)

    async def test_kinds_aggregates_across_models_per_request_kind_and_key(self) -> None:
        db = MagicMock()
        db.query_usage_by_kind = AsyncMock(return_value=[
            {"proxy_key": "sp-a", "key_name": "Petya", "request_kind": "subagent",
             "provider": "anthropic", "model": "claude-a", "input_tokens": 100, "output_tokens": 10,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 3},
            {"proxy_key": "sp-a", "key_name": "Petya", "request_kind": "subagent",
             "provider": "anthropic", "model": "claude-b", "input_tokens": 50, "output_tokens": 5,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 2},
            {"proxy_key": "sp-a", "key_name": "Petya", "request_kind": "main",
             "provider": "anthropic", "model": "claude-a", "input_tokens": 1000, "output_tokens": 100,
             "cache_read_tokens": 0, "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 9},
        ])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/usage/kinds",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        import json
        body = json.loads((await dashboard_api._api_usage_kinds(req)).body)
        self.assertEqual(len(body["kinds"]), 2)
        subagent = next(k for k in body["kinds"] if k["request_kind"] == "subagent")
        self.assertEqual(subagent["input_tokens"], 150)
        self.assertEqual(subagent["output_tokens"], 15)
        self.assertEqual(subagent["requests"], 5)

    async def test_sessions_unauthorized(self) -> None:
        req = make_mocked_request(
            "GET", "/api/sessions",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": MagicMock()},
        )
        self.assertEqual((await dashboard_api._api_sessions(req)).status, 401)

    async def test_sessions_sums_per_model_rows_into_one_session(self) -> None:
        db = MagicMock()
        db.query_top_sessions = AsyncMock(return_value=[
            {"session_id": "sess-1", "proxy_key": "sp-a", "key_name": "Petya",
             "provider": "anthropic", "model": "claude-a",
             "first_date": "2026-07-01", "last_date": "2026-07-02",
             "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 4},
            {"session_id": "sess-1", "proxy_key": "sp-a", "key_name": "Petya",
             "provider": "anthropic", "model": "claude-b",
             "first_date": "2026-06-30", "last_date": "2026-07-05",
             "input_tokens": 50, "output_tokens": 5, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 1},
        ])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/sessions?limit=10",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        import json
        resp = await dashboard_api._api_sessions(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(len(body["sessions"]), 1)
        session = body["sessions"][0]
        self.assertEqual(session["session_id"], "sess-1")
        self.assertEqual(session["key_name"], "Petya")
        self.assertEqual(session["input_tokens"], 150)
        self.assertEqual(session["output_tokens"], 15)
        self.assertEqual(session["requests"], 5)
        # merged across the two per-model rows: earliest first_date, latest last_date
        self.assertEqual(session["first_date"], "2026-06-30")
        self.assertEqual(session["last_date"], "2026-07-05")
        # project/title label and per-kind breakdown (mock rows omit
        # request_kind, so the lenient reader buckets both under "unknown")
        self.assertIn("project", session)
        self.assertIn("title", session)
        self.assertIsInstance(session["kinds"], list)
        self.assertEqual(len(session["kinds"]), 1)
        self.assertEqual(session["kinds"][0]["request_kind"], "unknown")
        self.assertEqual(session["kinds"][0]["requests"], 5)

    async def test_sessions_ordered_by_cost_desc_not_row_order_or_tokens(self) -> None:
        """build_sessions_json must re-sort by total cost desc after
        aggregating per-model rows into one entry per session -- neither
        the raw DB row order nor per-session token totals are a reliable
        proxy for cost, since different models are priced differently.

        ``sess-cheap`` has a much larger token total (10,000) than
        ``sess-multi`` (2,000 across two models) but is priced far more
        cheaply, so its total cost ($0.001) is much lower than
        sess-multi's ($0.20). The mocked rows list sess-cheap first, so
        this only passes if the aggregation step actively re-sorts by
        cost rather than preserving input/row order or token order.
        """
        db = MagicMock()
        db.query_top_sessions = AsyncMock(return_value=[
            {"session_id": "sess-cheap", "proxy_key": "sp-a", "key_name": "Cheap",
             "provider": "anthropic", "model": "model-cheap",
             "first_date": "2026-07-01", "last_date": "2026-07-01",
             "input_tokens": 10_000, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 1},
            {"session_id": "sess-multi", "proxy_key": "sp-b", "key_name": "Multi",
             "provider": "anthropic", "model": "model-expensive-a",
             "first_date": "2026-07-01", "last_date": "2026-07-01",
             "input_tokens": 1_000, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 1},
            {"session_id": "sess-multi", "proxy_key": "sp-b", "key_name": "Multi",
             "provider": "anthropic", "model": "model-expensive-b",
             "first_date": "2026-07-02", "last_date": "2026-07-02",
             "input_tokens": 1_000, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 1},
        ])
        db.get_all_model_prices = AsyncMock(return_value=[
            {"model_prefix": "model-cheap", "provider": "anthropic",
             "input_price": 0.10, "output_price": 0.50},
            {"model_prefix": "model-expensive-a", "provider": "anthropic",
             "input_price": 100.0, "output_price": 200.0},
            {"model_prefix": "model-expensive-b", "provider": "anthropic",
             "input_price": 100.0, "output_price": 200.0},
        ])
        req = make_mocked_request(
            "GET", "/api/sessions?limit=10",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        import json
        resp = await dashboard_api._api_sessions(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        sessions = body["sessions"]
        self.assertEqual(len(sessions), 2)
        self.assertEqual([s["session_id"] for s in sessions], ["sess-multi", "sess-cheap"])
        self.assertAlmostEqual(sessions[0]["cost"], 0.2, places=6)
        self.assertAlmostEqual(sessions[1]["cost"], 0.001, places=6)
        self.assertEqual(sessions[0]["input_tokens"], 2000)
        self.assertEqual(sessions[1]["input_tokens"], 10_000)

    async def test_sessions_default_and_invalid_limit(self) -> None:
        db = MagicMock()
        db.query_top_sessions = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        req = make_mocked_request(
            "GET", "/api/sessions?limit=notanumber",
            app={"anthropic_pool": _pool(), "dashboard_secret": ADMIN, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_sessions(req)
        self.assertEqual(resp.status, 200)
        db.query_top_sessions.assert_awaited_once_with(50)


class KeyLimitsApiTests(unittest.IsolatedAsyncioTestCase):
    def _app(self):
        pool = _pool("sp-team")
        db = MagicMock()
        db.list_proxy_keys = AsyncMock(return_value=[{
            "key": "sp-team-full", "name": "laptop", "active": 1,
            "created_at": "2026-07-30T10:00:00+00:00",
        }])
        db.get_proxy_key_by_created_at = AsyncMock(return_value="sp-team-full")
        limiter = MagicMock()
        limiter.limits_for.return_value = {"daily_usd": 25.0}
        limiter.snapshot.return_value = {"daily_usd": {
            "limit_usd": 25.0, "spent_usd": 18.42, "remaining_usd": 6.58,
            "percent": 73.7, "resets_at": "2026-07-31T00:00:00+02:00",
            "exceeded": False,
        }}
        limiter.set_limit = AsyncMock()
        return {"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db, "key_limiter": limiter}, db, limiter

    async def test_list_keys_includes_limits_and_usage(self):
        app, _db, _limiter = self._app()
        req = make_mocked_request("GET", "/api/keys", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        resp = await dashboard_api._api_keys(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["keys"][0]["limits"], {"daily_usd": 25.0})
        self.assertAlmostEqual(body["keys"][0]["usage"]["daily_usd"]["spent_usd"], 18.42)

    async def test_set_limit_persists_and_applies(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"daily_usd": 30.0},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 200)
        limiter.set_limit.assert_awaited_once_with("sp-team-full", "daily_usd", 30.0)

    async def test_null_amount_clears_the_limit(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"daily_usd": None},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 200)
        limiter.set_limit.assert_awaited_once_with("sp-team-full", "daily_usd", None)

    async def test_unknown_kind_is_rejected_before_any_write(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"weekly_bananas": 3},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 400)
        limiter.set_limit.assert_not_awaited()

    async def test_negative_amount_is_rejected_before_any_write(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"daily_usd": -1},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 400)
        limiter.set_limit.assert_not_awaited()

    async def test_unknown_created_at_is_404(self):
        app, db, limiter = self._app()
        db.get_proxy_key_by_created_at = AsyncMock(return_value=None)
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        req.json = AsyncMock(return_value={
            "created_at": "nope", "limits": {"daily_usd": 1.0}})
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 404)
        limiter.set_limit.assert_not_awaited()

    async def test_requires_action_auth(self):
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": "Bearer sp-team"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00", "limits": {"daily_usd": 1.0}})
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 403)
        limiter.set_limit.assert_not_awaited()

    async def test_compound_valid_then_invalid_kind_rejects_entire_payload(self):
        """Validate entire payload before writing anything. A request with one
        good kind and one bad kind must reject both and touch nothing."""
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"daily_usd": 30.0, "weekly_bananas": 3},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 400)
        limiter.set_limit.assert_not_awaited()

    async def test_compound_invalid_then_valid_kind_rejects_entire_payload(self):
        """Validate entire payload before writing anything. A request with one
        bad kind and one good kind (reverse order) must reject both and touch nothing."""
        app, _db, limiter = self._app()
        req = make_mocked_request("POST", "/api/keys/limits", app=app,
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        req.json = AsyncMock(return_value={
            "created_at": "2026-07-30T10:00:00+00:00",
            "limits": {"weekly_bananas": 3, "daily_usd": 30.0},
        })
        resp = await dashboard_api._api_key_limits(req)
        self.assertEqual(resp.status, 400)
        limiter.set_limit.assert_not_awaited()


class CooldownClearTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard's escape hatch for a rate-limited pool."""

    def _app(self, pool):
        db = MagicMock()
        db.list_anthropic_keys = AsyncMock(return_value=[])
        db.list_proxy_keys = AsyncMock(return_value=[])
        return {"anthropic_pool": pool, "dashboard_secret": ADMIN, "db": db}

    async def test_key_list_reports_parked_keys(self) -> None:
        pool = _pool()
        pool.cooldown_snapshot.return_value = [
            {"key_id": "key-a", "model": "claude-opus-5", "seconds_left": 2309}
        ]
        req = make_mocked_request(
            "GET", "/api/anthropic/keys", app=self._app(pool),
            headers={"Authorization": "Bearer sp-team"},
        )
        resp = await dashboard_api._api_anthropic_keys(req)
        self.assertEqual(resp.status, 200)
        # The button only shows when this is non-empty, so it has to ship here.
        self.assertEqual(json.loads(resp.body)["cooldowns"][0]["seconds_left"], 2309)

    async def test_clear_requires_admin_not_just_a_proxy_key(self) -> None:
        pool = _pool()
        req = make_mocked_request(
            "POST", "/api/anthropic/cooldowns/clear", app=self._app(pool),
            headers={"Authorization": "Bearer sp-team"},
        )
        self.assertEqual((await dashboard_api._api_anthropic_cooldowns_clear(req)).status, 403)
        pool.clear_cooldowns.assert_not_called()

    async def test_admin_clears_every_model_at_once(self) -> None:
        pool = _pool()
        pool.clear_cooldowns.return_value = 3
        req = make_mocked_request(
            "POST", "/api/anthropic/cooldowns/clear", app=self._app(pool),
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        resp = await dashboard_api._api_anthropic_cooldowns_clear(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["cooldowns_cleared"], 3)
        pool.clear_cooldowns.assert_called_once_with()
