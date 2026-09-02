from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import (
    AnthropicKeyPool,
    TOKEN_URL,
    _AnthropicKey,
    _build_oauth_usage_payload,
    _refresh_oauth_token,
    _run_oauth_smoke_pass,
)
from tests.db_test_utils import connect_test_database


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


class _FakeAsyncClient:
    def __init__(self, response_data: dict, status_code: int = 200) -> None:
        self._response_data = response_data
        self._status_code = status_code
        self.calls: list[tuple[str, dict]] = []
        self.request_calls: list[tuple[str, str, dict]] = []
        self.send_calls: list[tuple[str, object, bool]] = []
        self.last_build: dict | None = None

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse(self._response_data, self._status_code)

    async def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        self.request_calls.append((method, url, kwargs))
        return _FakeResponse({}, 200)

    def build_request(self, method: str, url: str, headers: dict, content: bytes | None):
        self.last_build = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "content": content,
        }
        return SimpleNamespace(method=method, url=url, headers=headers, content=content)

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        self.send_calls.append((req.method, req.url, stream))
        return _FakeResponse({}, 200)

    async def get(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse({"five_hour": {"utilization": 12.0}}, 200)


class _FakeRawResponse:
    """A response httpx handed back without decoding the body.

    httpx skips a ``Content-Encoding`` it has no decoder for and returns the raw
    compressed bytes, so ``.json()`` (which is ``json.loads(self.content)``)
    explodes on binary. Mirrors that exactly."""

    def __init__(self, content: bytes, status_code: int = 200,
                 content_encoding: str = "br") -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"content-encoding": content_encoding} if content_encoding else {}
        self.request = None

    def json(self) -> dict:
        return json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    async def aclose(self) -> None:
        return None


class _FakeAsyncClientRaw(_FakeAsyncClient):
    """`.post` (the /token call) returns an undecoded body."""

    def __init__(self, content: bytes, status_code: int = 200,
                 content_encoding: str = "br") -> None:
        super().__init__({}, status_code)
        self._content = content
        self._content_encoding = content_encoding

    async def post(self, url: str, **kwargs) -> _FakeRawResponse:
        self.calls.append((url, kwargs))
        return _FakeRawResponse(self._content, self._status_code, self._content_encoding)


def _brotli_body(payload: dict) -> bytes:
    import brotli

    return brotli.compress(json.dumps(payload).encode())


class _FakeAsyncClientActivationFail(_FakeAsyncClient):
    async def request(self, method: str, url: str, **kwargs) -> _FakeResponse:
        self.request_calls.append((method, url, kwargs))
        return _FakeResponse({}, 401)


class _FakeAsyncClientRefreshRaises(_FakeAsyncClient):
    """`.post` (the /token call) raises the given exception; activation still 200."""
    def __init__(self, exc: Exception) -> None:
        super().__init__({}, 200)
        self._exc = exc

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        self.calls.append((url, kwargs))
        raise self._exc


def _future_ms(minutes: float) -> int:
    import time as _t
    return int(_t.time() * 1000) + int(minutes * 60_000)


class AnthropicOAuthRefreshTests(unittest.TestCase):
    def test_refresh_request_includes_scope_and_client_id(self) -> None:
        async def run() -> None:
            client = _FakeAsyncClient(
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 28800,
                }
            )
            access, expires_at, refresh = await _refresh_oauth_token(
                client,
                refresh_token="old-refresh",
                client_id="client-id-123",
                scope="user:profile user:inference",
            )

            self.assertEqual(access, "new-access")
            self.assertGreater(expires_at, 0)
            self.assertEqual(refresh, "new-refresh")
            self.assertEqual(len(client.calls), 1)

            url, kwargs = client.calls[0]
            self.assertEqual(url, TOKEN_URL)
            self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
            self.assertEqual(kwargs["headers"]["Accept"], "application/json, text/plain, */*")
            self.assertEqual(kwargs["headers"]["Accept-Encoding"], "gzip, compress, deflate, br")
            self.assertEqual(kwargs["headers"]["User-Agent"], "axios/1.13.6")
            self.assertEqual(kwargs["headers"]["Connection"], "keep-alive")
            self.assertEqual(kwargs["json"]["grant_type"], "refresh_token")
            self.assertEqual(kwargs["json"]["refresh_token"], "old-refresh")
            self.assertEqual(kwargs["json"]["client_id"], "client-id-123")
            self.assertEqual(kwargs["json"]["scope"], "user:profile user:inference")

        asyncio.run(run())

    def test_activation_failure_persists_rotated_token_and_returns_it(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    await db.insert_anthropic_key(
                        id="key-2", key_type="oauth",
                        access_token="old-access", refresh_token="old-refresh",
                        client_id="client-id-123", expires_at=0,
                        scopes='["user:profile","user:inference"]', name="test-key-fail",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()
                    key = pool.pick()
                    assert key is not None

                    client = _FakeAsyncClientActivationFail(
                        {"access_token": "new-access", "refresh_token": "new-refresh",
                         "expires_in": 28800})
                    token = await pool.ensure_valid_token(key, client)

                    # Activation warmup failed, but the rotated token is durable.
                    self.assertEqual(token, "new-access")
                    self.assertEqual(key.access_token, "new-access")
                    self.assertEqual(key.refresh_token, "new-refresh")

                    row = await db.get_anthropic_key("key-2")
                    assert row is not None
                    self.assertEqual(row["access_token"], "new-access")
                    self.assertEqual(row["refresh_token"], "new-refresh")
                    self.assertEqual(row["status"], "active")

                    events = [e["event_type"] for e in await db.list_anthropic_key_events("key-2")]
                    self.assertEqual(events, ["refresh_attempt", "refresh_succeeded", "activation_failed"])
                finally:
                    await db.close()

        asyncio.run(run())

    def test_refresh_rotation_is_saved_to_same_db_row(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-1",
                        key_type="oauth",
                        access_token="old-access",
                        refresh_token="old-refresh",
                        client_id="client-id-123",
                        expires_at=0,  # force refresh
                        scopes=json.dumps(
                            [
                                "user:profile",
                                "user:inference",
                                "user:sessions:claude_code",
                            ]
                        ),
                        name="test-key",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()
                    key = pool.pick()
                    self.assertIsNotNone(key)
                    assert key is not None

                    client = _FakeAsyncClient(
                        {
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_in": 28800,
                        }
                    )
                    token = await pool.ensure_valid_token(
                        key,
                        client,
                        audit_op_id="op-refresh-success",
                        audit_source="scheduled_smoke",
                        audit_path="/v1/messages",
                        audit_model="claude-haiku-4-5-20251001",
                    )
                    self.assertEqual(token, "new-access")
                    self.assertEqual(key.access_token, "new-access")
                    self.assertEqual(key.refresh_token, "new-refresh")
                    self.assertEqual(key.name, "test-key")

                    row = await db.get_anthropic_key("key-1")
                    self.assertIsNotNone(row)
                    assert row is not None
                    self.assertEqual(row["access_token"], "new-access")
                    self.assertEqual(row["refresh_token"], "new-refresh")

                    snapshots = await db.list_anthropic_key_snapshots("key-1")
                    events = await db.list_anthropic_key_events("key-1")
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(snapshots[0]["snapshot_kind"], "before_refresh_update")
                    self.assertEqual(snapshots[0]["access_token"], "old-access")
                    self.assertEqual(snapshots[0]["refresh_token"], "old-refresh")
                    self.assertEqual([event["event_type"] for event in events], [
                        "refresh_attempt",
                        "refresh_succeeded",
                    ])
                    self.assertEqual(events[0]["source"], "scheduled_smoke")
                    self.assertEqual(events[1]["snapshot_id"], snapshots[0]["id"])

                    _, kwargs = client.calls[0]
                    self.assertEqual(
                        kwargs["json"]["scope"],
                        "user:profile user:inference user:sessions:claude_code",
                    )
                    self.assertEqual(len(client.request_calls), 6)
                    first_method, first_url, first_kwargs = client.request_calls[0]
                    self.assertEqual(first_method, "GET")
                    self.assertTrue(first_url.endswith("/api/claude_code_penguin_mode"))
                    self.assertEqual(
                        first_kwargs["headers"]["Authorization"],
                        "Bearer new-access",
                    )
                finally:
                    await db.close()

        asyncio.run(run())

    def test_ensure_valid_token_activate_false_skips_activation(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    await db.insert_anthropic_key(id="k", key_type="oauth", access_token="old",
                        refresh_token="old-r", client_id="c", expires_at=0,
                        scopes='["user:inference"]', name="k")
                    pool = AnthropicKeyPool(db); await pool.reload(); key = pool.pick()
                    client = _FakeAsyncClient({"access_token": "new", "refresh_token": "new-r", "expires_in": 28800})
                    token = await pool.ensure_valid_token(key, client, activate=False)
                    self.assertEqual(token, "new")
                    self.assertEqual(len(client.request_calls), 0)     # NO activation calls
                    self.assertEqual((await db.get_anthropic_key("k"))["access_token"], "new")
                finally:
                    await db.close()
        asyncio.run(run())

    def test_scheduled_smoke_pass_logs_refresh_failure_before_deactivation(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-refresh-fail",
                        key_type="oauth",
                        access_token="old-access",
                        refresh_token="old-refresh",
                        client_id="client-id-123",
                        expires_at=0,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="failing-key",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    client = _FakeAsyncClient(
                        {"error": "invalid_grant", "error_description": "Refresh token invalid"},
                        status_code=400,
                    )
                    app = {
                        "db": db,
                        "anthropic_pool": pool,
                        "http_client": client,
                        "disable_1m_context": False,
                    }

                    await _run_oauth_smoke_pass(app, "midday")

                    row = await db.get_anthropic_key("key-refresh-fail")
                    self.assertIsNotNone(row)
                    assert row is not None
                    self.assertEqual(row["status"], "inactive")

                    snapshots = await db.list_anthropic_key_snapshots("key-refresh-fail")
                    events = await db.list_anthropic_key_events("key-refresh-fail")
                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(snapshots[0]["snapshot_kind"], "before_status_change")
                    self.assertEqual(snapshots[0]["status"], "active")
                    self.assertEqual([event["event_type"] for event in events], [
                        "refresh_attempt",
                        "refresh_failed",
                        "status_change",
                    ])
                    self.assertEqual(events[0]["source"], "scheduled_smoke")
                    self.assertEqual(events[1]["decision"], "deactivate")
                    self.assertIn("HTTP 400", events[1]["error_message"])
                    self.assertEqual(events[2]["snapshot_id"], snapshots[0]["id"])
                finally:
                    await db.close()

        asyncio.run(run())

    def test_scheduled_smoke_pass_reuses_refresh_flow_before_probe(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-schedule",
                        key_type="oauth",
                        access_token="old-access",
                        refresh_token="old-refresh",
                        client_id="client-id-123",
                        expires_at=0,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="scheduled-key",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    client = _FakeAsyncClient(
                        {
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_in": 28800,
                        }
                    )
                    app = {
                        "db": db,
                        "anthropic_pool": pool,
                        "http_client": client,
                        "disable_1m_context": False,
                    }

                    await _run_oauth_smoke_pass(app, "morning")

                    self.assertEqual(len(client.calls), 1)
                    self.assertEqual(len(client.request_calls), 6)
                    self.assertEqual(len(client.send_calls), 1)
                    self.assertIsNotNone(client.last_build)
                    assert client.last_build is not None
                    self.assertEqual(
                        client.last_build["headers"]["Authorization"],
                        "Bearer new-access",
                    )
                    live_key = pool.pick()
                    self.assertIsNotNone(live_key)
                    assert live_key is not None
                    self.assertEqual(live_key.access_token, "new-access")
                    self.assertEqual(live_key.refresh_token, "new-refresh")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_scheduled_smoke_pass_skips_standby_entirely(self) -> None:
        # The standby is now kept warm by the dedicated keep-warm loop, so the smoke
        # pass must not touch it at all — no refresh, no inference, zero footprint.
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-standby",
                        key_type="oauth",
                        access_token="old-access-standby",
                        refresh_token="old-refresh-standby",
                        client_id="client-id-123",
                        expires_at=0,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="standby-key",
                        role="standby",
                    )
                    await db.insert_anthropic_key(
                        id="key-primary",
                        key_type="oauth",
                        access_token="old-access-primary",
                        refresh_token="old-refresh-primary",
                        client_id="client-id-123",
                        expires_at=0,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="primary-key",
                        role="primary",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    client = _FakeAsyncClient(
                        {
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_in": 28800,
                        }
                    )
                    app = {
                        "db": db,
                        "anthropic_pool": pool,
                        "http_client": client,
                        "disable_1m_context": False,
                    }

                    await _run_oauth_smoke_pass(app, "midday")

                    # Only the primary key sends an inference smoke request.
                    self.assertEqual(len(client.send_calls), 1)

                    # Standby is fully untouched: no refresh, token unchanged, no events.
                    standby_row = await db.get_anthropic_key("key-standby")
                    assert standby_row is not None
                    self.assertEqual(standby_row["access_token"], "old-access-standby")
                    standby_events = await db.list_anthropic_key_events("key-standby")
                    self.assertEqual(standby_events, [])

                    primary_events = await db.list_anthropic_key_events("key-primary")
                    primary_refresh_events = [e for e in primary_events if e["event_type"] == "refresh_succeeded"]
                    self.assertEqual(len(primary_refresh_events), 1)
                    self.assertEqual(primary_refresh_events[0]["source"], "scheduled_smoke")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_scheduled_smoke_pass_applies_claude_like_headers_when_enabled(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-smoke-claude-like",
                        key_type="oauth",
                        access_token="fresh-access",
                        refresh_token="refresh-token",
                        client_id="client-id-123",
                        expires_at=9999999999999,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="scheduled-key-claude-like",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    client = _FakeAsyncClient({})
                    app = {
                        "db": db,
                        "anthropic_pool": pool,
                        "http_client": client,
                        "disable_1m_context": False,
                        "claude_like": True,
                    }

                    await _run_oauth_smoke_pass(app, "midday")

                    self.assertEqual(len(client.send_calls), 1)
                    self.assertIsNotNone(client.last_build)
                    assert client.last_build is not None
                    headers = client.last_build["headers"]
                    self.assertEqual(headers["User-Agent"], "claude-cli/2.1.92 (external, cli)")
                    self.assertEqual(headers["x-app"], "cli")
                    UUID(headers["X-Claude-Code-Session-Id"])
                finally:
                    await db.close()

        asyncio.run(run())

    def test_oauth_usage_refresh_keeps_loaded_pool_key_in_sync(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-usage-sync",
                        key_type="oauth",
                        access_token="old-access",
                        refresh_token="old-refresh",
                        client_id="client-id-123",
                        expires_at=0,
                        scopes=json.dumps(["user:profile", "user:inference"]),
                        name="usage-sync-key",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()

                    client = _FakeAsyncClient(
                        {
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_in": 28800,
                        }
                    )

                    keys, last_failure = await _build_oauth_usage_payload(pool, client, db)

                    self.assertIsNone(last_failure)
                    self.assertEqual(len(keys), 1)
                    self.assertIn("usage", keys[0])
                    live_key = pool.pick()
                    self.assertIsNotNone(live_key)
                    assert live_key is not None
                    self.assertEqual(live_key.access_token, "new-access")
                    self.assertEqual(live_key.refresh_token, "new-refresh")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_usage_payload_skips_standby(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    await db.insert_anthropic_key(id="stby", key_type="oauth", access_token="a",
                        refresh_token="r", client_id="c", expires_at=0,
                        scopes='["user:inference"]', name="stby", role="standby")
                    pool = AnthropicKeyPool(db); await pool.reload()
                    client = _FakeAsyncClient({"access_token": "new", "refresh_token": "nr", "expires_in": 28800})
                    keys, _ = await _build_oauth_usage_payload(pool, client, db)
                    self.assertEqual(keys, [])                 # standby absent
                    self.assertEqual(len(client.calls), 0)     # no /token refresh for standby
                finally:
                    await db.close()
        asyncio.run(run())

    def test_refresh_raises_typed_error_on_4xx(self) -> None:
        from smart_proxy.anthropic_oauth import OAuthRefreshError

        async def run() -> None:
            client = _FakeAsyncClient(
                {"error": "invalid_grant", "error_description": "bad"},
                status_code=400,
            )
            with self.assertRaises(OAuthRefreshError) as ctx:
                await _refresh_oauth_token(client, refresh_token="r", client_id="c")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.error_code, "invalid_grant")
            self.assertIn("HTTP 400", str(ctx.exception))
            self.assertIsInstance(ctx.exception, RuntimeError)  # backward compat

        asyncio.run(run())

    def test_refresh_raises_typed_error_on_malformed_body(self) -> None:
        from smart_proxy.anthropic_oauth import OAuthRefreshError

        async def run() -> None:
            client = _FakeAsyncClient({"no_token": True}, status_code=200)
            with self.assertRaises(OAuthRefreshError) as ctx:
                await _refresh_oauth_token(client, refresh_token="r", client_id="c")
            self.assertEqual(ctx.exception.status_code, 200)
            # A 200 means Anthropic already rotated: the old refresh token is spent.
            self.assertTrue(ctx.exception.token_consumed)

        asyncio.run(run())

    def test_undecoded_compressed_2xx_body_is_recovered(self) -> None:
        # The rotated refresh token lives in this body and nowhere else — if we
        # cannot read it the key is dead. Decompress ourselves rather than lose it.
        async def run() -> None:
            client = _FakeAsyncClientRaw(
                _brotli_body({
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 28800,
                })
            )
            access, expires_at, refresh = await _refresh_oauth_token(
                client, refresh_token="old-refresh", client_id="c",
            )
            self.assertEqual(access, "new-access")
            self.assertEqual(refresh, "new-refresh")
            self.assertGreater(expires_at, 0)

        asyncio.run(run())

    def test_unreadable_2xx_body_raises_token_consumed_error(self) -> None:
        from smart_proxy.anthropic_oauth import OAuthRefreshError

        async def run() -> None:
            client = _FakeAsyncClientRaw(
                b"\xfd\xfe\xff not json", content_encoding="x-unknown",
            )
            with self.assertRaises(OAuthRefreshError) as ctx:
                await _refresh_oauth_token(client, refresh_token="r", client_id="c")
            self.assertEqual(ctx.exception.status_code, 200)
            self.assertTrue(ctx.exception.token_consumed)
            # The alert must name the encoding, or the next outage is another blind hunt.
            self.assertIn("x-unknown", str(ctx.exception))
            self.assertEqual(ctx.exception.error_code, "unreadable_response")

        asyncio.run(run())

    def test_4xx_error_code_survives_undecoded_body(self) -> None:
        from smart_proxy.anthropic_oauth import OAuthRefreshError

        async def run() -> None:
            client = _FakeAsyncClientRaw(
                _brotli_body({
                    "error": "invalid_grant",
                    "error_description": "Refresh token not found or invalid",
                }),
                status_code=400,
            )
            with self.assertRaises(OAuthRefreshError) as ctx:
                await _refresh_oauth_token(client, refresh_token="r", client_id="c")
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.error_code, "invalid_grant")
            self.assertIn("invalid_grant", str(ctx.exception))
            self.assertFalse(ctx.exception.token_consumed)  # 4xx: never reached the server's rotation

        asyncio.run(run())


class RefreshFailurePolicyTests(unittest.TestCase):
    async def _pool_with_key(self, db, expires_at: int):
        await db.insert_anthropic_key(
            id="k", key_type="oauth", access_token="valid-access", refresh_token="old-refresh",
            client_id="client-id-123", expires_at=expires_at,
            scopes='["user:inference"]', name="k")
        pool = AnthropicKeyPool(db)
        await pool.reload()
        key = pool.pick()
        assert key is not None
        return pool, key

    def test_invalid_grant_while_valid_keeps_serving(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, _future_ms(2))  # inside 5-min buffer, still valid
                    client = _FakeAsyncClient(
                        {"error": "invalid_grant", "error_description": "bad"}, status_code=400)
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, "valid-access")            # kept serving
                    self.assertIn("k", pool._refresh_dead)             # latched (refresh token dead)
                    row = await db.get_anthropic_key("k")
                    self.assertEqual(row["status"], "active")          # NOT deactivated
                    events = [e["event_type"] for e in await db.list_anthropic_key_events("k")]
                    self.assertIn("refresh_deferred", events)
                finally:
                    await db.close()
        asyncio.run(run())

    def test_unreadable_2xx_latches_instead_of_retrying(self) -> None:
        # Regression: an unreadable 2xx used to be classified transient, so the next
        # attempt spent the already-rotated refresh token and earned a fatal 400.
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, _future_ms(2))
                    client = _FakeAsyncClientRaw(b"\xfd\xfe\xff", content_encoding="x-unknown")
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, "valid-access")   # access token still serves
                    self.assertIn("k", pool._refresh_dead)    # latched — no retry burns the corpse
                    row = await db.get_anthropic_key("k")
                    self.assertEqual(row["status"], "active")
                    self.assertEqual(len(client.calls), 1)
                finally:
                    await db.close()
        asyncio.run(run())

    def test_network_error_while_valid_keeps_serving(self) -> None:
        async def run() -> None:
            import httpx
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, _future_ms(2))
                    client = _FakeAsyncClientRefreshRaises(httpx.ConnectError("boom"))
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, "valid-access")
                    row = await db.get_anthropic_key("k")
                    self.assertEqual(row["status"], "active")
                finally:
                    await db.close()
        asyncio.run(run())

    def test_invalid_grant_while_expired_returns_none(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)  # truly expired
                    client = _FakeAsyncClient(
                        {"error": "invalid_grant"}, status_code=400)
                    token = await pool.ensure_valid_token(key, client)
                    self.assertIsNone(token)  # caller deactivates
                finally:
                    await db.close()
        asyncio.run(run())

    def test_network_while_expired_blocks_not_none(self) -> None:
        async def run() -> None:
            import httpx
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)
                    client = _FakeAsyncClientRefreshRaises(httpx.ConnectError("boom"))
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, pool._REFRESH_BLOCKED)  # not None → not deactivated
                finally:
                    await db.close()
        asyncio.run(run())

    def test_transient_escape_valve_deactivates_after_max(self) -> None:
        async def run() -> None:
            import httpx
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    from smart_proxy.anthropic_proxy import _MAX_TRANSIENT_REFRESH_FAILS
                    pool, key = await self._pool_with_key(db, 0)
                    client = _FakeAsyncClientRefreshRaises(httpx.ConnectError("boom"))
                    result = None
                    for _ in range(_MAX_TRANSIENT_REFRESH_FAILS):
                        pool._cooldowns.pop("k", None)  # clear the per-attempt cooldown so we can retry
                        result = await pool.ensure_valid_token(key, client)
                    self.assertIsNone(result)  # escape valve → deactivate
                finally:
                    await db.close()
        asyncio.run(run())

    def test_backoff_suppresses_second_refresh(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, _future_ms(2))
                    client = _FakeAsyncClient({"error": "invalid_grant"}, status_code=400)
                    await pool.ensure_valid_token(key, client)   # 1st call → 1 /token call, sets backoff
                    await pool.ensure_valid_token(key, client)   # 2nd call → within backoff, no /token call
                    self.assertEqual(len(client.calls), 1)
                finally:
                    await db.close()
        asyncio.run(run())

    def test_missing_refresh_token_while_valid_keeps_serving(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    await db.insert_anthropic_key(
                        id="k", key_type="oauth", access_token="valid-access", refresh_token=None,
                        client_id="c", expires_at=_future_ms(2), scopes='["user:inference"]', name="k")
                    pool = AnthropicKeyPool(db); await pool.reload(); key = pool.pick()
                    token = await pool.ensure_valid_token(key, _FakeAsyncClient({}))
                    self.assertEqual(token, "valid-access")
                    self.assertEqual((await db.get_anthropic_key("k"))["status"], "active")
                finally:
                    await db.close()
        asyncio.run(run())

    def test_stale_object_invalid_grant_rereads_and_blocks(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)  # expired
                    # Another path already rotated the token in the DB:
                    await db.update_anthropic_oauth_tokens("k", "fresher-access", _future_ms(60), "fresher-refresh")
                    client = _FakeAsyncClient({"error": "invalid_grant"}, status_code=400)
                    token = await pool.ensure_valid_token(key, client)
                    self.assertEqual(token, pool._REFRESH_BLOCKED)   # re-read found newer token → don't deactivate
                    self.assertEqual(key.refresh_token, "fresher-refresh")
                    events = await db.list_anthropic_key_events("k")
                    deferred = [e for e in events if e["event_type"] == "refresh_deferred"]
                    self.assertTrue(deferred and deferred[-1]["decision"] == "reread_newer_token")
                finally:
                    await db.close()
        asyncio.run(run())

    def test_cancellation_still_persists(self) -> None:
        async def run() -> None:
            import contextlib
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    pool, key = await self._pool_with_key(db, 0)
                    real_client = _FakeAsyncClient(
                        {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800})

                    import smart_proxy.anthropic_proxy as ap
                    orig = ap._refresh_oauth_token
                    entered = asyncio.Event()
                    async def slow(*a, **k):
                        entered.set()                 # signal: the shielded refresh is in-flight
                        await asyncio.sleep(0.02)
                        return await orig(*a, **k)
                    with patch.object(ap, "_refresh_oauth_token", slow):
                        outer = asyncio.ensure_future(pool.ensure_valid_token(key, real_client))
                        await entered.wait()          # deterministic: refresh has started
                        outer.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await outer
                        # deterministic: await the shielded task(s) to completion (no fixed sleep)
                        pending = list(pool._refresh_tasks)
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                    row = await db.get_anthropic_key("k")
                    self.assertEqual(row["access_token"], "new-access")  # persisted despite cancel
                    # lock released — a subsequent call proceeds:
                    self.assertFalse(pool._refresh_lock.locked())
                finally:
                    await db.close()
        asyncio.run(run())


class RefreshHelperTests(unittest.TestCase):
    def test_is_auth_fatal_classification(self) -> None:
        import httpx
        from smart_proxy.anthropic_oauth import OAuthRefreshError
        from smart_proxy.anthropic_proxy import _is_auth_fatal
        self.assertTrue(_is_auth_fatal(OAuthRefreshError("x", status_code=400, error_code="invalid_grant")))
        self.assertTrue(_is_auth_fatal(OAuthRefreshError("x", status_code=401)))
        self.assertFalse(_is_auth_fatal(OAuthRefreshError("x", status_code=500)))
        self.assertFalse(_is_auth_fatal(RuntimeError("x")))
        self.assertFalse(_is_auth_fatal(httpx.ConnectError("x")))
        # A 2xx we could not read means the refresh token was already spent upstream:
        # fatal, because retrying can only produce a 400.
        self.assertTrue(_is_auth_fatal(OAuthRefreshError("x", status_code=200, token_consumed=True)))
        self.assertFalse(_is_auth_fatal(OAuthRefreshError("x", status_code=200)))

    def test_parse_retry_after(self) -> None:
        from types import SimpleNamespace
        from smart_proxy.anthropic_proxy import _parse_retry_after
        good = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "120"}))
        junk = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "soon"}))
        missing = SimpleNamespace(response=SimpleNamespace(headers={}))
        self.assertEqual(_parse_retry_after(good, 60), 120)
        self.assertEqual(_parse_retry_after(junk, 60), 60)
        self.assertEqual(_parse_retry_after(missing, 60), 60)

    def test_reload_prunes_refresh_backoff(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
                try:
                    await db.insert_anthropic_key(
                        id="live", key_type="oauth", access_token="a", refresh_token="r",
                        client_id="c", expires_at=9999999999999,
                        scopes='["user:inference"]', name="live")
                    pool = AnthropicKeyPool(db)
                    import time as _t
                    pool._refresh_backoff = {"live": _t.monotonic() + 999, "dead": _t.monotonic() + 999}
                    await pool.reload()
                    self.assertIn("live", pool._refresh_backoff)
                    self.assertNotIn("dead", pool._refresh_backoff)  # not a live key → pruned
                finally:
                    await db.close()
        asyncio.run(run())

class RefreshWithUnavailableDbTests(unittest.TestCase):
    """Never consume a single-use refresh token that cannot be persisted.

    Anthropic rotates the refresh token on every refresh and retires the old
    one. Refreshing while the database is down therefore trades a *temporary*
    outage for a *permanent* key loss: the rotated token exists only in memory,
    and the next restart or pool reload reads the dead one back, leaving manual
    re-authentication as the only recovery.

    Refusing costs far less — the key keeps serving on its current access token
    (~8h life, worst case ~4.5 min if the outage starts just before a refresh
    was due) and becomes usable again the moment the database returns.
    """

    def _key(self, *, expires_at: int) -> _AnthropicKey:
        return _AnthropicKey(
            key_id="key-db-down", key_type="oauth", status="active", api_key=None,
            access_token="current-access", refresh_token="current-refresh",
            client_id="client-id", expires_at=expires_at, scopes="user:inference",
        )

    def _pool(self, key: _AnthropicKey, *, available: bool):
        class _Db:
            def __init__(self) -> None:
                self.events: list[dict] = []

            def is_available(self) -> bool:
                return available

            async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
                self.events.append(dict(kwargs))
                return len(self.events)

            async def update_anthropic_oauth_tokens(self, *a, **k):  # noqa: ANN002, ANN003
                return None

            async def get_anthropic_key(self, key_id: str):
                return None

        pool = AnthropicKeyPool(_Db())
        pool._keys = [key]
        return pool

    def test_db_down_with_a_valid_token_serves_it_and_never_calls_token(self) -> None:
        async def run() -> None:
            # Inside the 5-min refresh buffer, so a refresh is due, but still
            # well above the 30s serve floor — the case the rule exists for.
            key = self._key(expires_at=_future_ms(2))
            pool = self._pool(key, available=False)
            client = _FakeAsyncClient({"access_token": "must-not-be-used",
                                       "refresh_token": "must-not-be-used",
                                       "expires_in": 28800})

            token = await pool.ensure_valid_token(
                key, client, audit_op_id="op", audit_source="proxy_request",
                audit_path="/v1/messages", audit_model="claude-opus-5",
                activate=False,
            )

            self.assertEqual(token, "current-access")
            self.assertEqual(len(client.calls), 0)          # /token untouched
            self.assertEqual(key.refresh_token, "current-refresh")
            # Refresh is parked so every request does not re-probe.
            self.assertIn("key-db-down", pool._refresh_backoff)

        asyncio.run(run())

    def test_db_down_with_an_expired_token_blocks_without_killing_the_key(self) -> None:
        """_REFRESH_BLOCKED, never None: None makes callers deactivate."""

        async def run() -> None:
            key = self._key(expires_at=_future_ms(-60))     # already expired
            pool = self._pool(key, available=False)
            client = _FakeAsyncClient({"access_token": "x", "refresh_token": "y",
                                       "expires_in": 28800})

            result = await pool.ensure_valid_token(
                key, client, audit_op_id="op", audit_source="proxy_request",
                audit_path="/v1/messages", audit_model="claude-opus-5",
                activate=False,
            )

            self.assertIs(result, pool._REFRESH_BLOCKED)
            self.assertIsNot(result, None)
            self.assertEqual(len(client.calls), 0)
            # The key must stay usable the moment the DB returns.
            self.assertNotIn("key-db-down", pool._refresh_dead)
            self.assertEqual(key.status, "active")

        asyncio.run(run())

    def test_the_rule_leaves_no_residue_once_the_db_returns(self) -> None:
        async def run() -> None:
            key = self._key(expires_at=_future_ms(-60))
            pool = self._pool(key, available=True)
            client = _FakeAsyncClient({"access_token": "new-access",
                                       "refresh_token": "new-refresh",
                                       "expires_in": 28800})

            token = await pool.ensure_valid_token(
                key, client, audit_op_id="op", audit_source="proxy_request",
                audit_path="/v1/messages", audit_model="claude-opus-5",
                activate=False,
            )

            self.assertEqual(token, "new-access")
            self.assertEqual(len(client.calls), 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
