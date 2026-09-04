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


class DatabaseSnapshotRoundTripTests(unittest.TestCase):
    def test_export_snapshot_round_trips_all_tables(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                source = Database(str(Path(td) / "source.db"))
                target = Database(str(Path(td) / "target.db"))
                await source.connect()
                await target.connect()
                try:
                    await source.add_proxy_key("sp-roundtrip", "roundtrip")
                    await source.upsert_usage_batch(
                        [
                            (
                                "2026-04-04",
                                "sp-roundtrip",
                                "group-a",
                                "cred-1",
                                "openai",
                                "gpt-5.4",
                                1,  # via_openai_compat
                                10,
                                20,
                                1,
                                2,
                                3,
                                4,
                                5,
                                6,
                            )
                        ]
                    )
                    await source.upsert_model_price(
                        model_prefix="gpt-5.4",
                        provider="openai",
                        input_price=1.5,
                        output_price=2.5,
                        cache_read_price=0.1,
                        cache_write_5m_price=0.2,
                        cache_write_1h_price=0.3,
                    )
                    await source.insert_anthropic_key(
                        id="anth-1",
                        key_type="oauth",
                        access_token="access-token",
                        refresh_token="refresh-token",
                        client_id="client-id",
                        expires_at=1712188800000,
                        scopes='["user:profile","user:inference"]',
                        subscription_type="pro",
                        rate_limit_tier="tier-1",
                        name="anthropic-key",
                    )
                    await source.set_anthropic_key_status("anth-1", "low_balance")
                    await source.record_rate_limit(
                        provider="anthropic",
                        credential_id="anth-1",
                        retry_after=60,
                        reset_at="2026-04-04T01:00:00+00:00",
                        limit_type="five_hour",
                        utilization_5h=0.75,
                        utilization_7d=0.25,
                    )
                    await source.upsert_usage_kind_batch(
                        [
                            (
                                "2026-04-04",
                                "sp-roundtrip",
                                "main",
                                "openai",
                                "gpt-5.4",
                                10, 20, 1, 2, 3, 4, 5, 6,
                            )
                        ]
                    )
                    await source.upsert_usage_session_batch(
                        [
                            (
                                "sess-roundtrip",
                                "sp-roundtrip",
                                "main",
                                "openai",
                                "gpt-5.4",
                                "2026-04-04",
                                "2026-04-04",
                                "smart-proxy",
                                "roundtrip",
                                10, 20, 1, 2, 3, 4, 5, 6,
                            )
                        ]
                    )

                    await source.upsert_usage_bucket_batch(
                        [
                            (
                                "2026-04-04T07",
                                "sp-roundtrip",
                                "group-a",
                                "cred-1",
                                "openai",
                                "gpt-5.4",
                                1,  # via_openai_compat
                                "main",
                                10, 20, 1, 2, 3, 4, 5, 6,
                            )
                        ]
                    )

                    snapshot = await source.export_snapshot()
                    await target.replace_snapshot(snapshot)
                    roundtrip = await target.export_snapshot()

                    self.assertEqual(roundtrip, snapshot)
                    self.assertTrue(snapshot["usage_bucket"], "table must be exported")
                finally:
                    await source.close()
                    await target.close()

        asyncio.run(run())

    def test_usage_daily_snapshot_keeps_via_openai_compat(self) -> None:
        """`via_openai_compat` is a PK member and must survive a restore.

        It was missing from `SNAPSHOT_TABLE_SPECS`, so export dropped it and
        restore wrote the column default 0: a compat row came back as native
        traffic, and a native+compat pair for one (date, key, cred, provider,
        model) collided on the primary key instead of restoring. The
        all-tables round-trip test cannot see this -- it compares an export to
        an export, and both sides are equally wrong -- so this one reads the
        restored column back out of the target.
        """

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                source = Database(str(Path(td) / "source.db"))
                target = Database(str(Path(td) / "target.db"))
                await source.connect()
                await target.connect()
                try:
                    await source.add_proxy_key("sp-compat", "compat")
                    # A native and a compat row that differ ONLY in the flag.
                    await source.upsert_usage_batch(
                        [
                            (
                                "2026-04-04", "sp-compat", "group-a", "cred-1",
                                "openai", "gpt-5.4", 0,
                                10, 20, 0, 0, 0, 0, 0, 1,
                            ),
                            (
                                "2026-04-04", "sp-compat", "group-a", "cred-1",
                                "openai", "gpt-5.4", 1,
                                30, 40, 0, 0, 0, 0, 0, 1,
                            ),
                        ]
                    )

                    # Must not raise a duplicate-key error.
                    await target.replace_snapshot(await source.export_snapshot())

                    cur = await target.db.execute(
                        "SELECT via_openai_compat, input_tokens FROM usage_daily"
                        " ORDER BY via_openai_compat"
                    )
                    rows = [dict(r) for r in await cur.fetchall()]
                    self.assertEqual(
                        [(r["via_openai_compat"], r["input_tokens"]) for r in rows],
                        [(0, 10), (1, 30)],
                        "the compat flag must survive the round trip",
                    )
                finally:
                    await source.close()
                    await target.close()

        asyncio.run(run())

    def test_replace_snapshot_clears_preexisting_kind_and_session_rows(self) -> None:
        """`replace_snapshot` must clear pre-existing usage_kind_daily/usage_session
        rows before re-inserting, not just append.

        Regression coverage for the SNAPSHOT_DELETE_ORDER gap: those two tables
        were added to SNAPSHOT_TABLE_SPECS (so replace_snapshot INSERTs into
        them) without also being added to SNAPSHOT_DELETE_ORDER (so the prior
        DELETE FROM pass never touched them). On a target DB that already has
        rows -- including a row sharing a PK with an incoming snapshot row --
        that produced a duplicate-key error instead of a clean overwrite.
        """

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                source = Database(str(Path(td) / "source.db"))
                target = Database(str(Path(td) / "target.db"))
                await source.connect()
                await target.connect()
                try:
                    await source.add_proxy_key("sp-restore", "restore")
                    # Snapshot to restore: one kind row and one session row.
                    await source.upsert_usage_kind_batch(
                        [
                            (
                                "2026-04-04",
                                "sp-restore",
                                "main",
                                "anthropic",
                                "claude-x",
                                100, 200, 1, 2, 3, 4, 5, 6,
                            )
                        ]
                    )
                    await source.upsert_usage_session_batch(
                        [
                            (
                                "sess-restore",
                                "sp-restore",
                                "main",
                                "anthropic",
                                "claude-x",
                                "2026-04-04",
                                "2026-04-04",
                                "smart-proxy",
                                "restore",
                                100, 200, 1, 2, 3, 4, 5, 6,
                            )
                        ]
                    )
                    snapshot = await source.export_snapshot()

                    # Target already has rows before the restore: one that
                    # shares its PK with an incoming snapshot row (so a
                    # missed DELETE would raise a duplicate-key error on
                    # INSERT) and one with a disjoint PK (so a missed DELETE
                    # would instead show up as a leftover row afterwards).
                    await target.add_proxy_key("sp-restore", "restore")
                    await target.add_proxy_key("sp-stale", "stale")
                    await target.upsert_usage_kind_batch(
                        [
                            (
                                "2026-04-04", "sp-restore", "main",
                                "anthropic", "claude-x",
                                999, 999, 0, 0, 0, 0, 0, 1,
                            ),
                            (
                                "2026-01-01", "sp-stale", "background",
                                "openai", "gpt-old",
                                1, 1, 0, 0, 0, 0, 0, 1,
                            ),
                        ]
                    )
                    await target.upsert_usage_session_batch(
                        [
                            (
                                "sess-restore", "sp-restore", "main", "anthropic", "claude-x",
                                "2026-01-01", "2026-01-01", "smart-proxy", "stale-restore",
                                999, 999, 0, 0, 0, 0, 0, 1,
                            ),
                            (
                                "sess-stale", "sp-stale", "main", "openai", "gpt-old",
                                "2026-01-01", "2026-01-01", "other-proj", "stale",
                                1, 1, 0, 0, 0, 0, 0, 1,
                            ),
                        ]
                    )

                    await target.upsert_usage_bucket_batch(
                        [
                            (
                                "2026-01-01T00", "sp-stale", None, "cred-stale",
                                "openai", "gpt-old", 0, "main",
                                1, 1, 0, 0, 0, 0, 0, 1,
                            )
                        ]
                    )

                    # Must not raise (duplicate-key on the shared-PK row) and
                    # must fully replace the target's prior contents.
                    await target.replace_snapshot(snapshot)

                    restored = await target.export_snapshot()
                    self.assertEqual(restored["usage_kind_daily"], snapshot["usage_kind_daily"])
                    self.assertEqual(restored["usage_session"], snapshot["usage_session"])
                    self.assertEqual(restored["usage_bucket"], snapshot["usage_bucket"])
                finally:
                    await source.close()
                    await target.close()

        asyncio.run(run())

    def test_snapshot_roundtrip_new_columns(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "a.db"))
                await db.connect()
                await db.upsert_usage_session_batch([
                    ("s1","sp-k","main","anthropic","m","2026-07-01","2026-07-02","smart-proxy","hi",10,0,0,0,0,0,0,4),
                ])
                snap = await db.export_snapshot()
                db2 = Database(str(Path(td) / "b.db"))
                await db2.connect()
                await db2.replace_snapshot(snap)
                cur = await db2.db.execute(
                    "SELECT request_kind, project, title FROM usage_session WHERE session_id='s1'")
                row = await cur.fetchone()
                assert (row["request_kind"], row["project"], row["title"]) == ("main", "smart-proxy", "hi")
                await db.close(); await db2.close()
        asyncio.run(run())

    def test_snapshot_import_defaults_missing_new_columns(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "c.db"))
                await db.connect()
                # Old-shape snapshot row: no request_kind/project/title keys.
                old_snap = {"usage_session": [{
                    "session_id": "s9", "proxy_key": "sp-k", "provider": "anthropic",
                    "model": "m", "first_date": "2026-07-01", "last_date": "2026-07-01",
                    "input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
                    "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 1,
                }]}
                await db.replace_snapshot(old_snap)  # must not raise
                cur = await db.db.execute(
                    "SELECT request_kind, project, title FROM usage_session WHERE session_id='s9'")
                row = await cur.fetchone()
                assert (row["request_kind"], row["project"], row["title"]) == ("unknown", "", "")
                await db.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
