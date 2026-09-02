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

from smart_proxy.anthropic_proxy import _oauth_usage_history_handler


def _log_row(**overrides: object) -> dict:
    base = {
        "id": 1,
        "key_id": "oauth-key-1",
        "window_kind": "seven_day",
        "resets_at": "2026-07-02T11:00:00+00:00",
        "resets_at_raw": "2026-07-02T11:00:00.028998+00:00",
        "first_seen_at": "2026-06-28T09:00:00+00:00",
        "first_active_at": "2026-06-28T09:30:00+00:00",
        "last_seen_at": "2026-07-02T10:58:00+00:00",
        "observations": 812,
        "last_utilization": 34.0,
        "max_utilization": 71.0,
        "max_utilization_at": "2026-07-01T18:00:00+00:00",
    }
    base.update(overrides)
    return base


def _drop_row(**overrides: object) -> dict:
    base = {
        "id": 1,
        "key_id": "oauth-key-1",
        "window_kind": "seven_day",
        "resets_at": "2026-07-02T11:00:00+00:00",
        "dropped_at": "2026-07-01T22:00:00+00:00",
        "prev_seen_at": "2026-07-01T21:58:00+00:00",
        "from_utilization": 85.0,
        "to_utilization": 0.0,
    }
    base.update(overrides)
    return base


def _app(
    rows: list[dict],
    drops: list[dict] | None = None,
    wipes: list[dict] | None = None,
    usage: list[dict] | None = None,
    pending: list[dict] | None = None,
    prices: list[dict] | None = None,
    **extra: object,
) -> dict:
    mock_db = MagicMock()
    mock_db.list_anthropic_keys = AsyncMock(
        return_value=[
            {"id": "oauth-key-1", "key_type": "oauth", "status": "active",
             "name": "pro-sp-auth"},
            {"id": "api-key-1", "key_type": "api_key", "status": "active",
             "name": "big-key"},
        ]
    )
    mock_db.list_oauth_window_log = AsyncMock(return_value=rows)
    mock_db.list_oauth_window_drops = AsyncMock(return_value=drops or [])
    mock_db.list_oauth_limit_wipes = AsyncMock(return_value=wipes or [])
    mock_db.list_oauth_window_usage = AsyncMock(return_value=usage or [])
    mock_db.list_oauth_window_usage_pending = AsyncMock(
        return_value=pending or [])
    mock_db.get_all_model_prices = AsyncMock(return_value=prices or [])
    mock_pool = MagicMock()
    mock_pool.check_auth.return_value = False
    app = {"anthropic_pool": mock_pool, "db": mock_db}
    app.update(extra)
    return app


class OauthUsageHistoryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_groups_by_kind_and_computes_spans_newest_first(self) -> None:
        # list_oauth_window_log returns window_kind ASC, resets_at ASC
        rows = [
            _log_row(id=1, window_kind="five_hour",
                     resets_at="2026-07-01T21:00:00+00:00"),
            _log_row(id=2, resets_at="2026-06-28T05:00:00+00:00"),
            _log_row(id=3, resets_at="2026-07-02T11:00:00+00:00"),
        ]
        app = _app(rows)
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app)
        )
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertIn("generated_at", body)
        self.assertEqual(len(body["keys"]), 1)  # api_key row excluded
        key = body["keys"][0]
        self.assertEqual(key["id"], "oauth-key-1")
        self.assertEqual(key["name"], "pro-sp-auth")
        self.assertEqual(set(key["windows"]), {"five_hour", "seven_day"})
        seven = key["windows"]["seven_day"]
        self.assertEqual(len(seven), 2)
        # newest first
        self.assertEqual(seven[0]["resets_at"], "2026-07-02T11:00:00+00:00")
        # 06-28T05:00 -> 07-02T11:00 = 4d6h = 4.25 days
        self.assertEqual(seven[0]["span_days_since_prev"], 4.25)
        self.assertIsNone(seven[1]["span_days_since_prev"])  # oldest has no prev
        self.assertEqual(seven[0]["observations"], 812)
        self.assertEqual(seven[0]["max_utilization"], 71.0)
        self.assertEqual(
            seven[0]["first_active_at"], "2026-06-28T09:30:00+00:00"
        )
        self.assertIsNone(
            key["windows"]["five_hour"][0]["span_days_since_prev"]
        )

    async def test_kind_filter_and_limit(self) -> None:
        rows = [
            _log_row(id=1, window_kind="five_hour",
                     resets_at="2026-07-01T21:00:00+00:00"),
            _log_row(id=2, resets_at="2026-06-20T05:00:00+00:00"),
            _log_row(id=3, resets_at="2026-06-28T05:00:00+00:00"),
            _log_row(id=4, resets_at="2026-07-02T11:00:00+00:00"),
        ]
        app = _app(rows)
        resp = await _oauth_usage_history_handler(
            make_mocked_request(
                "GET", "/_oauth_usage_history?kind=seven_day&limit=2", app=app
            )
        )
        body = json.loads(resp.body)
        windows = body["keys"][0]["windows"]
        self.assertEqual(set(windows), {"seven_day"})
        seven = windows["seven_day"]
        self.assertEqual(len(seven), 2)  # limit applied after newest-first sort
        self.assertEqual(seven[0]["resets_at"], "2026-07-02T11:00:00+00:00")
        self.assertEqual(seven[1]["resets_at"], "2026-06-28T05:00:00+00:00")
        # span still computed from the full sequence (prev = 06-20 row)
        self.assertEqual(seven[1]["span_days_since_prev"], 8.0)

    async def test_drops_exposed_with_hours_before_claimed_reset(self) -> None:
        # 22:00 -> claimed reset 07-02T11:00 = 13h early
        app = _app([_log_row()], drops=[_drop_row()])
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app)
        )
        body = json.loads(resp.body)
        key = body["keys"][0]
        self.assertIn("drops", key)
        drops = key["drops"]["seven_day"]
        self.assertEqual(len(drops), 1)
        drop = drops[0]
        self.assertEqual(drop["from_utilization"], 85.0)
        self.assertEqual(drop["to_utilization"], 0.0)
        self.assertEqual(drop["dropped_at"], "2026-07-01T22:00:00+00:00")
        self.assertEqual(drop["prev_seen_at"], "2026-07-01T21:58:00+00:00")
        self.assertEqual(drop["resets_at_claimed"], "2026-07-02T11:00:00+00:00")
        self.assertEqual(drop["hours_before_claimed_reset"], 13.0)

    async def test_drops_respect_kind_filter_and_limit_newest_first(self) -> None:
        drops = [
            _drop_row(id=1, window_kind="five_hour",
                      dropped_at="2026-07-01T10:00:00+00:00"),
            _drop_row(id=2, dropped_at="2026-06-25T22:00:00+00:00"),
            _drop_row(id=3, dropped_at="2026-07-01T22:00:00+00:00"),
        ]
        app = _app([], drops=drops)
        resp = await _oauth_usage_history_handler(
            make_mocked_request(
                "GET", "/_oauth_usage_history?kind=seven_day&limit=1", app=app
            )
        )
        body = json.loads(resp.body)
        key = body["keys"][0]
        self.assertEqual(set(key["drops"]), {"seven_day"})
        seven = key["drops"]["seven_day"]
        self.assertEqual(len(seven), 1)
        self.assertEqual(seven[0]["dropped_at"], "2026-07-01T22:00:00+00:00")

    async def test_requires_auth_when_flag_enabled(self) -> None:
        app = _app([], oauth_usage_require_auth=True)
        resp = await _oauth_usage_history_handler(
            make_mocked_request(
                "GET", "/_oauth_usage_history", app=app,
                headers={"Authorization": "Bearer sp-bad"},
            )
        )
        self.assertEqual(resp.status, 401)

    async def test_route_is_registered(self) -> None:
        import inspect  # noqa: PLC0415

        import smart_proxy.anthropic_proxy as mod  # noqa: PLC0415

        source = inspect.getsource(mod)
        self.assertIn('"/_oauth_usage_history"', source)


def _usage_row(**overrides: object) -> dict:
    base = {
        "window_id": 1, "model": "claude-sonnet-5",
        "input_tokens": 1_000_000, "output_tokens": 200_000,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "cache_creation_5m_tokens": 0, "cache_creation_1h_tokens": 0,
        "web_search_requests": 0, "requests": 42,
    }
    base.update(overrides)
    return base


_SONNET_PRICE = {
    "model_prefix": "claude-sonnet-5", "provider": "anthropic",
    "input_price": 3.0, "output_price": 15.0, "cache_read_price": 0.3,
    "cache_write_5m_price": 3.75, "cache_write_1h_price": 6.0,
    "updated_at": "2026-07-12T00:00:00+00:00",
}


class OauthUsageHistoryUsageBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_block_with_costs(self) -> None:
        app = _app([_log_row(id=1)], usage=[_usage_row()],
                   prices=[_SONNET_PRICE])
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        win = body["keys"][0]["windows"]["seven_day"][0]
        model = win["usage"]["models"]["claude-sonnet-5"]
        self.assertEqual(model["input_tokens"], 1_000_000)
        self.assertEqual(model["cost_usd"], 6.0)  # 1M*3 + 0.2M*15 per MTok
        totals = win["usage"]["totals"]
        self.assertEqual(totals["output_tokens"], 200_000)
        self.assertEqual(totals["requests"], 42)
        self.assertEqual(totals["cost_usd"], 6.0)
        self.assertFalse(totals["cost_partial"])
        self.assertEqual(body["keys"][0]["pending"], {})

    async def test_unpriced_model_sets_cost_partial(self) -> None:
        app = _app([_log_row(id=1)], usage=[_usage_row()], prices=[])
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        win = body["keys"][0]["windows"]["seven_day"][0]
        self.assertIsNone(win["usage"]["models"]["claude-sonnet-5"]["cost_usd"])
        self.assertEqual(win["usage"]["totals"]["cost_usd"], 0.0)
        self.assertTrue(win["usage"]["totals"]["cost_partial"])

    async def test_window_without_usage_has_null_block(self) -> None:
        app = _app([_log_row(id=1)])
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        self.assertIsNone(body["keys"][0]["windows"]["seven_day"][0]["usage"])

    async def test_pending_block_grouped_by_kind(self) -> None:
        pending = [{
            "key_id": "oauth-key-1", "window_kind": "seven_day",
            "model": "claude-sonnet-5",
            "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0, "web_search_requests": 0,
            "requests": 1, "updated_at": "2026-07-12T10:00:00+00:00",
        }]
        app = _app([_log_row(id=1)], pending=pending)
        resp = await _oauth_usage_history_handler(
            make_mocked_request("GET", "/_oauth_usage_history", app=app))
        body = json.loads(resp.body)
        block = body["keys"][0]["pending"]["seven_day"]
        self.assertEqual(block["models"]["claude-sonnet-5"]["input_tokens"], 7)
        self.assertEqual(block["updated_at"], "2026-07-12T10:00:00+00:00")

    async def test_pending_respects_kind_filter(self) -> None:
        pending = [{
            "key_id": "oauth-key-1", "window_kind": "five_hour",
            "model": "claude-sonnet-5",
            "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
            "cache_creation_1h_tokens": 0, "web_search_requests": 0,
            "requests": 1, "updated_at": "2026-07-12T10:00:00+00:00",
        }]
        app = _app([_log_row(id=1)], pending=pending)
        resp = await _oauth_usage_history_handler(make_mocked_request(
            "GET", "/_oauth_usage_history?kind=seven_day", app=app))
        body = json.loads(resp.body)
        self.assertEqual(body["keys"][0]["pending"], {})


if __name__ == "__main__":
    unittest.main()
