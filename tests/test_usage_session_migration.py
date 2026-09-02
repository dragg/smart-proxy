import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.db import Database

OLD_DDL = """
CREATE TABLE usage_session (
    session_id TEXT NOT NULL, proxy_key TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL, model TEXT NOT NULL,
    first_date TEXT NOT NULL, last_date TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0, cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, proxy_key, provider, model)
);
"""


class UsageSessionMigrationTests(unittest.TestCase):
    def test_fresh_db_has_new_shape(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "fresh.db"))
                await db.connect()
                cur = await db.db.execute("PRAGMA table_info(usage_session)")
                cols = {r["name"] for r in await cur.fetchall()}
                self.assertIn("request_kind", cols)
                self.assertIn("project", cols)
                self.assertIn("title", cols)
                await db.close()
        asyncio.run(run())

    def test_old_shape_rebuilds_once_preserving_rows(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                path = str(Path(td) / "old.db")
                # Seed an OLD-shape table with one row, before Database touches it.
                con = sqlite3.connect(path)
                con.executescript(OLD_DDL)
                con.execute(
                    "INSERT INTO usage_session VALUES "
                    "('s1','sp-k','anthropic','m','2026-07-01','2026-07-02',1,2,3,4,5,6,7,8)"
                )
                con.commit(); con.close()

                db = Database(path)
                await db.connect()  # runs migrations → rebuild
                cur = await db.db.execute("PRAGMA table_info(usage_session)")
                cols = {r["name"] for r in await cur.fetchall()}
                self.assertEqual({"request_kind", "project", "title"} & cols,
                                 {"request_kind", "project", "title"})
                cur = await db.db.execute(
                    "SELECT request_kind, project, title, input_tokens, requests "
                    "FROM usage_session WHERE session_id='s1'")
                row = await cur.fetchone()
                self.assertEqual(row["request_kind"], "unknown")
                self.assertEqual(row["project"], "")
                self.assertEqual(row["title"], "")
                self.assertEqual(int(row["input_tokens"]), 1)
                self.assertEqual(int(row["requests"]), 8)
                # Idempotent: second connect is a no-op (row still there).
                await db.close()
                db2 = Database(path)
                await db2.connect()
                cur = await db2.db.execute("SELECT COUNT(*) AS c FROM usage_session")
                self.assertEqual(int((await cur.fetchone())["c"]), 1)
                await db2.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
