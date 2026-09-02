from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import (
    AnthropicKeyPool,
    _AnthropicKey,
    _standby_keepwarm_step,
    _standby_keepwarm_sleep_seconds,
    _STANDBY_KEEPWARM_FLOOR_S,
    _STANDBY_KEEPWARM_CAP_S,
)
from smart_proxy.anthropic_oauth import OAuthRefreshError
from tests.db_test_utils import connect_test_database


def _future_ms(minutes: float) -> int:
    return int(time.time() * 1000) + int(minutes * 60_000)


class _FakeResponse:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = json.dumps(data)
        self.request = None

    def json(self) -> dict:
        return self._data

    async def aclose(self) -> None:
        return None


class _FakeClient:
    """post = /token refresh, request = activation, send = inference.

    ``refresh_exc`` (if set) is raised from the /token call to simulate a failed refresh."""

    def __init__(self, *, refresh_exc: Exception | None = None) -> None:
        self.refresh_exc = refresh_exc
        self.post_calls: list[str] = []
        self.request_calls: list[str] = []
        self.send_calls: list[str] = []

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.post_calls.append(url)
        if self.refresh_exc is not None:
            raise self.refresh_exc
        return _FakeResponse(
            {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800}
        )

    async def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        self.request_calls.append(url)
        return _FakeResponse({}, 200)

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        self.send_calls.append(str(getattr(req, "url", "")))
        return _FakeResponse({}, 200)

    def build_request(self, method: str, url: str, headers: dict, content: bytes | None):
        return SimpleNamespace(method=method, url=url, headers=headers, content=content)


def _oauth_key(
    *,
    key_id: str = "k",
    expires_minutes: float,
    role: str = "standby",
    status: str = "active",
    key_type: str = "oauth",
) -> _AnthropicKey:
    return _AnthropicKey(
        key_id=key_id,
        key_type=key_type,
        status=status,
        api_key=None,
        access_token="tok",
        refresh_token="r",
        client_id="c",
        expires_at=_future_ms(expires_minutes),
        role=role,
    )


class IsExpiredBufferTests(unittest.TestCase):
    def test_is_expired_respects_custom_buffer(self) -> None:
        key = _oauth_key(expires_minutes=90)
        # Default 5-min buffer: a token with 90 min of life is NOT due.
        self.assertFalse(key.is_expired())
        # 120-min buffer: the same token IS due for a proactive refresh.
        self.assertTrue(key.is_expired(120 * 60 * 1000))

    def test_is_expired_false_for_non_oauth_regardless_of_buffer(self) -> None:
        key = _AnthropicKey(
            key_id="k", key_type="api_key", status="active",
            api_key="sk", access_token=None, refresh_token=None,
            client_id="", expires_at=None, role="standby",
        )
        self.assertFalse(key.is_expired(120 * 60 * 1000))


class SleepCalcTests(unittest.TestCase):
    _BUF = 120 * 60 * 1000  # 2h

    def _sleep(self, keys: list[_AnthropicKey]) -> float:
        return _standby_keepwarm_sleep_seconds(
            keys, int(time.time() * 1000), self._BUF,
            floor_s=_STANDBY_KEEPWARM_FLOOR_S, cap_s=_STANDBY_KEEPWARM_CAP_S,
        )

    def test_no_standby_returns_cap(self) -> None:
        self.assertEqual(self._sleep([]), _STANDBY_KEEPWARM_CAP_S)

    def test_standby_due_far_out_is_capped(self) -> None:
        # expires in 8h, buffer 2h → due in ~6h, far beyond the cap.
        self.assertEqual(self._sleep([_oauth_key(expires_minutes=480)]), _STANDBY_KEEPWARM_CAP_S)

    def test_standby_due_now_or_past_is_floored(self) -> None:
        # expires in 60 min, buffer 120 min → already due → floor (no busy-spin).
        self.assertEqual(self._sleep([_oauth_key(expires_minutes=60)]), _STANDBY_KEEPWARM_FLOOR_S)

    def test_standby_due_between_floor_and_cap(self) -> None:
        # expires in 125 min, buffer 120 min → due in ~5 min (300 s).
        s = self._sleep([_oauth_key(expires_minutes=125)])
        self.assertTrue(_STANDBY_KEEPWARM_FLOOR_S < s <= _STANDBY_KEEPWARM_CAP_S)
        self.assertAlmostEqual(s, 300, delta=5)

    def test_ignores_primary_and_inactive_standby(self) -> None:
        keys = [
            _oauth_key(key_id="p", expires_minutes=1, role="primary"),          # due now, but primary
            _oauth_key(key_id="s-dead", expires_minutes=1, status="inactive"),  # due now, but inactive
            _oauth_key(key_id="s-live", expires_minutes=480),                   # far out
        ]
        self.assertEqual(self._sleep(keys), _STANDBY_KEEPWARM_CAP_S)


class _StepTestBase(unittest.TestCase):
    _BUFFER_MIN = 120

    async def _make_app(self, db, client, keys: list[dict]) -> dict:
        for k in keys:
            await db.insert_anthropic_key(
                id=k["id"], key_type="oauth",
                access_token=k.get("access_token", f"old-access-{k['id']}"),
                refresh_token=k.get("refresh_token", f"old-refresh-{k['id']}"),
                client_id="client-id-123",
                expires_at=_future_ms(k["expires_minutes"]),
                scopes=json.dumps(["user:profile", "user:inference"]),
                name=k["id"], role=k["role"],
            )
        pool = AnthropicKeyPool(db)
        await pool.reload()
        return {
            "db": db,
            "anthropic_pool": pool,
            "http_client": client,
            "standby_keepwarm_buffer_ms": self._BUFFER_MIN * 60 * 1000,
        }


class StepRefreshTests(_StepTestBase):
    def test_refreshes_standby_within_buffer_and_leaves_primary_alone(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    client = _FakeClient()
                    app = await self._make_app(db, client, [
                        {"id": "sb", "role": "standby", "expires_minutes": 60},   # < 2h buffer → due
                        {"id": "pr", "role": "primary", "expires_minutes": 60},   # not the loop's job
                    ])
                    sleep_s = await _standby_keepwarm_step(app)

                    sb = await db.get_anthropic_key("sb")
                    self.assertEqual(sb["access_token"], "new-access")
                    self.assertEqual(sb["refresh_token"], "new-refresh")
                    self.assertEqual(sb["status"], "active")

                    ev = [e for e in await db.list_anthropic_key_events("sb")
                          if e["event_type"] == "refresh_succeeded"]
                    self.assertEqual(len(ev), 1)
                    self.assertEqual(ev[0]["source"], "standby_keepwarm")

                    # Primary is untouched by the loop, and no inference/activation ran.
                    pr = await db.get_anthropic_key("pr")
                    self.assertEqual(pr["access_token"], "old-access-pr")
                    self.assertEqual(client.send_calls, [])
                    self.assertEqual(client.request_calls, [])   # activate=False
                    # After refresh the token is ~8h out → next check is capped.
                    self.assertEqual(sleep_s, _STANDBY_KEEPWARM_CAP_S)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_skips_standby_outside_buffer(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    client = _FakeClient()
                    app = await self._make_app(db, client, [
                        {"id": "sb", "role": "standby", "expires_minutes": 300},  # 5h > 2h buffer
                    ])
                    await _standby_keepwarm_step(app)

                    sb = await db.get_anthropic_key("sb")
                    self.assertEqual(sb["access_token"], "old-access-sb")   # untouched
                    self.assertEqual(client.post_calls, [])                 # no /token call
                finally:
                    await db.close()

        asyncio.run(run())

    def test_refreshes_standby_even_when_primary_cooled(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    client = _FakeClient()
                    app = await self._make_app(db, client, [
                        {"id": "sb", "role": "standby", "expires_minutes": 60},
                        {"id": "pr", "role": "primary", "expires_minutes": 300},
                    ])
                    pool = app["anthropic_pool"]
                    primary = next(k for k in pool._keys if k.key_id == "pr")
                    pool.cooldown(primary, 3600)   # primary parked — must NOT gate keep-warm

                    await _standby_keepwarm_step(app)

                    sb = await db.get_anthropic_key("sb")
                    self.assertEqual(sb["access_token"], "new-access")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_failed_refresh_keeps_old_token_while_still_valid(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    client = _FakeClient(refresh_exc=OAuthRefreshError(
                        "invalid_grant", status_code=400, error_code="invalid_grant"))
                    app = await self._make_app(db, client, [
                        {"id": "sb", "role": "standby", "expires_minutes": 60},  # within buffer, still valid
                    ])
                    await _standby_keepwarm_step(app)

                    sb = await db.get_anthropic_key("sb")
                    # Refresh failed but the token has ~60 min left → keep serving it, do NOT deactivate.
                    self.assertEqual(sb["access_token"], "old-access-sb")
                    self.assertEqual(sb["status"], "active")
                    self.assertTrue(client.post_calls)   # it did attempt the refresh
                finally:
                    await db.close()

        asyncio.run(run())

    def test_deactivates_standby_when_token_already_dead(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    client = _FakeClient(refresh_exc=OAuthRefreshError(
                        "invalid_grant", status_code=400, error_code="invalid_grant"))
                    app = await self._make_app(db, client, [
                        {"id": "sb", "role": "standby", "expires_minutes": -10},  # already expired
                    ])
                    await _standby_keepwarm_step(app)

                    sb = await db.get_anthropic_key("sb")
                    self.assertEqual(sb["status"], "inactive")
                finally:
                    await db.close()

        asyncio.run(run())
