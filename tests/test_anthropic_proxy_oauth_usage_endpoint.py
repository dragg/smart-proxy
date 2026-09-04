from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request

from smart_proxy import anthropic_proxy
from smart_proxy.claude_code_identity import ClaudeCodeVersion, DEFAULT_CLAUDE_CODE_VERSION
from smart_proxy.anthropic_proxy import AnthropicKeyPool, _oauth_usage_handler


def _oauth_row(**overrides: object) -> dict:
    base = {
        "id": "oauth-key-1",
        "key_type": "oauth",
        "status": "active",
        "api_key": None,
        "access_token": "sk-ant-oat01-access",
        "refresh_token": "sk-ant-ort01-refresh",
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "expires_at": 9_999_999_999_999,
        "scopes": None,
        "name": "test-oauth",
    }
    base.update(overrides)
    return base


class OauthUsageEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_when_check_auth_fails(self) -> None:
        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = False
        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": MagicMock(),
            "db": MagicMock(),
            "oauth_usage_require_auth": True,
        }
        req = make_mocked_request(
            "GET",
            "/_oauth_usage",
            app=app,
            headers={"Authorization": "Bearer sp-bad"},
        )
        resp = await _oauth_usage_handler(req)
        self.assertEqual(resp.status, 401)
        body = json.loads(resp.body)
        self.assertEqual(body.get("error"), "unauthorized")

    async def test_auth_disabled_skips_check_even_if_token_invalid(self) -> None:
        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = False
        mock_pool.ensure_valid_token = AsyncMock()

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": MagicMock(),
            "db": mock_db,
        }
        req = make_mocked_request("GET", "/_oauth_usage", app=app)
        resp = await _oauth_usage_handler(req)
        self.assertEqual(resp.status, 200)
        mock_pool.check_auth.assert_not_called()

    async def test_skips_non_oauth_rows(self) -> None:
        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(
            return_value=[_oauth_row(key_type="api_key", api_key="sk-ant-api")]
        )
        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool.ensure_valid_token = AsyncMock()
        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": MagicMock(),
            "db": mock_db,
        }
        req = make_mocked_request("GET", "/_oauth_usage", app=app)
        resp = await _oauth_usage_handler(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["keys"], [])
        mock_pool.ensure_valid_token.assert_not_called()

    async def test_returns_usage_json_on_200(self) -> None:
        usage_payload = {"five_hour": {"utilization": 12.0}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
        }
        req = make_mocked_request("GET", "/_oauth_usage", app=app)
        resp = await _oauth_usage_handler(req)
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertIn("generated_at", body)
        self.assertIn("cache_ttl_seconds", body)
        self.assertFalse(body["served_from_cache"])
        self.assertIsNone(body["cached_until"])
        self.assertNotIn("last_failure", body)
        self.assertEqual(len(body["keys"]), 1)
        row = body["keys"][0]
        self.assertEqual(row["id"], "oauth-key-1")
        self.assertEqual(row["usage"], usage_payload)
        self.assertEqual(row["http_status"], 200)

        mock_client.get.assert_awaited_once()
        call_kw = mock_client.get.await_args
        self.assertIn("/api/oauth/usage", call_kw[0][0])
        hdrs = call_kw[1]["headers"]
        self.assertEqual(hdrs["Authorization"], "Bearer fresh-token")
        self.assertIn("anthropic-beta", hdrs)

    async def test_returns_expires_at_and_refresh_due_in_human(self) -> None:
        usage_payload = {"five_hour": {"utilization": 12.0}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        now_seconds = 1_700_000_000
        expires_at = (now_seconds * 1000) + (10 * 60 * 1000)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row(expires_at=expires_at)])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
        }
        with patch("smart_proxy.anthropic_proxy.time.time", return_value=now_seconds):
            resp = await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))

        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        row = body["keys"][0]
        self.assertEqual(row["expires_at"], expires_at)
        self.assertEqual(row["refresh_due_in_human"], "in 5m")

    async def test_refresh_blocked_surfaces_error(self) -> None:
        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value=AnthropicKeyPool._REFRESH_BLOCKED)

        mock_client = MagicMock()
        mock_client.get = AsyncMock()

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
        }
        req = make_mocked_request("GET", "/_oauth_usage", app=app)
        resp = await _oauth_usage_handler(req)
        body = json.loads(resp.body)
        self.assertEqual(body["keys"][0]["error"], "oauth_refresh_rate_limited")
        self.assertFalse(body["served_from_cache"])
        self.assertIsNone(body["cached_until"])
        self.assertIn("last_failure", body)
        self.assertEqual(body["last_failure"]["error"], "oauth_refresh_rate_limited")
        mock_client.get.assert_not_called()

    async def test_cache_reuses_payload_within_ttl(self) -> None:
        usage_payload = {"five_hour": {"utilization": 12.0}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
            "oauth_usage_cache_seconds": 60,
            "_oauth_usage_cache_lock": asyncio.Lock(),
            "_oauth_usage_cache_entry": None,
        }
        with patch(
            "smart_proxy.anthropic_proxy.time.monotonic",
            side_effect=[100.0, 150.0],
        ):
            first = await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))
            second = await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))
        self.assertEqual(mock_client.get.await_count, 1)
        first_body = json.loads(first.body)
        second_body = json.loads(second.body)
        self.assertEqual(first_body["cached_until"], second_body["cached_until"])
        self.assertFalse(first_body["served_from_cache"])
        self.assertTrue(second_body["served_from_cache"])
        self.assertNotIn("last_failure", second_body)

    async def test_cache_expires_after_ttl(self) -> None:
        usage_payload = {"five_hour": {"utilization": 12.0}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
            "oauth_usage_cache_seconds": 60,
            "_oauth_usage_cache_lock": asyncio.Lock(),
            "_oauth_usage_cache_entry": None,
        }
        with patch(
            "smart_proxy.anthropic_proxy.time.monotonic",
            side_effect=[100.0, 150.0, 200.0],
        ):
            await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))
            await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))
            await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))
        self.assertEqual(mock_client.get.await_count, 2)

    async def test_failed_payload_is_not_cached_and_returns_last_failure(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {
            "error": {"type": "rate_limit_error", "message": "Rate limited. Please try again later."}
        }
        mock_resp.headers = {
            "request-id": "req_fail_123",
            "retry-after": "60",
            "x-extra-debug": "yes",
        }
        mock_resp.text = json.dumps(mock_resp.json.return_value)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
            "oauth_usage_cache_seconds": 60,
            "_oauth_usage_cache_lock": asyncio.Lock(),
            "_oauth_usage_cache_entry": None,
        }
        with patch(
            "smart_proxy.anthropic_proxy.time.monotonic",
            side_effect=[100.0, 150.0],
        ):
            first = await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))
            second = await _oauth_usage_handler(make_mocked_request("GET", "/_oauth_usage", app=app))
        self.assertEqual(mock_client.get.await_count, 2)
        first_body = json.loads(first.body)
        second_body = json.loads(second.body)
        self.assertIsNone(first_body["cached_until"])
        self.assertFalse(first_body["served_from_cache"])
        self.assertEqual(first_body["last_failure"]["http_status"], 429)
        self.assertEqual(first_body["last_failure"]["response_headers"]["retry-after"], "60")
        self.assertEqual(first_body["last_failure"]["response_headers"]["x-extra-debug"], "yes")
        self.assertIn("last_failure", second_body)

    async def test_success_records_window_observations(self) -> None:
        usage_payload = {
            "five_hour": {
                "utilization": 4.0,
                "resets_at": "2026-07-02T02:10:00.028978+00:00",
            },
            "seven_day": {
                "utilization": 1.0,
                "resets_at": "2026-07-02T11:00:00.028998+00:00",
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])
        mock_db.record_oauth_window_observations = AsyncMock(return_value=[])

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
        }
        resp = await _oauth_usage_handler(
            make_mocked_request("GET", "/_oauth_usage", app=app)
        )
        self.assertEqual(resp.status, 200)
        mock_db.record_oauth_window_observations.assert_awaited_once()
        call = mock_db.record_oauth_window_observations.await_args
        self.assertEqual(call[0][0], "oauth-key-1")
        kinds = {o["window_kind"] for o in call[0][1]}
        self.assertEqual(kinds, {"five_hour", "seven_day"})

    async def test_recording_failure_does_not_break_response(self) -> None:
        usage_payload = {
            "seven_day": {
                "utilization": 1.0,
                "resets_at": "2026-07-02T11:00:00+00:00",
            },
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = usage_payload

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])
        mock_db.record_oauth_window_observations = AsyncMock(
            side_effect=RuntimeError("db locked")
        )

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
        }
        resp = await _oauth_usage_handler(
            make_mocked_request("GET", "/_oauth_usage", app=app)
        )
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["keys"][0]["usage"], usage_payload)
        self.assertNotIn("error", body["keys"][0])

    async def test_upstream_error_does_not_record(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"error": {"type": "rate_limit_error"}}
        mock_resp.headers = {}
        mock_resp.text = "{}"

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)

        mock_db = MagicMock()
        mock_db.list_anthropic_keys = AsyncMock(return_value=[_oauth_row()])
        mock_db.record_oauth_window_observations = AsyncMock()

        mock_pool = MagicMock()
        mock_pool.check_auth.return_value = True
        mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
        mock_pool.ensure_valid_token = AsyncMock(return_value="fresh-token")

        app = {
            "anthropic_pool": mock_pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": mock_client,
            "db": mock_db,
        }
        resp = await _oauth_usage_handler(
            make_mocked_request("GET", "/_oauth_usage", app=app)
        )
        self.assertEqual(resp.status, 200)
        mock_db.record_oauth_window_observations.assert_not_awaited()


class SmartProxyLimitBlockTests(unittest.IsolatedAsyncioTestCase):
    def _limiter(self, limit=25.0):
        limiter = MagicMock()
        limiter.snapshot.return_value = {"daily_usd": {
            "limit_usd": limit, "spent_usd": 18.42, "remaining_usd": 6.58,
            "percent": 73.7, "resets_at": "2026-07-31T00:00:00+02:00",
            "exceeded": False,
        }}
        return limiter

    def test_injects_entry_into_each_usage_limits_array(self):
        payload = {"keys": [{"id": "k1", "usage": {
            "five_hour": {"utilization": 12},
            "limits": [{"kind": "weekly_scoped", "percent": 3}],
        }}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(), "sp-a")
        limits = out["keys"][0]["usage"]["limits"]
        self.assertEqual(len(limits), 2)
        self.assertEqual(limits[0]["kind"], "weekly_scoped")
        self.assertEqual(limits[1], {
            "kind": "smartproxy_daily_usd",
            "source": "smartproxy",
            "limit_usd": 25.0,
            "spent_usd": 18.42,
            "percent": 73.7,
            "resets_at": "2026-07-31T00:00:00+02:00",
        })

    def test_creates_the_limits_array_when_upstream_has_none(self):
        payload = {"keys": [{"id": "k1", "usage": {"five_hour": {"utilization": 12}}}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(), "sp-a")
        self.assertEqual(len(out["keys"][0]["usage"]["limits"]), 1)

    def test_does_not_mutate_the_input_payload(self):
        payload = {"keys": [{"id": "k1", "usage": {"limits": []}}]}
        anthropic_proxy._inject_smartproxy_limits(payload, self._limiter(), "sp-a")
        self.assertEqual(payload["keys"][0]["usage"]["limits"], [])

    def test_unlimited_key_appends_nothing(self):
        payload = {"keys": [{"id": "k1", "usage": {"limits": []}}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(limit=None), "sp-a")
        self.assertEqual(out["keys"][0]["usage"]["limits"], [])

    def test_entry_without_usage_is_left_alone(self):
        payload = {"keys": [{"id": "k1", "error": "no_valid_oauth_token"}]}
        out = anthropic_proxy._inject_smartproxy_limits(
            payload, self._limiter(), "sp-a")
        self.assertEqual(out["keys"][0], {"id": "k1", "error": "no_valid_oauth_token"})


class SmartProxyLimitHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_payload_is_not_poisoned_between_keys(self):
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.side_effect = lambda t: t in ("sp-a", "sp-b")
        limiter = MagicMock()

        def snapshot(key, now=None):
            spent = {"sp-a": 1.0, "sp-b": 2.0}[key]
            return {"daily_usd": {
                "limit_usd": 10.0, "spent_usd": spent, "remaining_usd": 10.0 - spent,
                "percent": spent * 10, "resets_at": "2026-07-31T00:00:00+02:00",
                "exceeded": False,
            }}
        limiter.snapshot.side_effect = snapshot

        app = {
            "anthropic_pool": pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": MagicMock(),
            "db": MagicMock(),
            "key_limiter": limiter,
            "oauth_usage_cache_seconds": 60,
            "_oauth_usage_cache_lock": asyncio.Lock(),
            "_oauth_usage_cache_entry": None,
        }
        built = [{"id": "k1", "usage": {"limits": []}}]
        with patch.object(
            anthropic_proxy, "_build_oauth_usage_payload",
            AsyncMock(return_value=(built, None)),
        ):
            first = await anthropic_proxy._oauth_usage_handler(
                make_mocked_request("GET", "/_oauth_usage?key=sp-a", app=app))
            second = await anthropic_proxy._oauth_usage_handler(
                make_mocked_request("GET", "/_oauth_usage?key=sp-b", app=app))

        a = json.loads(first.body)["keys"][0]["usage"]["limits"]
        b = json.loads(second.body)["keys"][0]["usage"]["limits"]
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)      # not 2 — the cache must not accumulate
        self.assertEqual(a[0]["spent_usd"], 1.0)
        self.assertEqual(b[0]["spent_usd"], 2.0)   # served from cache, but live

    async def test_no_key_param_leaves_the_payload_untouched(self):
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.is_proxy_key.return_value = False
        app = {
            "anthropic_pool": pool, "http_client": MagicMock(), "db": MagicMock(),
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "key_limiter": MagicMock(), "oauth_usage_cache_seconds": 0,
        }
        built = [{"id": "k1", "usage": {"limits": []}}]
        with patch.object(
            anthropic_proxy, "_build_oauth_usage_payload",
            AsyncMock(return_value=(built, None)),
        ):
            resp = await anthropic_proxy._oauth_usage_handler(
                make_mocked_request("GET", "/_oauth_usage", app=app))
        self.assertEqual(json.loads(resp.body)["keys"][0]["usage"]["limits"], [])


if __name__ == "__main__":
    unittest.main()
