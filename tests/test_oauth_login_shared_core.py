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
    """/callback keeps its plain-text / HTML behaviour.

    Named "legacy" because it once also served /_oauth/submit, the
    unauthenticated paste-the-code form, which has since been removed."""

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
