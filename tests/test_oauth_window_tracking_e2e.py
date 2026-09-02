from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aiohttp.test_utils import make_mocked_request

from smart_proxy.anthropic_proxy import (
    AnthropicKeyPool,
    _oauth_usage_handler,
    _oauth_usage_history_handler,
)
from smart_proxy.db import Database


def _usage_payload(resets_at: str, utilization: float) -> dict:
    return {
        "seven_day": {"utilization": utilization, "resets_at": resets_at},
    }


class OauthWindowTrackingE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_polls_accumulate_and_history_reports_span(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                now = datetime.now(timezone.utc).isoformat()
                await db.db.execute(
                    """INSERT INTO anthropic_keys
                       (id, key_type, status, access_token, refresh_token,
                        expires_at, name, created_at, updated_at)
                       VALUES (?, 'oauth', 'active', 'tok', 'ref',
                               9999999999999, 'e2e', ?, ?)""",
                    ("oauth-key-1", now, now),
                )
                await db.db.commit()

                responses = [
                    _usage_payload("2026-07-02T11:00:00.028998+00:00", 71.0),
                    _usage_payload("2026-07-02T11:00:00.874164+00:00", 72.0),
                    _usage_payload("2026-07-06T16:00:00.111111+00:00", 1.0),
                ]

                def _resp(payload: dict) -> MagicMock:
                    m = MagicMock()
                    m.status_code = 200
                    m.json.return_value = payload
                    return m

                mock_client = MagicMock()
                mock_client.get = AsyncMock(
                    side_effect=[_resp(p) for p in responses]
                )

                mock_pool = MagicMock()
                mock_pool.check_auth.return_value = True
                mock_pool._REFRESH_BLOCKED = AnthropicKeyPool._REFRESH_BLOCKED
                mock_pool.ensure_valid_token = AsyncMock(return_value="tok")
                mock_pool.get_loaded_key.return_value = None

                app = {
                    "anthropic_pool": mock_pool,
                    "http_client": mock_client,
                    "db": db,
                }
                for _ in responses:
                    resp = await _oauth_usage_handler(
                        make_mocked_request("GET", "/_oauth_usage", app=app)
                    )
                    self.assertEqual(resp.status, 200)

                hist = await _oauth_usage_history_handler(
                    make_mocked_request("GET", "/_oauth_usage_history", app=app)
                )
                body = json.loads(hist.body)
                seven = body["keys"][0]["windows"]["seven_day"]
                self.assertEqual(len(seven), 2)
                self.assertEqual(seven[0]["resets_at"], "2026-07-06T16:00:00+00:00")
                self.assertEqual(seven[0]["span_days_since_prev"], 4.21)
                self.assertEqual(seven[1]["observations"], 2)  # jittered dup merged
                self.assertEqual(seven[1]["max_utilization"], 72.0)
                self.assertEqual(seven[1]["last_utilization"], 72.0)
            finally:
                await db.close()

    async def test_token_attribution_pending_drain_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "t.db"))
            await db.connect()
            try:
                now = datetime.now(timezone.utc)
                iso_now = now.isoformat()
                await db.db.execute(
                    """INSERT INTO anthropic_keys
                       (id, key_type, status, access_token, refresh_token,
                        expires_at, name, created_at, updated_at)
                       VALUES (?, 'oauth', 'active', 'tok', 'ref',
                               9999999999999, 'e2e', ?, ?)""",
                    ("oauth-key-1", iso_now, iso_now),
                )
                await db.db.commit()

                def _minute(dt: datetime) -> str:
                    return dt.replace(second=0, microsecond=0).isoformat()

                delta = {
                    "key_id": "oauth-key-1", "model": "claude-sonnet-5",
                    "input_tokens": 100, "output_tokens": 50,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "cache_creation_5m_tokens": 0,
                    "cache_creation_1h_tokens": 0,
                    "web_search_requests": 0, "requests": 2,
                }

                # Poll observes a window; tokens then arrive after it expired.
                first = now + timedelta(hours=1)
                await db.record_oauth_window_observations("oauth-key-1", [{
                    "window_kind": "seven_day",
                    "resets_at": _minute(first),
                    "resets_at_raw": first.isoformat(),
                    "utilization": 90.0,
                }])
                late = (now + timedelta(hours=2)).isoformat()
                await db.attribute_oauth_window_usage([delta], now=late)
                self.assertEqual(
                    len(await db.list_oauth_window_usage_pending("oauth-key-1")),
                    1)

                # Next poll sees the successor window: pending drains into it.
                second = now + timedelta(days=7)
                await db.record_oauth_window_observations("oauth-key-1", [{
                    "window_kind": "seven_day",
                    "resets_at": _minute(second),
                    "resets_at_raw": second.isoformat(),
                    "utilization": 1.0,
                }])
                self.assertEqual(
                    await db.list_oauth_window_usage_pending("oauth-key-1"), [])

                # More traffic while the new window is live.
                await db.attribute_oauth_window_usage([delta], now=late)

                app = {"anthropic_pool": MagicMock(), "db": db}
                hist = await _oauth_usage_history_handler(
                    make_mocked_request("GET", "/_oauth_usage_history", app=app))
                body = json.loads(hist.body)
                seven = body["keys"][0]["windows"]["seven_day"]
                self.assertEqual(len(seven), 2)
                newest = seven[0]
                self.assertEqual(newest["usage"]["totals"]["input_tokens"], 200)
                self.assertEqual(newest["usage"]["totals"]["requests"], 4)
                self.assertIn(
                    "claude-sonnet-5", newest["usage"]["models"])
                self.assertIsNone(seven[1]["usage"])
                self.assertEqual(body["keys"][0]["pending"], {})
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
