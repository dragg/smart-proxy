"""How long the proxy is allowed to finish in-flight requests on SIGTERM.

aiohttp stops accepting connections and then waits for running handlers, but
its default is 60 s while a single upstream call may run for 600 s
(``_UPSTREAM_TIMEOUT``). A deploy restart therefore used to cut long Claude
Code turns in half. The wait is now a setting, and systemd's TimeoutStopSec
has to be larger than it or SIGKILL arrives first.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from smart_proxy.config import Settings


class ShutdownTimeoutSettingTests(unittest.TestCase):
    def test_default_outlasts_a_slow_turn(self) -> None:
        value = Settings(_env_file=None).anthropic_proxy_shutdown_timeout_seconds
        self.assertGreaterEqual(value, 120.0)

    def test_is_configurable(self) -> None:
        self.assertEqual(
            Settings(
                _env_file=None, anthropic_proxy_shutdown_timeout_seconds=5.0
            ).anthropic_proxy_shutdown_timeout_seconds,
            5.0,
        )


class RunAppWiringTests(unittest.TestCase):
    def test_main_passes_the_timeout_to_run_app(self) -> None:
        from smart_proxy import anthropic_proxy

        captured: dict = {}

        def fake_run_app(app, **kwargs):
            captured.update(kwargs)

        settings = Settings(
            _env_file=None, anthropic_proxy_shutdown_timeout_seconds=7.5
        )
        with (
            patch.object(anthropic_proxy.web, "run_app", fake_run_app),
            patch.object(anthropic_proxy, "create_app", lambda *a, **k: {}),
            patch("smart_proxy.config.get_settings", lambda: settings),
        ):
            anthropic_proxy.main()

        self.assertEqual(captured.get("shutdown_timeout"), 7.5)


if __name__ == "__main__":
    unittest.main()
