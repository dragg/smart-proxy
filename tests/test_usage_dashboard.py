from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from smart_proxy.anthropic_proxy import _extract_client_token, _usage_dashboard_authorize
from smart_proxy.usage_dashboard import (
    make_usage_dashboard_handler,
    register_usage_dashboard,
)


def _anthropic_handler(pool: object, db: object):
    """Build the /_usage handler wired exactly as anthropic_proxy wires it."""
    return make_usage_dashboard_handler(
        authorize=lambda req: req.app["anthropic_pool"].check_auth(
            _extract_client_token(req)
        ),
        get_db=lambda req: req.app.get("db"),
    )


class UsageDashboardHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_auth(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = False
        handler = _anthropic_handler(pool, MagicMock())
        request = make_mocked_request(
            "GET",
            "/_usage",
            app={"anthropic_pool": pool, "db": MagicMock()},
            headers={"Authorization": "Bearer sp-bad"},
        )

        response = await handler(request)

        self.assertEqual(response.status, 401)
        self.assertEqual(response.content_type, "application/json")
        pool.check_auth.assert_called_once_with("sp-bad")

    async def test_renders_key_totals_and_model_breakdown(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True
        db = MagicMock()
        # A historical OpenAI row (gpt-4o-mini) plus an Anthropic row: proves the
        # dashboard still renders decommissioned-provider history with cost.
        db.query_usage_by_key_model = AsyncMock(
            return_value=[
                {
                    "proxy_key": "sp-team",
                    "group_name": None,
                    "key_name": "Team Key",
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "input_tokens": 1_000_000,
                    "output_tokens": 500_000,
                    "cache_read_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_creation_5m_tokens": 0,
                    "cache_creation_1h_tokens": 0,
                    "web_search_requests": 0,
                    "requests": 7,
                },
                {
                    "proxy_key": "claude-passthrough",
                    "group_name": "Claude OAuth",
                    "key_name": "",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "cache_read_tokens": 1_000_000,
                    "cache_creation_tokens": 2_000_000,
                    "cache_creation_5m_tokens": 1_000_000,
                    "cache_creation_1h_tokens": 1_000_000,
                    "web_search_requests": 2,
                    "requests": 3,
                },
            ]
        )
        db.get_all_model_prices = AsyncMock(
            return_value=[
                {
                    "model_prefix": "gpt-4o-mini",
                    "provider": "openai",
                    "input_price": 0.15,
                    "output_price": 0.60,
                    "cache_read_price": None,
                    "cache_write_5m_price": None,
                    "cache_write_1h_price": None,
                    "updated_at": "2026-04-01T00:00:00Z",
                },
                {
                    "model_prefix": "claude-sonnet-4-6",
                    "provider": "anthropic",
                    "input_price": 3.00,
                    "output_price": 15.00,
                    "cache_read_price": 0.30,
                    "cache_write_5m_price": 3.75,
                    "cache_write_1h_price": 6.00,
                    "updated_at": "2026-04-01T00:00:00Z",
                },
            ]
        )
        handler = _anthropic_handler(pool, db)
        request = make_mocked_request(
            "GET",
            "/_usage?start=2026-04-01&end=2026-04-02",
            app={"anthropic_pool": pool, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )

        response = await handler(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        body = response.text
        assert body is not None
        self.assertIn("Usage Cost", body)
        self.assertIn("Team Key", body)
        self.assertIn("Claude OAuth", body)
        self.assertIn("gpt-4o-mini", body)
        self.assertIn("claude-sonnet-4-6", body)
        self.assertIn("$0.45", body)
        self.assertIn("$28.07", body)
        self.assertIn("How cost is calculated", body)
        db.query_usage_by_key_model.assert_awaited_once_with("2026-04-01", "2026-04-02")

    async def test_preserves_query_key_in_period_form(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True
        db = MagicMock()
        db.query_usage_by_key_model = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        handler = _anthropic_handler(pool, db)
        request = make_mocked_request(
            "GET",
            "/_usage?key=sp-team&start=2026-04-01&end=2026-04-02",
            app={"anthropic_pool": pool, "db": db},
            headers={"Authorization": "Bearer sp-team"},
        )

        response = await handler(request)

        body = response.text
        assert body is not None
        self.assertIn('type="hidden" name="key" value="sp-team"', body)

    async def test_db_unavailable_returns_500(self) -> None:
        pool = MagicMock()
        pool.check_auth.return_value = True
        handler = make_usage_dashboard_handler(
            authorize=lambda req: True,
            get_db=lambda req: None,
        )
        request = make_mocked_request(
            "GET",
            "/_usage",
            app={"anthropic_pool": pool, "db": None},
            headers={"Authorization": "Bearer sp-team"},
        )

        response = await handler(request)

        self.assertEqual(response.status, 500)
        self.assertEqual(response.content_type, "application/json")

    async def test_bad_date_range_returns_400(self) -> None:
        handler = make_usage_dashboard_handler(
            authorize=lambda req: True,
            get_db=lambda req: MagicMock(),
        )
        request = make_mocked_request(
            "GET",
            "/_usage?start=2026-04-02&end=2026-04-01",
            app={},
        )

        response = await handler(request)

        self.assertEqual(response.status, 400)


class UsageDashboardRegistrationTests(unittest.TestCase):
    def test_register_adds_usage_get_route(self) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="It is recommended to use web.AppKey instances for keys.",
            )
            app = web.Application()
            register_usage_dashboard(
                app,
                authorize=lambda req: True,
                get_db=lambda req: None,
            )

        canonicals = {r.canonical for r in app.router.resources()}
        self.assertIn("/_usage", canonicals)
        methods = {
            route.method
            for resource in app.router.resources()
            if resource.canonical == "/_usage"
            for route in resource
        }
        self.assertIn("GET", methods)


class UsageDashboardAnthropicAuthTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard is browser-facing: it is opened as /_usage?key=sp-xxx.

    Browsers cannot set an Authorization header, so query-param auth must work.
    Regression guard: the migration to the anthropic proxy reused a token
    extractor that only read Authorization/x-api-key, 401ing valid keys.
    """

    async def test_query_key_authorizes_browser_request(self) -> None:
        pool = MagicMock()
        pool.check_auth.side_effect = lambda t: t == "sp-team"
        db = MagicMock()
        db.query_usage_by_key_model = AsyncMock(return_value=[])
        db.get_all_model_prices = AsyncMock(return_value=[])
        handler = make_usage_dashboard_handler(
            authorize=_usage_dashboard_authorize,
            get_db=lambda req: req.app.get("db"),
        )
        request = make_mocked_request(
            "GET",
            "/_usage?key=sp-team",
            app={"anthropic_pool": pool, "db": db},
        )

        response = await handler(request)

        self.assertEqual(response.status, 200)
        pool.check_auth.assert_called_once_with("sp-team")

    def test_authorize_prefers_header_then_query(self) -> None:
        pool = MagicMock()
        pool.check_auth.side_effect = lambda t: t == "sp-team"

        header_req = make_mocked_request(
            "GET",
            "/_usage",
            app={"anthropic_pool": pool},
            headers={"Authorization": "Bearer sp-team"},
        )
        self.assertTrue(_usage_dashboard_authorize(header_req))

        query_req = make_mocked_request(
            "GET",
            "/_usage?key=sp-team",
            app={"anthropic_pool": pool},
        )
        self.assertTrue(_usage_dashboard_authorize(query_req))

    def test_authorize_rejects_when_no_token(self) -> None:
        pool = MagicMock()
        pool.check_auth.side_effect = lambda t: t == "sp-team"
        req = make_mocked_request("GET", "/_usage", app={"anthropic_pool": pool})
        self.assertFalse(_usage_dashboard_authorize(req))


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


if __name__ == "__main__":
    unittest.main()
