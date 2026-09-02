from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import AnthropicKeyPool, _humanize_seconds
from tests.db_test_utils import connect_test_database


class HumanizeSecondsTests(unittest.TestCase):
    def test_formats_two_largest_units(self) -> None:
        self.assertEqual(_humanize_seconds(45), "45s")
        self.assertEqual(_humanize_seconds(90), "1m 30s")
        self.assertEqual(_humanize_seconds(3600), "1h")
        self.assertEqual(_humanize_seconds(4500), "1h 15m")
        self.assertEqual(_humanize_seconds(90062), "1d 1h")

    def test_non_positive_is_zero_seconds(self) -> None:
        self.assertEqual(_humanize_seconds(0), "0s")
        self.assertEqual(_humanize_seconds(-5), "0s")


async def _pool_with_single_oauth_key(td: str) -> tuple[AnthropicKeyPool, object]:
    db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "test.db"))
    await db.insert_anthropic_key(
        id="key-solo",
        key_type="oauth",
        access_token="token-solo",
        refresh_token="refresh-solo",
        client_id="client-id",
        expires_at=9999999999999,
        scopes=json.dumps(["user:profile", "user:inference"]),
        name="key-solo",
    )
    pool = AnthropicKeyPool(db)
    await pool.reload()
    return pool, db


class ModelCooldownTests(unittest.TestCase):
    def test_model_cooldown_isolates_only_that_model(self) -> None:
        """A 429 for one model must not brick the whole (single) key."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                pool, db = await _pool_with_single_oauth_key(td)
                try:
                    key = pool.pick(model="claude-fable-5")
                    self.assertIsNotNone(key)
                    assert key is not None

                    pool.cooldown(key, 99, model="claude-fable-5")

                    # fable-5 is parked on this key ...
                    self.assertIsNone(pool.pick(model="claude-fable-5"))
                    # ... but every other model still flows through the same key.
                    other = pool.pick(model="claude-sonnet-5")
                    self.assertIsNotNone(other)
                    assert other is not None
                    self.assertEqual(other.key_id, "key-solo")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_whole_key_cooldown_still_blocks_all_models(self) -> None:
        """A key-level cooldown (refresh/SSE) parks every model, as before."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                pool, db = await _pool_with_single_oauth_key(td)
                try:
                    key = pool.pick(model="claude-fable-5")
                    assert key is not None
                    pool.cooldown(key, 99)  # no model → whole key

                    self.assertIsNone(pool.pick(model="claude-fable-5"))
                    self.assertIsNone(pool.pick(model="claude-sonnet-5"))
                    self.assertIsNone(pool.pick())
                finally:
                    await db.close()

        asyncio.run(run())

    def test_cooldown_duration_is_capped(self) -> None:
        """A pathological retry-after (25h) is capped so it can't park for a day."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                pool, db = await _pool_with_single_oauth_key(td)
                try:
                    key = pool.pick(model="claude-fable-5")
                    assert key is not None
                    pool.cooldown(key, 90084, model="claude-fable-5")

                    remaining = pool.next_available_in(model="claude-fable-5")
                    self.assertGreater(remaining, 0)
                    self.assertLessEqual(remaining, 3601)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_next_available_in_reports_model_cooldown(self) -> None:
        """When the only key is model-cooled, the retry-after reflects it."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                pool, db = await _pool_with_single_oauth_key(td)
                try:
                    key = pool.pick(model="claude-fable-5")
                    assert key is not None
                    pool.cooldown(key, 120, model="claude-fable-5")

                    # fable-5 has to wait ...
                    self.assertGreater(pool.next_available_in(model="claude-fable-5"), 0)
                    # ... sonnet does not.
                    self.assertEqual(pool.next_available_in(model="claude-sonnet-5"), 0)
                finally:
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
