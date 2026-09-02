# tests/test_proxy_failure_alerts.py
"""Alerting on unexpected in-process failures.

Until now every Telegram alert was a key-lifecycle event fired from inside the
OAuth refresh path. When the proxy stopped serving on 2026-08-21 at 06:37 UTC
nothing was sent: `Usage flush error` had been logged at WARNING every 60s for
hours, and the failures on the request path raised into aiohttp, which turned
them into a 500 and told nobody.

These tests pin the three pieces that close that gap:

* `AlertThrottle` -- one message per failure signature per window, with the
  suppressed repeat count carried on the next send, so a bug that fires on every
  request produces one Telegram message rather than thousands.
* `_failure_alert_middleware` -- alerts on any exception escaping a handler,
  while staying silent for the two things that are not failures: a client
  hanging up mid-stream (`ClientConnectionResetError`, routine here because
  Claude Code's stream watchdog aborts long Opus streams) and deliberate
  `web.HTTPException` responses.
* `_watch_background_task` -- an infinite loop that *exits* can never reach the
  alert calls inside its own `except` blocks, so its death is reported by a
  done-callback instead.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiohttp
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from smart_proxy import anthropic_proxy
from smart_proxy.notifier import AlertThrottle
from tests.test_anthropic_audit_event_best_effort import _FakeAsyncClient


class AlertThrottleTests(unittest.TestCase):
    def test_first_occurrence_sends_with_no_repeats(self) -> None:
        throttle = AlertThrottle(window_seconds=1800)
        self.assertEqual(throttle.should_send("sig", now=0.0), 0)

    def test_repeat_inside_window_is_suppressed(self) -> None:
        throttle = AlertThrottle(window_seconds=1800)
        throttle.should_send("sig", now=0.0)
        self.assertIsNone(throttle.should_send("sig", now=1.0))
        self.assertIsNone(throttle.should_send("sig", now=1799.0))

    def test_after_window_sends_with_the_suppressed_count(self) -> None:
        throttle = AlertThrottle(window_seconds=1800)
        throttle.should_send("sig", now=0.0)
        for i in range(412):
            throttle.should_send("sig", now=1.0 + i)
        # The next send past the window reports how many were swallowed.
        self.assertEqual(throttle.should_send("sig", now=1801.0), 412)
        # ...and the counter resets, so the following window starts clean.
        self.assertIsNone(throttle.should_send("sig", now=1802.0))

    def test_distinct_signatures_are_independent(self) -> None:
        throttle = AlertThrottle(window_seconds=1800)
        self.assertEqual(throttle.should_send("a", now=0.0), 0)
        self.assertEqual(throttle.should_send("b", now=0.0), 0)

    def test_signature_table_is_bounded(self) -> None:
        """A pathological spread of signatures must not grow without limit."""
        throttle = AlertThrottle(window_seconds=1800, max_signatures=8)
        for i in range(100):
            throttle.should_send(f"sig-{i}", now=float(i))
        self.assertLessEqual(len(throttle._seen), 8)
        # The most recent signature is still tracked, so its repeat is suppressed.
        self.assertIsNone(throttle.should_send("sig-99", now=99.5))


def _app_with_notifier() -> dict:
    notifier = MagicMock()
    sent: list[str] = []

    async def notify(text: str) -> bool:
        sent.append(text)
        return True

    notifier.notify = notify
    app: dict = {
        "_notifier": notifier,
        "_alert_throttle": AlertThrottle(window_seconds=1800),
        "_alert_tasks": set(),
    }
    return app, sent


class FailureAlertMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, app: dict, exc: BaseException | None, *, path: str = "/v1/messages"):
        request = make_mocked_request("POST", path, app=app)

        async def handler(_request):
            if exc is not None:
                raise exc
            return web.Response(text="ok")

        return await anthropic_proxy._failure_alert_middleware(request, handler)

    async def _drain(self, app: dict) -> None:
        """Alerts are fire-and-forget tasks; let them run."""
        for _ in range(3):
            await asyncio.sleep(0)
        tasks = [t for t in app.get("_alert_tasks", set())]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_unexpected_exception_alerts_and_still_raises(self) -> None:
        app, sent = _app_with_notifier()
        with self.assertRaises(ValueError):
            await self._run(app, ValueError("integer out of range"))
        await self._drain(app)

        self.assertEqual(len(sent), 1, sent)
        # The message must be actionable without opening the log.
        self.assertIn("ValueError", sent[0])
        self.assertIn("integer out of range", sent[0])
        self.assertIn("/v1/messages", sent[0])

    async def test_repeat_of_the_same_failure_sends_once(self) -> None:
        app, sent = _app_with_notifier()
        for _ in range(50):
            with self.assertRaises(ValueError):
                await self._run(app, ValueError("boom"))
        await self._drain(app)
        self.assertEqual(len(sent), 1, sent)

    async def test_client_disconnect_does_not_alert(self) -> None:
        """The client hanging up mid-stream is routine, not a failure.

        aiohttp raises ClientConnectionResetError from StreamResponse.write();
        it is a ConnectionResetError, NOT a CancelledError -- aiohttp >= 3.9
        does not cancel the handler on disconnect.
        """
        app, sent = _app_with_notifier()
        exc = aiohttp.ClientConnectionResetError("Cannot write to closing transport")
        self.assertIsInstance(exc, ConnectionResetError)
        with self.assertRaises(aiohttp.ClientConnectionResetError):
            await self._run(app, exc)
        await self._drain(app)
        self.assertEqual(sent, [])

    async def test_deliberate_http_exception_does_not_alert(self) -> None:
        app, sent = _app_with_notifier()
        with self.assertRaises(web.HTTPFound):
            await self._run(app, web.HTTPFound(location="/_app/"))
        await self._drain(app)
        self.assertEqual(sent, [])

    async def test_cancellation_does_not_alert(self) -> None:
        app, sent = _app_with_notifier()
        with self.assertRaises(asyncio.CancelledError):
            await self._run(app, asyncio.CancelledError())
        await self._drain(app)
        self.assertEqual(sent, [])

    async def test_successful_response_passes_through(self) -> None:
        app, sent = _app_with_notifier()
        resp = await self._run(app, None)
        await self._drain(app)
        self.assertEqual(resp.status, 200)
        self.assertEqual(sent, [])

    async def test_a_broken_alert_path_never_replaces_the_original_error(self) -> None:
        """Formatting/throttling runs inside the except block -- it must not win."""
        app, _sent = _app_with_notifier()
        app["_alert_throttle"] = MagicMock()
        app["_alert_throttle"].should_send.side_effect = RuntimeError("throttle exploded")
        with self.assertRaises(ValueError):
            await self._run(app, ValueError("the real problem"))


class BackgroundTaskDeathTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_loop_that_exits_with_an_error_alerts(self) -> None:
        app, sent = _app_with_notifier()

        async def doomed_loop() -> None:
            raise RuntimeError("smoke pass exploded")

        task = asyncio.create_task(doomed_loop())
        anthropic_proxy._watch_background_task(app, "oauth_smoke", task)
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.gather(*app["_alert_tasks"], return_exceptions=True)

        self.assertEqual(len(sent), 1, sent)
        self.assertIn("oauth_smoke", sent[0])
        self.assertIn("RuntimeError", sent[0])

    async def test_a_cancelled_loop_is_a_clean_shutdown(self) -> None:
        app, sent = _app_with_notifier()

        async def sleeper() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(sleeper())
        anthropic_proxy._watch_background_task(app, "flush", task)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(sent, [])

    async def test_a_loop_that_returns_quietly_still_alerts(self) -> None:
        """These loops are `while True` -- a plain return is a bug too."""
        app, sent = _app_with_notifier()

        async def quitter() -> None:
            return None

        task = asyncio.create_task(quitter())
        anthropic_proxy._watch_background_task(app, "keepwarm", task)
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.gather(*app["_alert_tasks"], return_exceptions=True)
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("keepwarm", sent[0])


class SwallowedFailureAlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_audit_write_alerts(self) -> None:
        """The 06:37 incident's own failure mode must not be silent.

        `_record_anthropic_event` swallows DB errors so they cannot 500 a live
        request -- correct, but it must still raise its hand.
        """
        app, sent = _app_with_notifier()

        class _BoomDb:
            async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
                del kwargs
                raise RuntimeError("relation anthropic_key_events does not exist")

        await anthropic_proxy._record_anthropic_event(
            _BoomDb(), key_id="k-123456789012", event_type="refresh_attempt",
            source="proxy_request", app=app,
        )
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.gather(*app["_alert_tasks"], return_exceptions=True)

        self.assertEqual(len(sent), 1, sent)
        self.assertIn("RuntimeError", sent[0])

    async def test_audit_failure_without_an_app_still_does_not_raise(self) -> None:
        """Most call sites have no app handle; they must keep working."""

        class _BoomDb:
            async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
                del kwargs
                raise RuntimeError("boom")

        await anthropic_proxy._record_anthropic_event(
            _BoomDb(), key_id="k", event_type="refresh_attempt",
        )


class NotifierConfigTests(unittest.TestCase):
    def test_unconfigured_notifier_warns_loudly(self) -> None:
        """Silent no-op alerting is how an outage goes unreported."""
        app = {"telegram_bot_token": "", "telegram_chat_id": ""}
        with self.assertLogs("anthropic_proxy", level="WARNING") as logs:
            self.assertIsNone(anthropic_proxy._build_notifier(app))
        self.assertTrue(
            any("ANTHROPIC_TELEGRAM_BOT_TOKEN" in line for line in logs.output),
            logs.output,
        )


class RotatedTokenPersistAlertTests(unittest.IsolatedAsyncioTestCase):
    """The double-persist failure is the event that bricks a key on restart.

    A refresh consumes the single-use refresh token upstream. If persisting the
    rotated one then fails twice, the DB keeps a token Anthropic has already
    retired: serving continues on the in-memory token, but the next restart --
    or any pool reload -- reads the dead one back and the key is gone for good.
    Until now that produced two logger.critical lines and nothing else, making
    the most dangerous state in the system the least alerted one.
    """

    def setUp(self) -> None:
        notifier = MagicMock()
        self.sent: list[str] = []

        async def notify(text: str) -> bool:
            self.sent.append(text)
            return True

        notifier.notify = notify
        from smart_proxy.notifier import AlertThrottle

        anthropic_proxy._ALERT_FALLBACK.clear()
        anthropic_proxy._ALERT_FALLBACK.update({
            "_notifier": notifier,
            "_alert_throttle": AlertThrottle(window_seconds=1800),
            "_alert_tasks": set(),
        })

    def tearDown(self) -> None:
        anthropic_proxy._ALERT_FALLBACK.clear()

    async def _drain(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0)
        tasks = anthropic_proxy._ALERT_FALLBACK.get("_alert_tasks") or set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_a_lost_rotated_token_alerts_and_keeps_serving(self) -> None:
        class _PersistBoomDb:
            def is_available(self) -> bool:
                # The DB is reachable — this is the nastier case, where the
                # write fails anyway and the rotated token is simply lost.
                return True

            async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
                del kwargs
                return 1

            async def update_anthropic_oauth_tokens(self, *args, **kwargs):  # noqa: ANN002, ANN003
                del args, kwargs
                raise RuntimeError("server closed the connection unexpectedly")

        key = anthropic_proxy._AnthropicKey(
            key_id="key-brickable-1", key_type="oauth", status="active",
            api_key=None, access_token="old-access", refresh_token="old-refresh",
            client_id="client-id", expires_at=0, scopes="user:inference",
        )
        pool = anthropic_proxy.AnthropicKeyPool(_PersistBoomDb())
        pool._keys = [key]

        client = _FakeAsyncClient({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 28800,
        })
        token = await pool.ensure_valid_token(
            key, client, audit_op_id="op", audit_source="proxy_request",
            audit_path="/v1/messages", audit_model="claude-opus-5", activate=False,
        )
        await self._drain()

        # Serving must continue -- the fresh token is valid, just not durable.
        self.assertEqual(token, "new-access")
        self.assertEqual(key.refresh_token, "new-refresh")
        # ...and somebody has to be told, because a restart now loses the key.
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn(key.key_id[:12], self.sent[0])   # ids are truncated to 12
        self.assertIn("restart", self.sent[0])         # says what is at stake


if __name__ == "__main__":
    unittest.main()
