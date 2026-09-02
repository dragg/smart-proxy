# tests/test_anthropic_key_rename_db.py
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tests.db_test_utils import connect_test_database  # noqa: E402


class SetAnthropicKeyNameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = await connect_test_database(
            sqlite_fallback_path=str(Path(self._tmp.name) / "smart-proxy.db")
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_rename_updates_name_and_updated_at(self) -> None:
        await self.db.insert_anthropic_key(
            id="k-rename-1", key_type="oauth",
            access_token="at", refresh_token="rt", name="old-name",
        )
        before = await self.db.get_anthropic_key("k-rename-1")
        ok = await self.db.set_anthropic_key_name("k-rename-1", "new-name")
        self.assertTrue(ok)
        row = await self.db.get_anthropic_key("k-rename-1")
        self.assertEqual(row["name"], "new-name")
        self.assertGreaterEqual(row["updated_at"], before["updated_at"])

    async def test_rename_unknown_id_returns_false(self) -> None:
        ok = await self.db.set_anthropic_key_name("missing-id", "x")
        self.assertFalse(ok)

    async def test_rename_with_audit_records_event(self) -> None:
        await self.db.insert_anthropic_key(
            id="k-rename-audit", key_type="oauth",
            access_token="at", refresh_token="rt", name="old-name",
        )
        ok = await self.db.set_anthropic_key_name(
            "k-rename-audit", "new-name",
            audit_op_id="op-rename-1",
            audit_source="dashboard",
            audit_event_type="dashboard_rename",
            audit_decision="rename",
            audit_error_type="manual_action",
            audit_error_message="Renamed to 'new-name' via dashboard",
        )
        self.assertTrue(ok)

        events = await self.db.list_anthropic_key_events("k-rename-audit")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "dashboard_rename")
        self.assertEqual(events[0]["source"], "dashboard")
        self.assertEqual(events[0]["decision"], "rename")
        self.assertIn("Renamed to", events[0]["error_message"])

    def test_role_defaults_primary_and_set_role_records_event(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    await db.insert_anthropic_key(id="k1", key_type="oauth", name="k1")
                    row = await db.get_anthropic_key("k1")
                    self.assertEqual(row["role"], "primary")

                    await db.insert_anthropic_key(id="k2", key_type="oauth", name="k2", role="standby")
                    self.assertEqual((await db.get_anthropic_key("k2"))["role"], "standby")

                    ok = await db.set_anthropic_key_role(
                        "k1", "standby", audit_source="dashboard",
                        audit_event_type="role_change", audit_decision="set_standby")
                    self.assertTrue(ok)
                    self.assertEqual((await db.get_anthropic_key("k1"))["role"], "standby")
                    events = await db.list_anthropic_key_events("k1")
                    self.assertTrue(any(e["event_type"] == "role_change" for e in events))
                    self.assertFalse(await db.set_anthropic_key_role("missing", "standby"))
                finally:
                    await db.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
