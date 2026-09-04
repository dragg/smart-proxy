from __future__ import annotations

import asyncio
import json
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

from smart_proxy.claude_code_identity import ClaudeCodeVersion, DEFAULT_CLAUDE_CODE_VERSION
from smart_proxy.claude_code_identity import (
    DEFAULT_CLAUDE_CODE_VERSION,
    render_billing_header,
    render_cli_user_agent,
)
from smart_proxy.anthropic_proxy import (
    _AnthropicKey,
    _extract_sse_error_details,
    _merge_beta_flags,
    _proxy_handler,
)


class _FakeUpstreamResponse:
    def __init__(
        self,
        status_code: int,
        body: bytes,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": content_type}
        if headers:
            self.headers.update(headers)

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        return None


class _FakeStreamingUpstreamResponse:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        status_code: int = 200,
        content_type: str = "text/event-stream; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if headers:
            self.headers.update(headers)
        self._chunks = list(chunks)
        self.closed = False

    def aiter_bytes(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aclose(self) -> None:
        self.closed = True


class _FakeStreamResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self.prepared = False
        self.eof_written = False

    async def prepare(self, request) -> "_FakeStreamResponse":  # noqa: ANN001
        self.prepared = True
        return self

    async def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    async def write_eof(self) -> None:
        self.eof_written = True


class _FakeHttpClient:
    def __init__(self, response: _FakeUpstreamResponse) -> None:
        self._response = response
        self.last_build: dict | None = None

    def build_request(self, method: str, url: str, headers: dict, content: bytes | None):
        self.last_build = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "content": content,
        }
        return SimpleNamespace(method=method, url=url, headers=headers, content=content)

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        return self._response


class _QueuedFakeHttpClient:
    def __init__(self, responses: list[_FakeStreamingUpstreamResponse]) -> None:
        self._responses = list(responses)
        self.last_build: dict | None = None
        self.build_calls: list[dict[str, object]] = []

    def build_request(self, method: str, url: str, headers: dict, content: bytes | None):
        built = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "content": content,
        }
        self.last_build = built
        self.build_calls.append(built)
        return SimpleNamespace(method=method, url=url, headers=headers, content=content)

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        return self._responses.pop(0)


class _FakePool:
    _REFRESH_BLOCKED = object()

    def __init__(self, key: _AnthropicKey, token: str = "oauth-access-token") -> None:
        self._keys = [key]
        self._token = token
        self.deactivate_calls = 0

    def check_auth(self, token: str) -> bool:
        return bool(token)

    def pick(self, model: str | None = None, *, fallback_for: str | None = None) -> _AnthropicKey | None:
        del model
        return self._keys[0]

    async def ensure_valid_token(self, key: _AnthropicKey, client, **kwargs) -> str:  # noqa: ANN001, ANN003
        del kwargs
        return self._token

    async def deactivate(self, key: _AnthropicKey, **kwargs) -> None:  # noqa: ANN003
        del kwargs
        self.deactivate_calls += 1


class _RetryableStreamPool:
    _REFRESH_BLOCKED = object()

    def __init__(self, keys: list[_AnthropicKey], token: str = "oauth-access-token") -> None:
        self._keys = list(keys)
        self._token = token
        self._cooldowns: set[str] = set()
        self.cooldown_calls: list[str] = []

    def check_auth(self, token: str) -> bool:
        return bool(token)

    def pick(self, model: str | None = None, *, fallback_for: str | None = None) -> _AnthropicKey | None:
        del model
        for key in self._keys:
            if key.key_id not in self._cooldowns:
                return key
        return None

    async def ensure_valid_token(self, key: _AnthropicKey, client, **kwargs) -> str:  # noqa: ANN001, ANN003
        del kwargs
        return self._token

    def cooldown(
        self, key: _AnthropicKey, seconds: int | None = None, *, model: str | None = None
    ) -> None:
        del seconds, model
        self._cooldowns.add(key.key_id)
        self.cooldown_calls.append(key.key_id)


class _Terminal429Pool(_FakePool):
    def __init__(self, key: _AnthropicKey, token: str = "oauth-access-token") -> None:
        super().__init__(key, token=token)
        self._cooled_down = False

    def pick(self, model: str | None = None, *, fallback_for: str | None = None) -> _AnthropicKey | None:
        if self._cooled_down:
            return None
        return super().pick(model)

    def cooldown(
        self, key: _AnthropicKey, seconds: int | None = None, *, model: str | None = None
    ) -> None:
        self._cooled_down = True


class _UnavailablePool:
    def __init__(self, retry_after: int = 17) -> None:
        self._keys = [object()]
        self._retry_after = retry_after

    def check_auth(self, token: str) -> bool:
        return bool(token)

    def pick(self, model: str | None = None, *, fallback_for: str | None = None):  # noqa: ANN001
        del model
        return None

    def next_available_in(self, model: str | None = None, *, fallback_for: str | None = None) -> int:
        del model
        return self._retry_after

    def has_scoped_fallback(self, proxy_key: str) -> bool:
        del proxy_key
        return False


class _FakeDb:
    async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
        del kwargs
        return 1

    async def record_rate_limit(self, **kwargs) -> None:  # noqa: ANN003
        return None


class _RecordingDb(_FakeDb):
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
        self.events.append(dict(kwargs))
        return len(self.events)


class _TieredPool:
    """Subscription key until it is cooled, then the scoped paid fallback."""

    _REFRESH_BLOCKED = object()

    def __init__(self, oauth_key: _AnthropicKey, fallback_key: _AnthropicKey) -> None:
        self._keys = [oauth_key, fallback_key]
        self._cooled: set[str] = set()
        self.fallback_serves: list[tuple[str, str]] = []

    def check_auth(self, token: str) -> bool:
        return bool(token)

    def pick(self, model: str | None = None, *, fallback_for: str | None = None):
        del model
        if self._keys[0].key_id not in self._cooled:
            return self._keys[0]
        return self._keys[1] if fallback_for else None

    def cooldown(self, key: _AnthropicKey, seconds: int | None = None, *, model: str | None = None) -> None:
        del seconds, model
        self._cooled.add(key.key_id)

    def next_available_in(self, model: str | None = None, *, fallback_for: str | None = None) -> int:
        del model, fallback_for
        return 0

    def has_scoped_fallback(self, proxy_key: str) -> bool:
        del proxy_key
        return True

    async def ensure_valid_token(self, key: _AnthropicKey, client, **kwargs) -> str:  # noqa: ANN001, ANN003
        del client, kwargs
        return key.api_key or "oauth-access-token"

    async def deactivate(self, key: _AnthropicKey, **kwargs) -> None:  # noqa: ANN003
        del key, kwargs

    async def mark_low_balance(self, key: _AnthropicKey, **kwargs) -> None:  # noqa: ANN003
        del key, kwargs

    async def note_fallback_serve(self, key: _AnthropicKey, proxy_key: str, **kwargs) -> None:  # noqa: ANN003
        del kwargs
        self.fallback_serves.append((key.key_id, proxy_key))


class _StubLimiter:
    """Just enough KeyLimiter surface for the proxy handler."""

    def __init__(self, limits: dict[str, dict] | None = None) -> None:
        self._limits = limits or {}

    def check(self, proxy_key: str):
        del proxy_key
        return None

    def limits_for(self, proxy_key: str) -> dict:
        return dict(self._limits.get(proxy_key) or {})


class _FakeRequest:
    def __init__(self, app: dict, headers: dict[str, str], body: bytes, path: str = "/v1/messages") -> None:
        self.app = app
        self.headers = headers
        self.method = "POST"
        self.path = path
        self.query_string = ""
        self.rel_url = SimpleNamespace(query={})
        self._body = body

    async def read(self) -> bytes:
        return self._body


class AnthropicProxyOAuthMessagesTests(unittest.TestCase):
    def _oauth_key(self) -> _AnthropicKey:
        return _AnthropicKey(
            key_id="key-123",
            key_type="oauth",
            status="active",
            api_key=None,
            access_token="old-access",
            refresh_token="refresh",
            client_id="client-id",
            expires_at=9999999999999,
            scopes="user:inference",
        )

    def _oauth_key_named(self, key_id: str) -> _AnthropicKey:
        key = self._oauth_key()
        key.key_id = key_id
        return key

    def _api_key_named(self, key_id: str) -> _AnthropicKey:
        return _AnthropicKey(
            key_id=key_id,
            key_type="api_key",
            status="active",
            api_key="sk-ant-test",
            access_token=None,
            refresh_token=None,
            client_id="client-id",
            expires_at=None,
        )

    def _app(
        self,
        client: _FakeHttpClient,
        pool: _FakePool,
        *,
        disable_1m_context: bool = False,
        strip_system_phrase: str = "",
        claude_like: bool = False,
        db: object | None = None,
        key_limiter: object | None = None,
    ) -> dict:
        return {
            "anthropic_pool": pool,
            "http_client": client,
            "usage_tracker": None,
            "disable_1m_context": disable_1m_context,
            "strip_system_phrase": strip_system_phrase,
            "claude_like": claude_like,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "db": db if db is not None else _FakeDb(),
            "key_limiter": key_limiter,
        }

    def test_merge_beta_flags_keeps_existing_and_adds_required(self) -> None:
        existing = "claude-code-20250219,interleaved-thinking-2025-05-14"
        required = "oauth-2025-04-20,interleaved-thinking-2025-05-14"
        merged = _merge_beta_flags(existing, required)
        self.assertEqual(
            merged,
            "claude-code-20250219,interleaved-thinking-2025-05-14,oauth-2025-04-20",
        )

    def test_messages_request_always_contains_required_oauth_headers(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool),
                headers={
                    "Authorization": "Bearer sp-test",
                    "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14",
                    "User-Agent": "claude-cli/2.1.91 (external, cli)",
                },
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            self.assertIsNotNone(client.last_build)
            assert client.last_build is not None
            sent_headers = client.last_build["headers"]
            self.assertIn("oauth-2025-04-20", sent_headers["anthropic-beta"])
            self.assertIn("claude-code-20250219", sent_headers["anthropic-beta"])
            self.assertEqual(sent_headers["anthropic-version"], "2023-06-01")

        asyncio.run(run())

    def test_oauth_unsupported_401_does_not_deactivate_key(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool),
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            self.assertEqual(pool.deactivate_calls, 0)

        asyncio.run(run())

    def test_context_1m_beta_is_forwarded_unchanged_when_flag_disabled(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, disable_1m_context=False),
                headers={
                    "Authorization": "Bearer sp-test",
                    "anthropic-beta": "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14",
                },
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            sent_header = client.last_build["headers"]["anthropic-beta"]
            self.assertIn("context-1m-2025-08-07", sent_header)
            self.assertIn("oauth-2025-04-20", sent_header)

        asyncio.run(run())

    def test_context_1m_beta_is_stripped_when_flag_enabled(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, disable_1m_context=True),
                headers={
                    "Authorization": "Bearer sp-test",
                    "anthropic-beta": "claude-code-20250219,context-1m-2025-08-07,interleaved-thinking-2025-05-14",
                },
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            sent_header = client.last_build["headers"]["anthropic-beta"]
            self.assertNotIn("context-1m-2025-08-07", sent_header)
            self.assertEqual(
                sent_header,
                "claude-code-20250219,interleaved-thinking-2025-05-14,oauth-2025-04-20,context-management-2025-06-27,prompt-caching-scope-2026-01-05",
            )

        asyncio.run(run())

    def test_claude_like_rewrites_non_claude_headers_and_adds_missing_cli_fields(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, claude_like=True),
                headers={
                    "Authorization": "Bearer sp-test",
                    "User-Agent": "Anthropic/JS 0.73.0",
                    "anthropic-beta": "fine-grained-tool-streaming-2025-05-14",
                    "X-Stainless-Package-Version": "0.73.0",
                    "X-Stainless-Runtime-Version": "v22.19.0",
                    "X-Stainless-Timeout": "600",
                    "x-stainless-helper-method": "stream",
                    "Accept-Language": "*",
                    "sec-fetch-mode": "cors",
                    "Accept-Encoding": "gzip, deflate",
                },
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            sent_headers = client.last_build["headers"]
            self.assertEqual(
                sent_headers["User-Agent"],
                render_cli_user_agent(DEFAULT_CLAUDE_CODE_VERSION),
            )
            self.assertEqual(sent_headers["x-app"], "cli")
            UUID(sent_headers["X-Claude-Code-Session-Id"])
            self.assertEqual(sent_headers["X-Stainless-Package-Version"], "0.112.1")
            self.assertEqual(sent_headers["X-Stainless-Runtime-Version"], "v26.3.0")
            self.assertEqual(sent_headers["X-Stainless-Timeout"], "600")
            self.assertEqual(sent_headers["Accept-Encoding"], "gzip, deflate, br, zstd")
            self.assertEqual(
                sent_headers["anthropic-beta"],
                "interleaved-thinking-2025-05-14,redact-thinking-2026-02-12,"
                "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
                "claude-code-20250219,oauth-2025-04-20",
            )
            self.assertNotIn("x-stainless-helper-method", sent_headers)
            self.assertNotIn("Accept-Language", sent_headers)
            self.assertNotIn("sec-fetch-mode", sent_headers)

        asyncio.run(run())

    def test_claude_like_preserves_existing_lowercase_claude_headers_without_duplicates(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, claude_like=True),
                headers={
                    "Authorization": "Bearer sp-test",
                    "user-agent": "claude-cli/2.1.91 (external, cli)",
                    "x-claude-code-session-id": "existing-session-id",
                    "x-app": "cli",
                },
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            sent_headers = client.last_build["headers"]
            self.assertEqual(sent_headers["user-agent"], "claude-cli/2.1.91 (external, cli)")
            self.assertEqual(sent_headers["x-claude-code-session-id"], "existing-session-id")
            self.assertNotIn("User-Agent", sent_headers)
            self.assertNotIn("X-Claude-Code-Session-Id", sent_headers)

        asyncio.run(run())

    def test_claude_like_disabled_preserves_non_claude_user_agent_and_missing_cli_fields(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, claude_like=False),
                headers={
                    "Authorization": "Bearer sp-test",
                    "User-Agent": "Anthropic/JS 0.73.0",
                },
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            sent_headers = client.last_build["headers"]
            self.assertEqual(sent_headers["User-Agent"], "Anthropic/JS 0.73.0")
            self.assertEqual(sent_headers["x-app"], "cli")
            self.assertNotIn("X-Claude-Code-Session-Id", sent_headers)

        asyncio.run(run())

    def test_synthetic_cooldown_429_sets_should_retry_false(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            client = _FakeHttpClient(_FakeUpstreamResponse(200, b"{}"))
            pool = _UnavailablePool(retry_after=17)
            req = _FakeRequest(
                app=self._app(client, pool),  # type: ignore[arg-type]
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 429)
            # retry-after header stays raw seconds (HTTP spec); the human message is friendly.
            self.assertEqual(resp.headers.get("retry-after"), "17")
            self.assertEqual(resp.headers.get("x-should-retry"), "false")
            payload = json.loads(resp.body)
            self.assertEqual(
                payload["error"]["message"],
                "You've reached your claude-haiku-4-5-20251001 limit. "
                "Retry in 17s or switch models with /model.",
            )

        asyncio.run(run())

    def test_terminal_upstream_429_sets_should_retry_false_when_no_next_key(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeUpstreamResponse(
                429,
                b'{"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your account\'s rate limit. Please try again later."},"request_id":"req_123"}',
                headers={
                    "retry-after": "17",
                    "request-id": "req_123",
                },
            )
            client = _FakeHttpClient(upstream)
            pool = _Terminal429Pool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool),  # type: ignore[arg-type]
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 429)
            self.assertEqual(resp.headers.get("retry-after"), "17")
            self.assertEqual(resp.headers.get("x-should-retry"), "false")
            self.assertIn(b"would exceed your account's rate limit", resp.body)

        asyncio.run(run())

    def test_system_text_phrase_is_removed_before_oauth_injection(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "ping"}],
                    "system": [
                        {
                            "type": "text",
                            "text": "prefix Claude Code suffix",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, strip_system_phrase="Claude Code"),
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            forwarded = json.loads(client.last_build["content"])
            self.assertEqual(
                forwarded["system"][0]["text"],
                render_billing_header(f"{DEFAULT_CLAUDE_CODE_VERSION}.a35"),
            )
            self.assertEqual(forwarded["system"][1]["text"], "prefix  suffix")
            self.assertEqual(
                forwarded["system"][1]["cache_control"],
                {"type": "ephemeral"},
            )

        asyncio.run(run())

    def test_system_text_filter_is_noop_when_phrase_is_empty(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "ping"}],
                    "system": [
                        {
                            "type": "text",
                            "text": "prefix Claude Code suffix",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, strip_system_phrase=""),
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            forwarded = json.loads(client.last_build["content"])
            self.assertEqual(
                forwarded["system"][1]["text"],
                "prefix Claude Code suffix",
            )
            self.assertEqual(
                forwarded["system"][1]["cache_control"],
                {"type": "ephemeral"},
            )

        asyncio.run(run())

    def test_system_text_filter_is_noop_when_phrase_is_absent(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "ping"}],
                    "system": [
                        {
                            "type": "text",
                            "text": "prefix Claude Code suffix",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"OAuth authentication is currently not supported."}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool, strip_system_phrase="Not Present"),
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 401)
            assert client.last_build is not None
            forwarded = json.loads(client.last_build["content"])
            self.assertEqual(
                forwarded["system"][1]["text"],
                "prefix Claude Code suffix",
            )
            self.assertEqual(
                forwarded["system"][1]["cache_control"],
                {"type": "ephemeral"},
            )

        asyncio.run(run())

    def test_absent_stream_is_not_forced_to_true_on_oauth(self) -> None:
        """A client that omits ``stream`` must not be upgraded to streaming.

        Forcing ``stream:true`` upstream made the proxy answer non-streaming
        clients (e.g. the Anthropic SDK's ``messages.create``) with SSE, which
        they parse as a string and crash on ``message.content``.
        """

        async def run() -> None:
            body = json.dumps(
                {
                    "model": "claude-haiku-4-5-20251001",
                    "messages": [{"role": "user", "content": "ping"}],
                }
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"x"}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool),
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            await _proxy_handler(req)

            assert client.last_build is not None
            forwarded = json.loads(client.last_build["content"])
            self.assertNotEqual(
                forwarded.get("stream"),
                True,
                "proxy must not force stream:true when the client omitted it",
            )

        asyncio.run(run())

    def test_explicit_stream_true_is_preserved_on_oauth(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {
                    "model": "claude-haiku-4-5-20251001",
                    "stream": True,
                    "messages": [{"role": "user", "content": "ping"}],
                }
            ).encode()
            upstream = _FakeUpstreamResponse(
                401,
                b'{"type":"error","error":{"type":"authentication_error","message":"x"}}',
            )
            client = _FakeHttpClient(upstream)
            pool = _FakePool(self._oauth_key())
            req = _FakeRequest(
                app=self._app(client, pool),
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            await _proxy_handler(req)

            assert client.last_build is not None
            forwarded = json.loads(client.last_build["content"])
            self.assertEqual(forwarded.get("stream"), True)

        asyncio.run(run())

    def test_truncated_stream_retries_without_cooling_down_oauth_key(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "stream": True, "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            truncated = _FakeStreamingUpstreamResponse(
                [
                    b"event: message_start\n",
                    (
                        b'data: {"type":"message_start","message":{"id":"msg_bad","type":"message",'
                        b'"role":"assistant","content":[],"usage":{"input_tokens":10}}}\n\n'
                    ),
                ]
            )
            complete = _FakeStreamingUpstreamResponse(
                [
                    b"event: message_start\n",
                    (
                        b'data: {"type":"message_start","message":{"id":"msg_ok","type":"message",'
                        b'"role":"assistant","content":[],"usage":{"input_tokens":10}}}\n\n'
                    ),
                    b"event: content_block_start\n",
                    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                    b"event: content_block_delta\n",
                    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n',
                    b"event: content_block_stop\n",
                    b'data: {"type":"content_block_stop","index":0}\n\n',
                    b"event: message_stop\n",
                    b'data: {"type":"message_stop"}\n\n',
                ]
            )
            client = _QueuedFakeHttpClient([truncated, complete])
            pool = _RetryableStreamPool(
                [self._oauth_key_named("key-bad"), self._oauth_key_named("key-good")]
            )
            req = _FakeRequest(
                app=self._app(client, pool),  # type: ignore[arg-type]
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
                resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.prepared)
            self.assertTrue(resp.eof_written)
            # OAuth keys are not cooled down: the single subscription key must
            # not be bricked, so it is simply retried.
            self.assertEqual(pool.cooldown_calls, [])
            self.assertEqual(len(client.build_calls), 2)
            self.assertEqual(
                b"".join(resp.chunks),
                b"event: message_start\n"
                b'data: {"type":"message_start","message":{"id":"msg_ok","type":"message","role":"assistant","content":[],"usage":{"input_tokens":10}}}\n\n'
                b"event: content_block_start\n"
                b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                b"event: content_block_delta\n"
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n'
                b"event: content_block_stop\n"
                b'data: {"type":"content_block_stop","index":0}\n\n'
                b"event: message_stop\n"
                b'data: {"type":"message_stop"}\n\n',
            )

        asyncio.run(run())

    def _fallback_key(self) -> _AnthropicKey:
        key = self._api_key_named("paid-1")
        key.role = "fallback"
        key.api_key = "sk-ant-paid"
        key.allowed_proxy_keys = frozenset({"sp-test"})
        return key

    def test_escalation_to_paid_key_does_not_reuse_the_oauth_request(self) -> None:
        """A 429 on the subscription key escalates to the paid fallback — and the
        paid request must not carry the Claude Code billing block or ?beta=true
        that were injected for the previous, oauth attempt."""
        async def run() -> None:
            body = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "user", "content": "ping"}],
                "system": "You are a translation service.",
            }).encode()
            rate_limited = _FakeUpstreamResponse(
                429, b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}',
                headers={"retry-after": "600"},
            )
            ok = _FakeStreamingUpstreamResponse(
                [
                    b"event: message_stop\n",
                    b'data: {"type":"message_stop"}\n\n',
                ]
            )
            client = _QueuedFakeHttpClient([rate_limited, ok])
            pool = _TieredPool(self._oauth_key(), self._fallback_key())
            req = _FakeRequest(
                app=self._app(client, pool,  # type: ignore[arg-type]
                              key_limiter=_StubLimiter({"sp-test": {"daily_usd": 50.0}})),
                headers={"Authorization": "Bearer sp-test",
                         "User-Agent": "anthropic-sdk-python/1.2"},
                body=body,
            )

            with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
                resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 200)
            self.assertEqual(len(client.build_calls), 2)
            first, second = client.build_calls
            self.assertIn("x-anthropic-billing-header", first["content"].decode())
            self.assertIn("beta=true", str(first["url"]))
            # The paid attempt is built from the client's original request.
            self.assertNotIn("x-anthropic-billing-header", second["content"].decode())
            self.assertNotIn("beta=true", str(second["url"]))
            self.assertEqual(second["headers"].get("x-api-key"), "sk-ant-paid")
            self.assertEqual(json.loads(second["content"])["system"],
                             "You are a translation service.")
            self.assertEqual(pool.fallback_serves, [("paid-1", "sp-test")])

        asyncio.run(run())

    def test_claude_code_request_never_escalates_to_the_paid_key(self) -> None:
        async def run() -> None:
            body = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "messages": [{"role": "user", "content": "ping"}],
            }).encode()
            rate_limited = _FakeUpstreamResponse(
                429, b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}',
                headers={"retry-after": "600"},
            )
            client = _QueuedFakeHttpClient([rate_limited])
            pool = _TieredPool(self._oauth_key(), self._fallback_key())
            req = _FakeRequest(
                app=self._app(client, pool,  # type: ignore[arg-type]
                              key_limiter=_StubLimiter({"sp-test": {"daily_usd": 50.0}})),
                headers={"Authorization": "Bearer sp-test",
                         "User-Agent": "claude-cli/2.1.92 (external, cli)"},
                body=body,
            )

            resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 429)
            self.assertEqual(len(client.build_calls), 1)
            self.assertEqual(pool.fallback_serves, [])

        asyncio.run(run())

    def test_truncated_stream_cools_down_and_rotates_api_key(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "stream": True, "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            truncated = _FakeStreamingUpstreamResponse(
                [
                    b"event: message_start\n",
                    (
                        b'data: {"type":"message_start","message":{"id":"msg_bad","type":"message",'
                        b'"role":"assistant","content":[],"usage":{"input_tokens":10}}}\n\n'
                    ),
                ]
            )
            complete = _FakeStreamingUpstreamResponse(
                [
                    b"event: content_block_delta\n",
                    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n',
                    b"event: message_stop\n",
                    b'data: {"type":"message_stop"}\n\n',
                ]
            )
            client = _QueuedFakeHttpClient([truncated, complete])
            pool = _RetryableStreamPool(
                [self._api_key_named("api-bad"), self._api_key_named("api-good")]
            )
            req = _FakeRequest(
                app=self._app(client, pool),  # type: ignore[arg-type]
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
                resp = await _proxy_handler(req)

            self.assertEqual(resp.status, 200)
            # API keys are still cooled down so the pool rotates to the next key.
            self.assertEqual(pool.cooldown_calls, ["api-bad"])
            self.assertEqual(len(client.build_calls), 2)

        asyncio.run(run())

    def test_sse_error_before_commit_overloaded_surfaces_529(self) -> None:
        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "stream": True, "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            errored = _FakeStreamingUpstreamResponse(
                [
                    b"event: error\n",
                    b'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n',
                ]
            )
            # A second upstream is queued to prove we do NOT burn a local retry
            # on overload: it must stay untouched.
            complete = _FakeStreamingUpstreamResponse(
                [
                    b"event: content_block_delta\n",
                    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}\n\n',
                    b"event: message_stop\n",
                    b'data: {"type":"message_stop"}\n\n',
                ]
            )
            client = _QueuedFakeHttpClient([errored, complete])
            pool = _RetryableStreamPool([self._oauth_key_named("key-1")])
            db = _RecordingDb()
            req = _FakeRequest(
                app=self._app(client, pool, db=db),  # type: ignore[arg-type]
                headers={"Authorization": "Bearer sp-test"},
                body=body,
            )

            with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
                resp = await _proxy_handler(req)

            # Overload is surfaced as a clean 529 with the original error body so
            # Claude Code's own backoff retry engine takes over.
            self.assertEqual(resp.status, 529)
            payload = json.loads(resp.body)
            self.assertEqual(payload["error"]["type"], "overloaded_error")
            self.assertEqual(payload["error"]["message"], "Overloaded")
            # No local retry was spent and the OAuth key was not cooled down.
            self.assertEqual(len(client.build_calls), 1)
            self.assertEqual(pool.cooldown_calls, [])
            sse_events = [
                e for e in db.events if e["event_type"] == "sse_error_before_commit"
            ]
            self.assertEqual(len(sse_events), 1)
            self.assertEqual(sse_events[0]["error_type"], "overloaded_error")
            self.assertEqual(sse_events[0]["error_message"], "Overloaded")
            self.assertEqual(sse_events[0]["decision"], "surface_529")
            self.assertEqual(sse_events[0]["http_status"], 200)

        asyncio.run(run())

    def test_extract_sse_error_details_parses_event_payload(self) -> None:
        buf = (
            b"event: error\n"
            b'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
        )
        self.assertEqual(
            _extract_sse_error_details(buf), ("overloaded_error", "Overloaded")
        )

    def test_extract_sse_error_details_falls_back_to_preview(self) -> None:
        error_type, message = _extract_sse_error_details(b"event: ping\ndata: garbage\n\n")
        self.assertEqual(error_type, "")
        self.assertIn("garbage", message)

    def _run_usage_attribution(self, *, headers: dict[str, str]) -> dict:
        """Drive _proxy_handler on a 200 JSON response, capture the record() call."""
        captured: dict = {}

        class _CapturingTracker:
            def record(self, *args, **kwargs):  # noqa: ANN002, ANN003
                captured["args"] = args
                captured["kwargs"] = kwargs

        async def run() -> None:
            body = json.dumps(
                {"model": "claude-haiku-4-5-20251001", "messages": [{"role": "user", "content": "ping"}]}
            ).encode()
            upstream = _FakeStreamingUpstreamResponse(
                [b'{"usage":{"input_tokens":10,"output_tokens":5}}'],
                status_code=200,
                content_type="application/json",
            )
            client = _QueuedFakeHttpClient([upstream])
            pool = _FakePool(self._oauth_key())
            app = self._app(client, pool)
            app["usage_tracker"] = _CapturingTracker()
            req = _FakeRequest(
                app=app,  # type: ignore[arg-type]
                headers={"Authorization": "Bearer sp-test", **headers},
                body=body,
            )
            with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
                resp = await _proxy_handler(req)
            self.assertEqual(resp.status, 200)

        asyncio.run(run())
        return captured

    def test_usage_tagged_via_openai_compat_when_loopback_header_present(self) -> None:
        captured = self._run_usage_attribution(headers={"x-smart-proxy-openai-compat": "1"})
        self.assertTrue(captured["kwargs"].get("via_openai_compat"))

    def test_usage_not_tagged_for_native_request(self) -> None:
        captured = self._run_usage_attribution(headers={})
        self.assertFalse(captured["kwargs"].get("via_openai_compat"))


if __name__ == "__main__":
    unittest.main()
