from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import aiosqlite


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database, build_usage_upsert_sql
from smart_proxy.usage import (
    UsageTracker,
    estimate_cost_with_cache,
    extract_usage,
    extract_usage_from_sse,
)
from tests.db_test_utils import connect_test_database


class AnthropicUsageParsingTests(unittest.TestCase):
    def test_extract_usage_from_sse_includes_cache_split(self) -> None:
        sse = (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":11,'
            b'"cache_read_input_tokens":22,"cache_creation_input_tokens":33,'
            b'"cache_creation":{"ephemeral_5m_input_tokens":7,"ephemeral_1h_input_tokens":26},'
            b'"output_tokens":0}}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n\n'
        )
        got = extract_usage_from_sse("anthropic", sse)
        self.assertEqual(got, (11, 5, 22, 33, 7, 26, 0))

    def test_extract_usage_non_stream_anthropic_includes_cache_split(self) -> None:
        body = (
            b'{"usage":{"input_tokens":3,"output_tokens":4,'
            b'"cache_read_input_tokens":10,"cache_creation_input_tokens":20,'
            b'"cache_creation":{"ephemeral_5m_input_tokens":8,"ephemeral_1h_input_tokens":12}}}'
        )
        got = extract_usage("anthropic", body)
        self.assertEqual(got, (3, 4, 10, 20, 8, 12, 0))

    def test_extract_usage_includes_web_search_requests(self) -> None:
        body = (
            b'{"usage":{"input_tokens":3,"output_tokens":4,'
            b'"cache_read_input_tokens":10,"cache_creation_input_tokens":20,'
            b'"cache_creation":{"ephemeral_5m_input_tokens":8,"ephemeral_1h_input_tokens":12},'
            b'"server_tool_use":{"web_search_requests":2}}}'
        )
        got = extract_usage("anthropic", body)
        self.assertEqual(got, (3, 4, 10, 20, 8, 12, 2))


class UsagePricingTests(unittest.TestCase):
    def test_estimate_cost_with_cache_exact_split(self) -> None:
        cost, partial = estimate_cost_with_cache(
            "claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
            cache_creation_tokens=2_000_000,
            cache_creation_5m_tokens=1_000_000,
            cache_creation_1h_tokens=1_000_000,
            web_search_requests=2,
        )
        self.assertFalse(partial)
        # 3 + 15 + 0.3 + 3.75 + 6.0 + 0.02 = 28.07
        self.assertAlmostEqual(cost or 0.0, 28.07, places=6)

    def test_estimate_cost_with_cache_legacy_fallback_marks_partial(self) -> None:
        cost, partial = estimate_cost_with_cache(
            "claude-sonnet-4-6",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=1_000_000,
            cache_creation_5m_tokens=0,
            cache_creation_1h_tokens=0,
        )
        self.assertTrue(partial)
        self.assertAlmostEqual(cost or 0.0, 3.75, places=6)


class UsageDatabaseFlowTests(unittest.TestCase):
    def test_usage_upsert_sql_qualifies_accumulator_columns_for_postgres(self) -> None:
        sql = build_usage_upsert_sql("postgres")

        self.assertIn(
            "input_tokens          = usage_daily.input_tokens + excluded.input_tokens",
            sql,
        )
        self.assertIn(
            "output_tokens         = usage_daily.output_tokens + excluded.output_tokens",
            sql,
        )
        self.assertIn(
            "requests              = usage_daily.requests + excluded.requests",
            sql,
        )

    def test_tracker_flush_upserts_new_cache_columns_and_query_returns_them(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    tracker = UsageTracker()
                    tracker.record(
                        "claude-passthrough",
                        "cred-1",
                        "anthropic",
                        "claude-sonnet-4-6",
                        10,
                        20,
                        30,
                        40,
                        12,
                        28,
                        2,
                        group_name="claude-pro",
                    )
                    tracker.record(
                        "claude-passthrough",
                        "cred-1",
                        "anthropic",
                        "claude-sonnet-4-6",
                        1,
                        2,
                        3,
                        4,
                        1,
                        3,
                        1,
                        group_name="claude-pro",
                    )
                    await tracker.flush(db)

                    rows = await db.query_usage("2000-01-01", "2100-01-01", by_key=False)
                    self.assertEqual(len(rows), 1)
                    row = rows[0]
                    self.assertEqual(row["input_tokens"], 11)
                    self.assertEqual(row["output_tokens"], 22)
                    self.assertEqual(row["cache_read_tokens"], 33)
                    self.assertEqual(row["cache_creation_tokens"], 44)
                    self.assertEqual(row["cache_creation_5m_tokens"], 13)
                    self.assertEqual(row["cache_creation_1h_tokens"], 31)
                    self.assertEqual(row["web_search_requests"], 3)
                    self.assertEqual(row["requests"], 2)

                    rows_by_key = await db.query_usage("2000-01-01", "2100-01-01", by_key=True)
                    self.assertEqual(len(rows_by_key), 1)
                    self.assertEqual(rows_by_key[0]["group_name"], "claude-pro")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_upsert_usage_batch_keeps_legacy_null_group_and_accepts_new_group(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    await db.upsert_usage_batch(
                        [
                            (
                                "2026-04-03",
                                "sp-key",
                                None,
                                "cred-openai",
                                "openai",
                                "gpt-4o-mini",
                                0,  # via_openai_compat
                                10,
                                20,
                                0,
                                0,
                                0,
                                0,
                                0,
                                1,
                            ),
                            (
                                "2026-04-03",
                                "sp-key",
                                "claude-pro",
                                "cred-ant",
                                "anthropic",
                                "claude-sonnet-4-6",
                                0,  # via_openai_compat
                                30,
                                40,
                                50,
                                60,
                                25,
                                35,
                                4,
                                1,
                            ),
                        ]
                    )
                    rows = await db.query_usage("2026-04-03", "2026-04-03", by_key=True)
                    self.assertEqual(len(rows), 2)
                    by_cred = {(r["provider"], r["model"]): r for r in rows}
                    self.assertIsNone(by_cred[("openai", "gpt-4o-mini")]["group_name"])
                    self.assertEqual(
                        by_cred[("anthropic", "claude-sonnet-4-6")]["group_name"],
                        "claude-pro",
                    )
                    self.assertEqual(
                        by_cred[("anthropic", "claude-sonnet-4-6")]["web_search_requests"],
                        4,
                    )
                finally:
                    await db.close()

        asyncio.run(run())

    def test_query_usage_by_key_model_aggregates_range_and_keeps_names(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    await db.add_proxy_key("sp-team", "Team Key")
                    await db.upsert_usage_batch(
                        [
                            (
                                "2026-04-01",
                                "sp-team",
                                None,
                                "cred-openai-1",
                                "openai",
                                "gpt-4o-mini",
                                0,  # via_openai_compat
                                10,
                                20,
                                0,
                                0,
                                0,
                                0,
                                0,
                                1,
                            ),
                            (
                                "2026-04-02",
                                "sp-team",
                                None,
                                "cred-openai-2",
                                "openai",
                                "gpt-4o-mini",
                                0,  # via_openai_compat
                                30,
                                40,
                                0,
                                0,
                                0,
                                0,
                                0,
                                2,
                            ),
                            (
                                "2026-04-02",
                                "claude-passthrough",
                                "Claude OAuth",
                                "cred-ant",
                                "anthropic",
                                "claude-sonnet-4-6",
                                0,  # via_openai_compat
                                100,
                                200,
                                30,
                                50,
                                20,
                                30,
                                1,
                                3,
                            ),
                        ]
                    )

                    rows = await db.query_usage_by_key_model("2026-04-01", "2026-04-02")

                    by_model = {(r["proxy_key"], r["provider"], r["model"]): r for r in rows}
                    openai = by_model[("sp-team", "openai", "gpt-4o-mini")]
                    self.assertEqual(openai["key_name"], "Team Key")
                    self.assertIsNone(openai["group_name"])
                    self.assertEqual(openai["input_tokens"], 40)
                    self.assertEqual(openai["output_tokens"], 60)
                    self.assertEqual(openai["requests"], 3)

                    anthropic = by_model[
                        ("claude-passthrough", "anthropic", "claude-sonnet-4-6")
                    ]
                    self.assertEqual(anthropic["key_name"], "")
                    self.assertEqual(anthropic["group_name"], "Claude OAuth")
                    self.assertEqual(anthropic["cache_read_tokens"], 30)
                    self.assertEqual(anthropic["cache_creation_tokens"], 50)
                    self.assertEqual(anthropic["cache_creation_5m_tokens"], 20)
                    self.assertEqual(anthropic["cache_creation_1h_tokens"], 30)
                    self.assertEqual(anthropic["web_search_requests"], 1)
                    self.assertEqual(anthropic["requests"], 3)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_sqlite_migration_adds_via_openai_compat_to_existing_db(self) -> None:
        """An old usage_daily (no via_openai_compat) is rebuilt, rows preserved as native."""
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "old.db")
                # Seed a pre-migration usage_daily (old PK, no via_openai_compat).
                async with aiosqlite.connect(db_path) as raw:
                    await raw.execute(
                        """CREATE TABLE usage_daily (
                            date                  TEXT NOT NULL,
                            proxy_key             TEXT NOT NULL DEFAULT '',
                            group_name            TEXT,
                            credential_id         TEXT NOT NULL,
                            provider              TEXT NOT NULL,
                            model                 TEXT NOT NULL,
                            input_tokens          INTEGER NOT NULL DEFAULT 0,
                            output_tokens         INTEGER NOT NULL DEFAULT 0,
                            cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                            cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
                            cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
                            web_search_requests   INTEGER NOT NULL DEFAULT 0,
                            requests              INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (date, proxy_key, credential_id, provider, model)
                        )"""
                    )
                    await raw.execute(
                        "INSERT INTO usage_daily (date, proxy_key, group_name, "
                        "credential_id, provider, model, input_tokens, output_tokens, requests) "
                        "VALUES ('2026-05-01', 'sp-old', 'Legacy', 'cred-x', "
                        "'anthropic', 'claude-sonnet-5', 7, 9, 2)"
                    )
                    await raw.commit()

                # Connecting runs the SQLite migration (rebuild). Use the
                # SQLite Database directly — this path is backend-specific.
                db = Database(db_path=db_path)
                await db.connect()
                try:
                    cur = await db.db.execute("PRAGMA table_info(usage_daily)")
                    cols = {row["name"] for row in await cur.fetchall()}
                    self.assertIn("via_openai_compat", cols)

                    rows = await db.query_usage_by_key_model("2000-01-01", "2100-01-01")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(int(rows[0]["via_openai_compat"]), 0)
                    self.assertEqual(rows[0]["input_tokens"], 7)
                    self.assertEqual(rows[0]["group_name"], "Legacy")

                    # New PK now separates native vs compat for the same row.
                    tracker = UsageTracker()
                    tracker.record(
                        "sp-old", "cred-x", "anthropic", "claude-sonnet-5",
                        1, 1, group_name="Legacy", via_openai_compat=True,
                    )
                    await tracker.flush(db)
                    rows2 = await db.query_usage_by_key_model("2000-01-01", "2100-01-01")
                    self.assertEqual(len(rows2), 2)
                finally:
                    await db.close()

        asyncio.run(run())

    def test_via_openai_compat_rows_stay_distinct_from_native(self) -> None:
        """Same key/model/day split by via_openai_compat is two rows, not merged."""
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db_path = str(Path(td) / "test.db")
                db = await connect_test_database(sqlite_fallback_path=db_path)
                try:
                    tracker = UsageTracker()
                    # Native traffic on the key.
                    tracker.record(
                        "sp-warp", "cred-1", "anthropic", "claude-sonnet-5",
                        10, 20, group_name="Shared", via_openai_compat=False,
                    )
                    # Compat traffic on the SAME key/model/day.
                    tracker.record(
                        "sp-warp", "cred-1", "anthropic", "claude-sonnet-5",
                        3, 4, group_name="Shared", via_openai_compat=True,
                    )
                    tracker.record(
                        "sp-warp", "cred-1", "anthropic", "claude-sonnet-5",
                        5, 6, group_name="Shared", via_openai_compat=True,
                    )
                    flushed = await tracker.flush(db)
                    self.assertEqual(len(flushed), 2)  # native + compat, distinct

                    rows = await db.query_usage_by_key_model("2000-01-01", "2100-01-01")
                    by_flag = {int(r["via_openai_compat"]): r for r in rows}
                    self.assertEqual(set(by_flag), {0, 1})
                    self.assertEqual(by_flag[0]["input_tokens"], 10)
                    self.assertEqual(by_flag[0]["requests"], 1)
                    self.assertEqual(by_flag[1]["input_tokens"], 8)   # 3 + 5
                    self.assertEqual(by_flag[1]["output_tokens"], 10)  # 4 + 6
                    self.assertEqual(by_flag[1]["requests"], 2)
                finally:
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
