# tests/test_spend_limit_alerts.py
"""Telegram alerts when a caller's 24h spend cap runs out.

A key that hits its cap starts answering 429 to its consumer and nothing says
so out loud -- the operator only learns about it when someone complains. These
tests pin the three halves of the fix: the message names the consumer without
printing a usable key, a repeat is swallowed, and delivery is fire-and-forget
so a broken Telegram cannot touch the request that produced it.

The repeat case is not hypothetical. ``KeyLimiter.add`` derives a crossing from
one call's before/after spend, and ``seed`` can move that counter *backwards*
mid-window -- it re-reads ``usage_key_hourly``, which lags the live counter by
up to a flush interval. A restart that loses the last <60s of spend, or a
``/_reload``, therefore re-arms a threshold the operator has already been told
about. The latch here is what makes "one message per key per window" true.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy import anthropic_proxy
from smart_proxy.key_limits import LimitCrossing

# The real shape: sp- plus 32 hex (see __main__.py's key generator).
_PROXY_KEY = "sp-0123456789abcdef0123456789abcdef"


def _crossing(
    level: str = "warn",
    spent: float = 41.30,
    percent: float = 82.6,
    limit: float = 50.0,
    resets_at: str = "2026-09-09T00:00:00+02:00",
) -> LimitCrossing:
    return LimitCrossing(
        kind="daily_usd",
        label="24h",
        level=level,
        limit_usd=limit,
        spent_usd=spent,
        percent=percent,
        resets_at=resets_at,
        retry_after=7 * 3600 + 12 * 60,
    )


class _FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify(self, text: str) -> bool:
        self.messages.append(text)
        return True


class _FakePool:
    def __init__(self, names: dict[str, str] | None = None) -> None:
        self._names = names or {}

    def proxy_key_name(self, proxy_key: str) -> str:
        return self._names.get(proxy_key, "")


class _PoolWithoutNames:
    """A pool double from before proxy_key_name existed."""


class FormatTests(unittest.TestCase):
    def test_warning_names_the_consumer_and_what_is_left(self):
        text = anthropic_proxy._format_spend_alert(
            "Acme webapp …abcdef", _crossing())
        self.assertTrue(text.startswith("🟡"))
        self.assertIn("Acme webapp", text)
        self.assertIn("24h", text)
        self.assertIn("$41.30", text)
        self.assertIn("$50.00", text)
        self.assertIn("82.6%", text)
        self.assertIn("7h 12m", text)
        self.assertIn("2026-09-09T00:00:00+02:00", text)

    def test_exhausted_alert_is_louder_and_says_calls_are_refused(self):
        text = anthropic_proxy._format_spend_alert(
            "Acme webapp …abcdef", _crossing("exhausted", 50.40, 100.8))
        self.assertTrue(text.startswith("🔴"))
        self.assertIn("429", text)
        self.assertIn("$50.40", text)
        self.assertIn("7h 12m", text)


class CallerLabelTests(unittest.TestCase):
    """The alert reuses the caller label the rest of the proxy already uses,
    so a key prefix never reaches Telegram and internal buckets read as what
    they are rather than as a masked key."""

    def _label(self, pool, key=_PROXY_KEY):
        app = {"_notifier": _FakeNotifier(), "_alert_tasks": set(), "anthropic_pool": pool}

        async def run():
            anthropic_proxy._alert_spend_threshold(app, key, _crossing())
            await asyncio.gather(*list(app["_alert_tasks"]), return_exceptions=True)
            return app["_notifier"].messages[0]

        return asyncio.run(run())

    def test_named_key_shows_the_name_and_only_a_short_tail(self):
        text = self._label(_FakePool({_PROXY_KEY: "Acme webapp"}))
        self.assertIn("Acme webapp", text)
        self.assertIn("…abcdef", text)
        self.assertNotIn(_PROXY_KEY, text)
        self.assertNotIn(_PROXY_KEY[:12], text)

    def test_unnamed_key_still_carries_its_tail(self):
        text = self._label(_FakePool())
        self.assertIn("…abcdef", text)

    def test_internal_bucket_is_not_dressed_up_as_a_masked_key(self):
        text = self._label(_FakePool(), key="claude-passthrough")
        self.assertIn("claude-passthrough", text)
        self.assertNotIn("…", text.splitlines()[0])

    def test_a_pool_without_name_lookup_still_gets_an_alert_out(self):
        text = self._label(_PoolWithoutNames())
        self.assertIn("…abcdef", text)


class DeliveryTests(unittest.TestCase):
    def _app(self, notifier, name="Acme webapp"):
        return {
            "_notifier": notifier,
            "_alert_tasks": set(),
            "anthropic_pool": _FakePool({_PROXY_KEY: name}),
        }

    def _fire(self, app, *crossings):
        async def run():
            for crossing in crossings:
                anthropic_proxy._alert_spend_threshold(app, _PROXY_KEY, crossing)
            await asyncio.gather(*list(app["_alert_tasks"]), return_exceptions=True)

        asyncio.run(run())

    def test_alert_reaches_telegram_with_the_key_name(self):
        notifier = _FakeNotifier()
        app = self._app(notifier)
        self._fire(app, _crossing("exhausted", 50.40, 100.8))
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("Acme webapp", notifier.messages[0])

    def test_a_repeated_crossing_in_the_same_window_is_swallowed(self):
        notifier = _FakeNotifier()
        app = self._app(notifier)
        self._fire(app, _crossing(), _crossing())
        self.assertEqual(len(notifier.messages), 1)

    def test_both_levels_are_announced_in_the_same_window(self):
        notifier = _FakeNotifier()
        app = self._app(notifier)
        self._fire(app, _crossing("warn"), _crossing("exhausted", 50.4, 100.8))
        self.assertEqual(len(notifier.messages), 2)

    def test_the_next_window_re_arms_the_alert(self):
        notifier = _FakeNotifier()
        app = self._app(notifier)
        self._fire(app, _crossing(), _crossing(resets_at="2026-09-10T00:00:00+02:00"))
        self.assertEqual(len(notifier.messages), 2)

    def test_raising_the_cap_re_arms_the_alert_inside_the_window(self):
        notifier = _FakeNotifier()
        app = self._app(notifier)
        self._fire(app, _crossing(), _crossing(spent=410.0, percent=82.0, limit=500.0))
        self.assertEqual(len(notifier.messages), 2)

    def test_two_keys_do_not_share_a_latch(self):
        notifier = _FakeNotifier()
        app = self._app(notifier)

        async def run():
            anthropic_proxy._alert_spend_threshold(app, _PROXY_KEY, _crossing())
            anthropic_proxy._alert_spend_threshold(app, "sp-" + "f" * 32, _crossing())
            await asyncio.gather(*list(app["_alert_tasks"]), return_exceptions=True)

        asyncio.run(run())
        self.assertEqual(len(notifier.messages), 2)

    def test_without_a_notifier_nothing_happens(self):
        app = {"_alert_tasks": set(), "anthropic_pool": _FakePool()}
        anthropic_proxy._alert_spend_threshold(app, _PROXY_KEY, _crossing())
        self.assertEqual(app["_alert_tasks"], set())

    def test_a_missing_pool_still_sends_the_alert(self):
        """The name is a nicety; losing it must not cost the alert itself."""
        async def run():
            notifier = _FakeNotifier()
            app = {"_notifier": notifier, "_alert_tasks": set()}
            anthropic_proxy._alert_spend_threshold(app, _PROXY_KEY, _crossing())
            await asyncio.gather(*list(app["_alert_tasks"]), return_exceptions=True)
            self.assertEqual(len(notifier.messages), 1)

        asyncio.run(run())

    def test_a_failure_while_announcing_is_filed_under_alerting(self):
        """Not under 'spend accounting' -- the spend was already counted."""
        class _Broken:
            def notify(self, text: str) -> None:   # not awaitable
                return None

        sources = []
        app = {
            "_notifier": _Broken(),
            "_alert_tasks": set(),
            "anthropic_pool": _FakePool(),
        }
        with unittest.mock.patch.object(
            anthropic_proxy, "_alert_failure",
            lambda *a, **kw: sources.append(kw.get("source")),
        ):
            anthropic_proxy._alert_spend_threshold(app, _PROXY_KEY, _crossing())
        self.assertEqual(sources, ["spend threshold alert"])


if __name__ == "__main__":
    unittest.main()
