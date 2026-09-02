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
from tests.db_test_utils import connect_test_database


class AnthropicDatabaseContractTests(unittest.TestCase):
    def test_set_anthropic_key_status_records_audit_snapshot_and_event(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="audit-key",
                        key_type="oauth",
                        access_token="access-token",
                        refresh_token="refresh-token",
                        client_id="client-id-123",
                        expires_at=123456789,
                        name="audit-key",
                    )

                    await db.set_anthropic_key_status(
                        "audit-key",
                        "inactive",
                        audit_op_id="op-status-1",
                        audit_source="manual_cli",
                        audit_event_type="status_change",
                        audit_decision="deactivate",
                        audit_error_type="manual_action",
                        audit_error_message="Deactivated via CLI for investigation",
                        audit_context={"actor": "operator"},
                    )

                    snapshots = await db.list_anthropic_key_snapshots("audit-key")
                    events = await db.list_anthropic_key_events("audit-key")

                    self.assertEqual(len(snapshots), 1)
                    self.assertEqual(snapshots[0]["snapshot_kind"], "before_status_change")
                    self.assertEqual(snapshots[0]["trigger_event_type"], "status_change")
                    self.assertEqual(snapshots[0]["status"], "active")
                    self.assertEqual(snapshots[0]["access_token"], "access-token")
                    self.assertEqual(snapshots[0]["refresh_token"], "refresh-token")

                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["op_id"], "op-status-1")
                    self.assertEqual(events[0]["source"], "manual_cli")
                    self.assertEqual(events[0]["event_type"], "status_change")
                    self.assertEqual(events[0]["decision"], "deactivate")
                    self.assertEqual(events[0]["snapshot_id"], snapshots[0]["id"])
                    self.assertEqual(events[0]["error_type"], "manual_action")
                    self.assertIn("Deactivated via CLI", events[0]["error_message"])
                    self.assertIn('"actor": "operator"', events[0]["context_json"])
                finally:
                    await db.close()

        asyncio.run(run())

    def test_get_low_balance_anthropic_keys_returns_named_rows(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-low-balance",
                        key_type="oauth",
                        access_token="access-token",
                        refresh_token="refresh-token",
                        name="lb-key",
                    )
                    await db.set_anthropic_key_status("key-low-balance", "low_balance")

                    rows = await db.get_low_balance_anthropic_keys()

                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["id"], "key-low-balance")
                    self.assertEqual(rows[0]["key_type"], "oauth")
                    self.assertEqual(rows[0]["access_token"], "access-token")
                    self.assertEqual(rows[0]["refresh_token"], "refresh-token")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_activate_anthropic_key_by_prefix_activates_exact_match(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="abc12345-key",
                        key_type="oauth",
                        access_token="access-token",
                        refresh_token="refresh-token",
                        name="prefix-key",
                    )
                    await db.set_anthropic_key_status("abc12345-key", "inactive")

                    full_id = await db.activate_anthropic_key_by_prefix("abc123")

                    self.assertEqual(full_id, "abc12345-key")
                    row = await db.get_anthropic_key("abc12345-key")
                    assert row is not None
                    self.assertEqual(row["status"], "active")
                finally:
                    await db.close()

        asyncio.run(run())

    def test_activate_anthropic_key_by_prefix_returns_none_for_ambiguous_prefix(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="shared-1111",
                        key_type="oauth",
                        access_token="token-1",
                        refresh_token="refresh-1",
                        name="shared-1",
                    )
                    await db.insert_anthropic_key(
                        id="shared-2222",
                        key_type="oauth",
                        access_token="token-2",
                        refresh_token="refresh-2",
                        name="shared-2",
                    )
                    await db.set_anthropic_key_status("shared-1111", "inactive")
                    await db.set_anthropic_key_status("shared-2222", "inactive")

                    full_id = await db.activate_anthropic_key_by_prefix("shared-")

                    self.assertIsNone(full_id)
                    first = await db.get_anthropic_key("shared-1111")
                    second = await db.get_anthropic_key("shared-2222")
                    assert first is not None
                    assert second is not None
                    self.assertEqual(first["status"], "inactive")
                    self.assertEqual(second["status"], "inactive")
                finally:
                    await db.close()

        asyncio.run(run())

class RecoveryRepersistContractTests(unittest.TestCase):
    """The reconciler's audit event must round-trip on the real schema."""

    def test_recovery_repersist_event_is_accepted(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-repersist", key_type="oauth",
                        access_token="a0", refresh_token="r0", client_id="c",
                        expires_at=1_700_000_000_000, scopes='["user:inference"]',
                        name="repersist",
                    )
                    await db.update_anthropic_oauth_tokens(
                        "key-repersist", "a1", 1_800_000_000_000, "r1",
                        audit_source="db_recovery",
                        audit_event_type="recovery_repersist",
                        audit_decision="update_tokens",
                        audit_context={"reason": "breaker_closed"},
                    )

                    row = await db.get_anthropic_key("key-repersist")
                    assert row is not None
                    self.assertEqual(row["refresh_token"], "r1")
                    self.assertEqual(row["expires_at"], 1_800_000_000_000)

                    events = [
                        e["event_type"]
                        for e in await db.list_anthropic_key_events("key-repersist")
                    ]
                    self.assertIn("recovery_repersist", events)
                finally:
                    await db.db.execute(
                        "DELETE FROM anthropic_key_events WHERE key_id = ?",
                        ("key-repersist",),
                    )
                    await db.db.execute(
                        "DELETE FROM anthropic_key_snapshots WHERE key_id = ?",
                        ("key-repersist",),
                    )
                    await db.db.execute(
                        "DELETE FROM anthropic_keys WHERE id = ?", ("key-repersist",)
                    )
                    await db.db.commit()
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
