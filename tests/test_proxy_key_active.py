from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import build_database_from_config


class SetProxyKeyActiveTests(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        return db

    async def asyncTearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    async def test_disable_then_enable_by_prefix(self) -> None:
        db = await self._db()
        await db.add_proxy_key("sp-alpha-123", "Alpha")

        full = await db.set_proxy_key_active("sp-alpha", False)
        self.assertEqual(full, "sp-alpha-123")
        row = (await db.list_proxy_keys())[0]
        self.assertEqual(int(row["active"]), 0)

        full = await db.set_proxy_key_active("sp-alpha", True)
        self.assertEqual(full, "sp-alpha-123")
        row = (await db.list_proxy_keys())[0]
        self.assertEqual(int(row["active"]), 1)

    async def test_ambiguous_prefix_returns_none(self) -> None:
        db = await self._db()
        await db.add_proxy_key("sp-a-1", "A1")
        await db.add_proxy_key("sp-a-2", "A2")
        self.assertIsNone(await db.set_proxy_key_active("sp-a", False))

    async def test_unknown_prefix_returns_none(self) -> None:
        db = await self._db()
        self.assertIsNone(await db.set_proxy_key_active("sp-missing", True))


class SetProxyKeyActiveByCreatedAtTests(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        return db

    async def asyncTearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp is not None:
            tmp.cleanup()

    async def _insert(self, db, key, name, created_at) -> None:
        await db.db.execute(
            "INSERT INTO proxy_api_keys (key, name, active, created_at) VALUES (?, ?, 1, ?)",
            (key, name, created_at),
        )
        await db.db.commit()

    async def test_created_at_disambiguates_prefix_colliding_keys(self) -> None:
        # Two keys share every char but the last, so prefix matching is
        # ambiguous — created_at is the stable identity.
        db = await self._db()
        alpha_ts = "2020-01-01T00:00:00+00:00"
        beta_ts = "2020-01-02T00:00:00+00:00"
        await self._insert(db, "sp-deadbeefc0000000000000000000000A", "alpha", alpha_ts)
        await self._insert(db, "sp-deadbeefc0000000000000000000000B", "beta", beta_ts)

        # Prefix is ambiguous for these two -> None.
        self.assertIsNone(await db.set_proxy_key_active("sp-deadbeefc", False))

        # created_at disambiguates: disable only beta.
        full = await db.set_proxy_key_active_by_created_at(beta_ts, False)
        self.assertEqual(full, "sp-deadbeefc0000000000000000000000B")
        by_name = {r["name"]: int(r["active"]) for r in await db.list_proxy_keys()}
        self.assertEqual(by_name["beta"], 0)
        self.assertEqual(by_name["alpha"], 1)

    async def test_unknown_created_at_returns_none(self) -> None:
        db = await self._db()
        self.assertIsNone(
            await db.set_proxy_key_active_by_created_at("1999-01-01T00:00:00+00:00", True))


if __name__ == "__main__":
    unittest.main()
