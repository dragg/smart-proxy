"""Operator-triggered cooldown clearing.

Cooldown deadlines come from upstream's retry-after but are clamped to one
hour, so a limit that resets further out re-arms a fresh hour every hour. These
cover the escape hatch that asks upstream again instead of waiting out the clamp.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import AnthropicKeyPool, _AnthropicKey  # noqa: E402


def _key(key_id: str) -> _AnthropicKey:
    return _AnthropicKey(
        key_id=key_id,
        key_type="oauth",
        status="active",
        api_key=None,
        access_token="token",
        refresh_token="refresh",
        client_id="client-id",
        expires_at=int(time.time() * 1000) + 3_600_000,
    )


class ClearCooldownsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = AnthropicKeyPool.__new__(AnthropicKeyPool)
        self.pool._cooldowns = {}
        self.pool._model_cooldowns = {}
        self.pool._refresh_backoff = {}
        self.pool._banned = set()
        self.pool._keys = [_key("key-a"), _key("key-b")]

    def _arm(self) -> None:
        deadline = time.monotonic() + 3600
        self.pool._cooldowns["key-a"] = deadline
        self.pool._model_cooldowns[("key-a", "claude-opus-5")] = deadline
        self.pool._model_cooldowns[("key-b", "claude-opus-5")] = deadline
        self.pool._model_cooldowns[("key-b", "claude-sonnet-5")] = deadline

    def test_clears_everything_by_default(self) -> None:
        self._arm()
        self.assertEqual(self.pool.clear_cooldowns(), 4)
        self.assertEqual(self.pool._cooldowns, {})
        self.assertEqual(self.pool._model_cooldowns, {})

    def test_key_becomes_available_again(self) -> None:
        self._arm()
        self.assertGreater(self.pool.next_available_in(model="claude-opus-5"), 0)
        self.pool.clear_cooldowns()
        self.assertEqual(self.pool.next_available_in(model="claude-opus-5"), 0)

    def test_model_filter_keeps_other_models_parked(self) -> None:
        self._arm()
        self.pool.clear_cooldowns(model="claude-opus-5")
        self.assertEqual(self.pool.next_available_in(model="claude-opus-5"), 0)
        # key-b is still parked for Sonnet; key-a is free, so the pool as a whole
        # can serve Sonnet — what must survive is key-b's own Sonnet deadline.
        self.assertIn(("key-b", "claude-sonnet-5"), self.pool._model_cooldowns)

    def test_model_filter_also_drops_key_level_cooldowns(self) -> None:
        # A key-level cooldown blocks every model, so leaving it would make
        # "clear this model" a no-op for that key.
        self._arm()
        self.pool.clear_cooldowns(model="claude-opus-5")
        self.assertEqual(self.pool._cooldowns, {})

    def test_refresh_backoff_and_bans_are_untouched(self) -> None:
        # Those guard the OAuth refresh path, where retrying too eagerly can
        # burn a single-use refresh token and brick the key.
        self._arm()
        self.pool._refresh_backoff["key-a"] = time.monotonic() + 300
        self.pool._banned.add("key-b")
        self.pool.clear_cooldowns()
        self.assertIn("key-a", self.pool._refresh_backoff)
        self.assertIn("key-b", self.pool._banned)

    def test_clearing_nothing_reports_zero(self) -> None:
        self.assertEqual(self.pool.clear_cooldowns(), 0)
        self.assertEqual(self.pool.clear_cooldowns(model="claude-opus-5"), 0)


if __name__ == "__main__":
    unittest.main()
