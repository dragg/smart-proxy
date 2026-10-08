from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database
from smart_proxy.usage import build_price_lookup, calculate_cost, estimate_cost_with_cache
from tests.db_test_utils import connect_test_database


class ModelPricesTests(unittest.TestCase):
    def test_model_prices_auto_seed_on_connect(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    count = await db.count_model_prices()
                    self.assertGreater(count, 0)
                    rows = await db.get_all_model_prices()
                    sonnet = next((r for r in rows if r["model_prefix"] == "claude-sonnet-4-6"), None)
                    self.assertIsNotNone(sonnet)
                    assert sonnet is not None
                    self.assertEqual(sonnet["provider"], "anthropic")
                    self.assertIsNotNone(sonnet["cache_read_price"])
                    self.assertIsNotNone(sonnet["cache_write_5m_price"])
                    self.assertIsNotNone(sonnet["cache_write_1h_price"])
                finally:
                    await db.close()

        asyncio.run(run())

    def test_image_model_prices_include_gpt_image_1_and_2(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    rows = await db.get_all_model_prices()
                    by_prefix = {row["model_prefix"]: row for row in rows}

                    self.assertIn("gpt-image-1", by_prefix)
                    self.assertIn("gpt-image-2", by_prefix)
                    self.assertEqual(by_prefix["gpt-image-1"]["provider"], "openai")
                    self.assertEqual(by_prefix["gpt-image-1"]["input_price"], 5.0)
                    self.assertEqual(by_prefix["gpt-image-1"]["output_price"], 40.0)
                    self.assertEqual(by_prefix["gpt-image-2"]["provider"], "openai")
                    self.assertEqual(by_prefix["gpt-image-2"]["input_price"], 5.0)
                    self.assertEqual(by_prefix["gpt-image-2"]["output_price"], 30.0)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_upsert_and_calculate_from_db_prices(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    await db.upsert_model_price(
                        model_prefix="my-model",
                        provider="test",
                        input_price=2.0,
                        output_price=4.0,
                        cache_read_price=0.5,
                        cache_write_5m_price=3.0,
                        cache_write_1h_price=6.0,
                    )
                    prices = build_price_lookup(await db.get_all_model_prices())
                    breakdown = calculate_cost(
                        model="my-model-v1",
                        input_tokens=1_000_000,
                        output_tokens=500_000,
                        cache_read_tokens=100_000,
                        cache_creation_tokens=50_000,
                        cache_creation_5m_tokens=50_000,
                        cache_creation_1h_tokens=0,
                        prices=prices,
                    )
                    self.assertFalse(breakdown.unknown_pricing)
                    self.assertEqual(breakdown.provider, "test")
                    # 2.00 + 2.00 + 0.05 + 0.15 = 4.20
                    self.assertAlmostEqual(breakdown.total_cost or 0.0, 4.20, places=6)

                    total, partial = estimate_cost_with_cache(
                        "my-model-v1",
                        input_tokens=1_000_000,
                        output_tokens=500_000,
                        cache_read_tokens=100_000,
                        cache_creation_tokens=50_000,
                        cache_creation_5m_tokens=50_000,
                        cache_creation_1h_tokens=0,
                        prices=prices,
                    )
                    self.assertFalse(partial)
                    self.assertAlmostEqual(total or 0.0, 4.20, places=6)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_new_claude_models_seeded_with_explicit_prices(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    rows = await db.get_all_model_prices()
                    by_prefix = {row["model_prefix"]: row for row in rows}

                    for prefix in ("claude-opus-4-8", "claude-opus-4-7"):
                        row = by_prefix[prefix]
                        self.assertEqual(row["provider"], "anthropic")
                        self.assertEqual(row["input_price"], 5.0)
                        self.assertEqual(row["output_price"], 25.0)
                        self.assertEqual(row["cache_write_1h_price"], 10.0)

                    sonnet5 = by_prefix["claude-sonnet-5"]
                    self.assertEqual(sonnet5["provider"], "anthropic")
                    self.assertEqual(sonnet5["input_price"], 2.0)
                    self.assertEqual(sonnet5["output_price"], 10.0)
                    self.assertEqual(sonnet5["cache_write_1h_price"], 4.0)

                    for prefix in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
                        self.assertEqual(by_prefix[prefix]["provider"], "anthropic")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_reseeding_corrects_untouched_superseded_default(self) -> None:
        """claude-sonnet-5 first shipped at $3/$15; the real price is $2/$10.

        A row still holding the old default exactly was never customized, so
        re-seeding moves it to the corrected default.
        """

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    await db.upsert_model_price(
                        model_prefix="claude-sonnet-5",
                        provider="anthropic",
                        input_price=3.0,
                        output_price=15.0,
                        cache_read_price=0.3,
                        cache_write_5m_price=3.75,
                        cache_write_1h_price=6.0,
                    )

                    await db._seed_model_prices()

                    rows = await db.get_all_model_prices()
                    sonnet5 = {row["model_prefix"]: row for row in rows}["claude-sonnet-5"]
                    self.assertEqual(sonnet5["input_price"], 2.0)
                    self.assertEqual(sonnet5["output_price"], 10.0)
                    self.assertEqual(sonnet5["cache_read_price"], 0.2)
                    self.assertEqual(sonnet5["cache_write_5m_price"], 2.5)
                    self.assertEqual(sonnet5["cache_write_1h_price"], 4.0)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_reseeding_does_not_clobber_user_customized_price(self) -> None:
        """A user-set price for a prefix must survive later re-seeding.

        Simulates: user manually ran `price set claude-sonnet-5 ...` with a
        custom rate before this prefix existed in the built-in defaults, then
        the app re-seeds on a later connect (e.g. after an upgrade adds
        claude-sonnet-5 to the defaults). Their custom row must not be
        overwritten by the default value.
        """

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    await db.upsert_model_price(
                        model_prefix="claude-sonnet-5",
                        provider="anthropic",
                        input_price=99.0,
                        output_price=199.0,
                        cache_read_price=9.9,
                        cache_write_5m_price=9.9,
                        cache_write_1h_price=9.9,
                    )

                    await db._seed_model_prices()

                    rows = await db.get_all_model_prices()
                    by_prefix = {row["model_prefix"]: row for row in rows}
                    self.assertEqual(by_prefix["claude-sonnet-5"]["input_price"], 99.0)
                    self.assertEqual(by_prefix["claude-sonnet-5"]["output_price"], 199.0)
                finally:
                    await db.close()

        asyncio.run(run())


class PrefixMatchingTests(unittest.TestCase):
    """Regression coverage for the prefix-resolution algorithm in calculate_cost.

    A newly released model (e.g. claude-opus-4-8) must never silently price
    like an unrelated, older bare-family entry (e.g. claude-opus-4, the
    original 2025 Opus 4). See _resolve_matched_prefix in usage.py.
    """

    def test_new_opus_models_do_not_fall_back_to_old_opus_4_price(self) -> None:
        for model in ("claude-opus-4-8", "claude-opus-4-7"):
            breakdown = calculate_cost(model, input_tokens=1_000_000, output_tokens=0)
            self.assertFalse(breakdown.unknown_pricing)
            self.assertEqual(breakdown.matched_prefix, model)
            self.assertAlmostEqual(breakdown.input_cost, 5.0, places=6)

    def test_sonnet_5_is_priced_not_unknown(self) -> None:
        breakdown = calculate_cost("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)
        self.assertFalse(breakdown.unknown_pricing)
        self.assertEqual(breakdown.matched_prefix, "claude-sonnet-5")
        self.assertAlmostEqual(breakdown.input_cost, 2.0, places=6)

    def test_claude_5_5_family_priced_exactly(self) -> None:
        # (model, input, output, cache_read, cache_write_5m, cache_write_1h) per MTok
        cases = (
            ("claude-opus-5-5", 4.00, 20.00, 0.20, 5.00, 8.00),
            ("claude-sonnet-5-5", 2.00, 10.00, 0.10, 2.50, 4.00),
            ("claude-haiku-5-5", 0.10, 0.50, 0.01, 0.125, 0.20),
            ("claude-fable-5-1", 10.00, 50.00, 0.25, 12.50, 20.00),
            ("claude-mythos-5-1", 10.00, 50.00, 0.25, 12.50, 20.00),
        )
        for model, inp, out, read, w5, w1 in cases:
            with self.subTest(model=model):
                b = calculate_cost(
                    model,
                    input_tokens=1_000_000,
                    output_tokens=1_000_000,
                    cache_read_tokens=1_000_000,
                    cache_creation_5m_tokens=1_000_000,
                    cache_creation_1h_tokens=1_000_000,
                )
                self.assertEqual(b.matched_prefix, model)
                self.assertAlmostEqual(b.input_cost, inp, places=6)
                self.assertAlmostEqual(b.output_cost, out, places=6)
                self.assertAlmostEqual(b.cache_read_cost, read, places=6)
                self.assertAlmostEqual(b.cache_write_5m_cost, w5, places=6)
                self.assertAlmostEqual(b.cache_write_1h_cost, w1, places=6)

    def test_previous_generation_not_priced_like_cheaper_5_5(self) -> None:
        # Adding claude-opus-5-5 ($4) must not reprice Opus 5 ($5) or a dated
        # Opus 5.5 snapshot onto the wrong entry.
        self.assertEqual(calculate_cost("claude-opus-5", 1_000_000, 0).input_cost, 5.0)
        snap = calculate_cost("claude-opus-5-5-20261001", 1_000_000, 0)
        self.assertEqual(snap.matched_prefix, "claude-opus-5-5")
        self.assertAlmostEqual(snap.input_cost, 4.0, places=6)

    def test_dotted_5_5_alias_prices_like_dashed(self) -> None:
        for dotted, dashed in (("claude-opus-5.5", "claude-opus-5-5"),
                               ("claude-sonnet-5.5", "claude-sonnet-5-5"),
                               ("claude-haiku-5.5", "claude-haiku-5-5")):
            with self.subTest(model=dotted):
                self.assertEqual(calculate_cost(dotted, 1_000_000, 0).matched_prefix, dashed)

    def test_legacy_bare_opus_4_and_dated_snapshot_keep_old_price(self) -> None:
        for model in ("claude-opus-4", "claude-opus-4-20250514"):
            breakdown = calculate_cost(model, input_tokens=1_000_000, output_tokens=0)
            self.assertFalse(breakdown.unknown_pricing)
            self.assertEqual(breakdown.matched_prefix, "claude-opus-4")
            self.assertAlmostEqual(breakdown.input_cost, 15.0, places=6)

    def test_unrecognized_future_opus_version_falls_back_to_latest_known_opus(self) -> None:
        # "claude-opus-4-99" doesn't exist in the price table. It must price
        # like the newest known Opus 4.x entry ($5/$25) -- not the old bare
        # "claude-opus-4" entry ($15/$75), and not a later generation
        # ("claude-opus-5-5", $4/$20) it doesn't belong to.
        breakdown = calculate_cost(
            "claude-opus-4-99",
            input_tokens=1_000_000,
            output_tokens=0,
            cache_creation_1h_tokens=1_000_000,
        )
        self.assertFalse(breakdown.unknown_pricing)
        self.assertEqual(breakdown.matched_prefix, "claude-opus-4-8")
        self.assertAlmostEqual(breakdown.input_cost, 5.0, places=6)
        self.assertAlmostEqual(breakdown.cache_write_1h_cost, 10.0, places=6)

    def test_unrecognized_future_version_uses_nearest_older_known_entry(self) -> None:
        self.assertEqual(calculate_cost("claude-opus-5-7", 1_000_000, 0).matched_prefix, "claude-opus-5-5")
        self.assertEqual(calculate_cost("claude-sonnet-6", 1_000_000, 0).matched_prefix, "claude-sonnet-5-5")

    def test_completely_unknown_model_is_still_unknown_pricing(self) -> None:
        breakdown = calculate_cost("some-made-up-model", input_tokens=1_000, output_tokens=1_000)
        self.assertTrue(breakdown.unknown_pricing)
        self.assertIsNone(breakdown.total_cost)


if __name__ == "__main__":
    unittest.main()
