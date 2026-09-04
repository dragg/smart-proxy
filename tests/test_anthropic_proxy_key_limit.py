# tests/test_anthropic_proxy_key_limit.py
from __future__ import annotations
import json, sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aiohttp.test_utils import make_mocked_request

from smart_proxy import anthropic_proxy
from smart_proxy.claude_code_identity import ClaudeCodeVersion, DEFAULT_CLAUDE_CODE_VERSION
from smart_proxy.key_limits import LimitBlock


class BillablePathTests(unittest.TestCase):
    def test_messages_post_is_billable(self):
        self.assertTrue(anthropic_proxy._is_billable_path("POST", "/v1/messages"))

    def test_count_tokens_is_not_billable(self):
        self.assertFalse(
            anthropic_proxy._is_billable_path("POST", "/v1/messages/count_tokens"))

    def test_non_post_and_other_paths_are_not_billable(self):
        self.assertFalse(anthropic_proxy._is_billable_path("GET", "/v1/messages"))
        self.assertFalse(anthropic_proxy._is_billable_path("POST", "/v1/models"))


class LimitGateTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, block):
        pool = MagicMock()
        pool.check_auth.return_value = True
        pool.pick.side_effect = AssertionError("pool must not be consulted when blocked")
        limiter = MagicMock()
        limiter.check.return_value = block
        return {
            "anthropic_pool": pool,
            "http_client": MagicMock(),
            "key_limiter": limiter,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
        }

    async def test_over_limit_returns_429_with_smartproxy_message(self):
        block = LimitBlock(kind="daily_usd", label="24h", retry_after=3554,
                           limit_usd=25.0, spent_usd=25.4)
        req = make_mocked_request(
            "POST", "/v1/messages", app=self._app(block),
            headers={"Authorization": "Bearer sp-a"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        resp = await anthropic_proxy._proxy_handler(req)
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers["retry-after"], "3554")
        self.assertEqual(resp.headers["x-should-retry"], "false")
        payload = json.loads(resp.body)
        self.assertEqual(payload["error"]["type"], "rate_limit_error")
        self.assertEqual(
            payload["error"]["message"],
            "SmartProxy: you have reached your 24h limit. Retry in 59m 14s",
        )

    async def test_count_tokens_is_not_gated(self):
        block = LimitBlock(kind="daily_usd", label="24h", retry_after=10,
                           limit_usd=1.0, spent_usd=2.0)
        app = self._app(block)
        app["anthropic_pool"].pick.side_effect = None
        app["anthropic_pool"].pick.return_value = None
        app["anthropic_pool"].next_available_in.return_value = 0
        app["anthropic_pool"]._keys = []
        req = make_mocked_request(
            "POST", "/v1/messages/count_tokens", app=app,
            headers={"Authorization": "Bearer sp-a"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        resp = await anthropic_proxy._proxy_handler(req)
        # Not a 429 from the limiter: it fell through to the normal no-keys path.
        self.assertEqual(resp.status, 503)

    async def test_under_limit_falls_through_to_the_pool(self):
        app = self._app(None)
        app["anthropic_pool"].pick.side_effect = None
        app["anthropic_pool"].pick.return_value = None
        app["anthropic_pool"].next_available_in.return_value = 0
        app["anthropic_pool"]._keys = []
        req = make_mocked_request(
            "POST", "/v1/messages", app=app, headers={"Authorization": "Bearer sp-a"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        resp = await anthropic_proxy._proxy_handler(req)
        self.assertEqual(resp.status, 503)

    async def test_passthrough_token_is_checked_under_its_own_bucket(self):
        app = self._app(None)
        app["anthropic_pool"].pick.side_effect = None
        app["anthropic_pool"].pick.return_value = None
        app["anthropic_pool"].next_available_in.return_value = 0
        app["anthropic_pool"]._keys = []
        req = make_mocked_request(
            "POST", "/v1/messages", app=app,
            headers={"Authorization": "Bearer sk-ant-xyz"},
        )
        req.read = _returns(b'{"model":"claude-opus-4-5"}')
        await anthropic_proxy._proxy_handler(req)
        app["key_limiter"].check.assert_called_once_with("claude-passthrough")


# -- fakes for driving _proxy_handler through a full successful response ---
# Styled after the fakes in test_anthropic_proxy_oauth_messages.py (not
# imported from there — that file's fakes are local to its own tests).

class _FakeUpstreamJsonResponse:
    """A non-streaming (plain JSON) upstream response carrying usage."""

    def __init__(self, body: bytes) -> None:
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self._body = body

    def aiter_bytes(self):
        async def _gen():
            yield self._body
        return _gen()

    async def aread(self) -> bytes:
        return self._body

    async def aclose(self) -> None:
        return None


class _FakeStreamResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []

    async def prepare(self, request):  # noqa: ANN001
        return self

    async def write(self, chunk: bytes) -> None:
        self.chunks.append(chunk)

    async def write_eof(self) -> None:
        return None


class _FakeHttpClient:
    def __init__(self, response) -> None:
        self._response = response

    def build_request(self, method: str, url: str, headers: dict, content: bytes | None):
        return SimpleNamespace(method=method, url=url, headers=headers, content=content)

    async def send(self, req, stream: bool = True):  # noqa: ANN001
        return self._response


class _FakePool:
    _REFRESH_BLOCKED = object()

    def __init__(self, key) -> None:
        self._keys = [key]

    def check_auth(self, token: str) -> bool:
        return bool(token)

    def pick(self, model: str | None = None, *, fallback_for: str | None = None):
        return self._keys[0]

    async def ensure_valid_token(self, key, client, **kwargs):  # noqa: ANN001, ANN003
        return "sk-ant-upstream-token"

    async def deactivate(self, key, **kwargs) -> None:  # noqa: ANN003
        return None


class _FakeDb:
    async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
        return 1

    async def record_rate_limit(self, **kwargs) -> None:  # noqa: ANN003
        return None


class _FakeRequest:
    def __init__(self, app: dict, headers: dict[str, str], body: bytes) -> None:
        self.app = app
        self.headers = headers
        self.method = "POST"
        self.path = "/v1/messages"
        self.query_string = ""
        self.rel_url = SimpleNamespace(query={})
        self._body = body

    async def read(self) -> bytes:
        return self._body


class SpendAddedOnSuccessTests(unittest.IsolatedAsyncioTestCase):
    """_proxy_handler must feed limiter.add() the same key limiter.check() saw
    and the same model tracker.record() got. Drives a real (non-streaming)
    success response through the handler rather than mocking the gate alone,
    since that's the only way to exercise the ``if usage: ... limiter.add(...)``
    block below the gate."""

    def _key(self):
        return anthropic_proxy._AnthropicKey(
            key_id="key-abc",
            key_type="api_key",
            status="active",
            api_key="sk-ant-test",
            access_token=None,
            refresh_token=None,
            client_id="client-id",
            expires_at=None,
        )

    async def test_limiter_add_matches_check_key_and_record_model(self):
        model = "claude-haiku-4-5-20251001"
        body = json.dumps(
            {"model": model, "messages": [{"role": "user", "content": "ping"}]}
        ).encode()
        upstream = _FakeUpstreamJsonResponse(
            b'{"usage":{"input_tokens":10,"output_tokens":5}}'
        )
        client = _FakeHttpClient(upstream)
        pool = _FakePool(self._key())
        tracker = MagicMock()
        limiter = MagicMock()
        limiter.check.return_value = None
        app = {
            "anthropic_pool": pool,
            "claude_code_version": ClaudeCodeVersion(DEFAULT_CLAUDE_CODE_VERSION),
            "http_client": client,
            "usage_tracker": tracker,
            "key_limiter": limiter,
            "db": _FakeDb(),
        }
        req = _FakeRequest(
            app=app, headers={"Authorization": "Bearer sp-test"}, body=body,
        )
        with patch("smart_proxy.anthropic_proxy.web.StreamResponse", _FakeStreamResponse):
            resp = await anthropic_proxy._proxy_handler(req)
        self.assertEqual(resp.status, 200)

        limiter.check.assert_called_once_with("sp-test")
        tracker.record.assert_called_once()
        limiter.add.assert_called_once()

        checked_key = limiter.check.call_args.args[0]
        recorded_model = tracker.record.call_args.args[3]
        added_key, added_model, added_usage = limiter.add.call_args.args
        self.assertEqual(added_key, checked_key)
        self.assertEqual(added_model, recorded_model)
        self.assertEqual(tuple(added_usage)[:2], (10, 5))


class ResyncLimiterTests(unittest.IsolatedAsyncioTestCase):
    """Seeding replaces the live counter with the hourly-bucket totals, so a
    reload must flush buffered usage first or it forgives unflushed spend."""

    async def test_resync_flushes_usage_before_reloading_the_limiter(self):
        calls = []
        tracker = MagicMock()
        db = MagicMock()
        limiter = MagicMock()
        limiter.load = _record_async(calls, "load")
        app = {"key_limiter": limiter, "usage_tracker": tracker, "db": db}
        with patch.object(
            anthropic_proxy, "_flush_usage", _record_async(calls, "flush")
        ):
            await anthropic_proxy._resync_limiter(app)
        self.assertEqual(calls, ["flush", "load"])

    async def test_resync_is_a_noop_without_a_limiter(self):
        calls = []
        app = {"usage_tracker": MagicMock(), "db": MagicMock()}
        with patch.object(
            anthropic_proxy, "_flush_usage", _record_async(calls, "flush")
        ):
            await anthropic_proxy._resync_limiter(app)
        self.assertEqual(calls, [])

    async def test_resync_still_loads_when_there_is_nothing_to_flush(self):
        calls = []
        limiter = MagicMock()
        limiter.load = _record_async(calls, "load")
        await anthropic_proxy._resync_limiter({"key_limiter": limiter})
        self.assertEqual(calls, ["load"])


def _record_async(calls, name):
    async def _inner(*_args, **_kwargs):
        calls.append(name)
    return _inner


def _returns(value):
    async def _inner():
        return value
    return _inner


if __name__ == "__main__":
    unittest.main()
