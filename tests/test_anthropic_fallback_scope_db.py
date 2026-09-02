"""Storage for a fallback key's proxy-key scope (``anthropic_keys.allowed_proxy_keys``)."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database, parse_allowed_proxy_keys


async def _fresh(td: str) -> Database:
    db = Database(str(Path(td) / "smart-proxy.db"))
    await db.connect()
    return db


class AllowedProxyKeysColumnTests(unittest.TestCase):
    def test_new_key_defaults_to_an_empty_scope(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await _fresh(td)
                try:
                    await db.insert_anthropic_key(id="k1", key_type="api_key", api_key="sk-ant")
                    row = await db.get_anthropic_key("k1")
                    self.assertEqual(row["allowed_proxy_keys"], "[]")
                    self.assertEqual(parse_allowed_proxy_keys(row["allowed_proxy_keys"]), frozenset())
                finally:
                    await db.close()

        asyncio.run(run())

    def test_scope_round_trips_and_records_an_audit_event(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await _fresh(td)
                try:
                    await db.insert_anthropic_key(
                        id="k1", key_type="api_key", api_key="sk-ant", role="fallback")
                    ok = await db.set_anthropic_key_scope(
                        "k1", ["sp-bbb-full", "sp-aaa-full", "sp-bbb-full", "  "],
                        audit_source="dashboard", audit_event_type="scope_change",
                        audit_decision="set_scope",
                    )
                    self.assertTrue(ok)
                    row = await db.get_anthropic_key("k1")
                    # Deduplicated, blank-stripped, stable order.
                    self.assertEqual(json.loads(row["allowed_proxy_keys"]),
                                     ["sp-aaa-full", "sp-bbb-full"])

                    events = await db.list_anthropic_key_events("k1")
                    scope_events = [e for e in events if e["event_type"] == "scope_change"]
                    self.assertEqual(len(scope_events), 1)
                    context = json.loads(scope_events[0]["context_json"])
                    # Audit trail carries prefixes only — never key material.
                    self.assertEqual(context["next_scope"], ["sp-aaa-full", "sp-bbb-full"])
                    self.assertEqual(context["previous_scope"], [])
                finally:
                    await db.close()

        asyncio.run(run())

    def test_unknown_key_returns_false(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await _fresh(td)
                try:
                    self.assertFalse(await db.set_anthropic_key_scope("nope", ["sp-x"]))
                finally:
                    await db.close()

        asyncio.run(run())

    def test_migration_backfills_existing_rows(self) -> None:
        """A DB created before the column: the ALTER runs and old rows read '[]'."""
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                path = str(Path(td) / "old.db")
                db = Database(path)
                await db.connect()
                await db.db.execute("DROP TABLE anthropic_keys")
                await db.db.execute(
                    """CREATE TABLE anthropic_keys (
                        id TEXT PRIMARY KEY, key_type TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active', api_key TEXT,
                        access_token TEXT, refresh_token TEXT, client_id TEXT,
                        expires_at INTEGER, scopes TEXT DEFAULT '[]',
                        subscription_type TEXT DEFAULT '', rate_limit_tier TEXT DEFAULT '',
                        name TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
                )
                await db.db.execute(
                    "INSERT INTO anthropic_keys (id, key_type, created_at, updated_at) "
                    "VALUES ('legacy', 'api_key', '2026-01-01', '2026-01-01')"
                )
                await db.db.commit()
                await db.close()

                db = Database(path)
                await db.connect()   # re-runs migrations
                try:
                    row = await db.get_anthropic_key("legacy")
                    self.assertEqual(row["role"], "primary")
                    self.assertEqual(parse_allowed_proxy_keys(row["allowed_proxy_keys"]),
                                     frozenset())
                finally:
                    await db.close()

        asyncio.run(run())


class SnapshotRoundTripTests(unittest.TestCase):
    def test_role_and_scope_survive_an_export_import_cycle(self) -> None:
        """Without this, restoring a snapshot resurrects a scoped fallback key as
        an unscoped primary — a paid credential silently serving all traffic."""
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                source = Database(str(Path(td) / "source.db"))
                target = Database(str(Path(td) / "target.db"))
                await source.connect()
                await target.connect()
                try:
                    await source.insert_anthropic_key(
                        id="paid", key_type="api_key", api_key="sk-ant", role="fallback")
                    await source.set_anthropic_key_scope("paid", ["sp-consumer"])
                    await target.replace_snapshot(await source.export_snapshot())

                    row = await target.get_anthropic_key("paid")
                    self.assertEqual(row["role"], "fallback")
                    self.assertEqual(parse_allowed_proxy_keys(row["allowed_proxy_keys"]),
                                     frozenset({"sp-consumer"}))
                finally:
                    await source.close()
                    await target.close()

        asyncio.run(run())

    def test_snapshot_predating_the_columns_imports_with_safe_defaults(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                target = Database(str(Path(td) / "target.db"))
                await target.connect()
                try:
                    await target.replace_snapshot({
                        "anthropic_keys": [{
                            "id": "old", "key_type": "api_key", "status": "active",
                            "api_key": "sk", "access_token": None, "refresh_token": None,
                            "client_id": "c", "expires_at": None, "scopes": "[]",
                            "subscription_type": "", "rate_limit_tier": "", "name": "old",
                            "created_at": "2026-01-01", "updated_at": "2026-01-01",
                        }],
                    })
                    row = await target.get_anthropic_key("old")
                    self.assertEqual(row["role"], "primary")
                    self.assertEqual(row["allowed_proxy_keys"], "[]")
                finally:
                    await target.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
