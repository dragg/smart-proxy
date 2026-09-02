# tests/test_usage_kind_session_db.py
from __future__ import annotations
import asyncio, sys, tempfile, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import Database, build_database_from_config


class KindSessionTables(unittest.IsolatedAsyncioTestCase):
    async def _db(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await db.connect()
        self.addAsyncCleanup(db.close)
        return db

    async def asyncTearDown(self):
        t = getattr(self, "_tmp", None)
        if t: t.cleanup()

    async def test_kind_upsert_accumulates(self):
        db = await self._db()
        row = ("2026-07-18", "sp-a", "subagent", "anthropic", "m", 10, 5, 0, 0, 0, 0, 0, 1)
        await db.upsert_usage_kind_batch([row])
        await db.upsert_usage_kind_batch([row])
        cur = await db.db.execute(
            "SELECT input_tokens, requests FROM usage_kind_daily WHERE request_kind='subagent'")
        r = await cur.fetchone()
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (20, 2))

    async def test_session_upsert_tracks_first_last_and_sums(self):
        db = await self._db()
        await db.upsert_usage_session_batch(
            [("sess-1", "sp-a", "unknown", "anthropic", "m", "2026-07-10", "2026-07-10", "", "",
              10, 0, 0, 0, 0, 0, 0, 1)])
        await db.upsert_usage_session_batch(
            [("sess-1", "sp-a", "unknown", "anthropic", "m", "2026-07-12", "2026-07-12", "", "",
              5, 0, 0, 0, 0, 0, 0, 1)])
        cur = await db.db.execute(
            "SELECT first_date, last_date, input_tokens, requests FROM usage_session WHERE session_id='sess-1'")
        r = await cur.fetchone()
        self.assertEqual(r["first_date"], "2026-07-10")
        self.assertEqual(r["last_date"], "2026-07-12")
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (15, 2))


class SessionUpsertKindLabelTests(unittest.TestCase):
    def _row(self, kind, first, last, project, title, inp, req):
        # canonical 17-col order
        return ("s1", "sp-k", kind, "anthropic", "m", first, last, project, title,
                inp, 0, 0, 0, 0, 0, 0, req)

    def test_kind_in_key_and_label_last_write_wins_nonempty(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                # main row establishes the label; a later main row updates it;
                # a subagent row for the same session must NOT clobber it and
                # is a distinct PK row.
                await db.upsert_usage_session_batch([self._row("main", "2026-07-05", "2026-07-05", "smart-proxy", "first ask", 10, 1)])
                await db.upsert_usage_session_batch([self._row("main", "2026-07-02", "2026-07-06", "smart-proxy", "second ask", 5, 1)])
                await db.upsert_usage_session_batch([self._row("subagent", "2026-07-05", "2026-07-05", "", "", 100, 3)])

                cur = await db.db.execute(
                    "SELECT request_kind, first_date, last_date, project, title, input_tokens, requests "
                    "FROM usage_session WHERE session_id='s1' ORDER BY request_kind")
                rows = [dict(r) for r in await cur.fetchall()]
                self.assertEqual([r["request_kind"] for r in rows], ["main", "subagent"])
                main = rows[0]
                self.assertEqual(main["project"], "smart-proxy")
                self.assertEqual(main["title"], "second ask")       # last non-empty wins
                self.assertEqual(main["first_date"], "2026-07-02")  # MIN
                self.assertEqual(main["last_date"], "2026-07-06")   # MAX
                self.assertEqual(int(main["input_tokens"]), 15)     # additive
                self.assertEqual(int(main["requests"]), 2)
                sub = rows[1]
                self.assertEqual((sub["project"], sub["title"]), ("", ""))
                self.assertEqual(int(sub["input_tokens"]), 100)
                await db.close()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
