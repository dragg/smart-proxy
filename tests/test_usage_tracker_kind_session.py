# tests/test_usage_tracker_kind_session.py
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import build_database_from_config
from smart_proxy.usage import UsageTracker


class TrackerKindSession(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_flush_writes_kind_and_session(self):
        t = UsageTracker()
        t.record("sp-a", "cred", "anthropic", "m", 10, 5,
                 request_kind="subagent", session_id="sess-1")
        t.record("sp-a", "cred", "anthropic", "m", 4, 2,
                 request_kind="subagent", session_id="sess-1")
        rows = await t.flush(self.db)
        self.assertEqual(len(rows), 1)  # usage_daily contract unchanged
        cur = await self.db.db.execute(
            "SELECT input_tokens, requests FROM usage_kind_daily WHERE request_kind='subagent'")
        r = await cur.fetchone()
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (14, 2))
        cur = await self.db.db.execute(
            "SELECT input_tokens, requests FROM usage_session WHERE session_id='sess-1' AND proxy_key='sp-a'")
        r = await cur.fetchone()
        self.assertEqual((int(r["input_tokens"]), int(r["requests"])), (14, 2))

    async def test_empty_session_id_skips_session_table(self):
        t = UsageTracker()
        t.record("sp-a", "cred", "anthropic", "m", 1, 1, request_kind="helper", session_id="")
        await t.flush(self.db)
        cur = await self.db.db.execute("SELECT COUNT(*) AS n FROM usage_session")
        self.assertEqual(int((await cur.fetchone())["n"]), 0)
        cur = await self.db.db.execute(
            "SELECT COUNT(*) AS n FROM usage_kind_daily WHERE request_kind='helper'")
        self.assertEqual(int((await cur.fetchone())["n"]), 1)

    def test_record_session_buffers_kind_and_label(self):
        from smart_proxy.usage import UsageTracker
        t = UsageTracker()
        t.record("sp-k", "cred", "anthropic", "m", 10, 2,
                 request_kind="main", session_id="s1", project="smart-proxy", title="hello")
        t.record("sp-k", "cred", "anthropic", "m", 5, 1,
                 request_kind="subagent", session_id="s1")  # no label
        # two buffer entries: one per (session, kind)
        keys = sorted(t._session_buf.keys())
        assert keys == [("s1", "sp-k", "main", "anthropic", "m"),
                        ("s1", "sp-k", "subagent", "anthropic", "m")]
        main = t._session_buf[("s1", "sp-k", "main", "anthropic", "m")]
        # [first_date, last_date, project, title, 8 counters]
        assert main[2] == "smart-proxy"
        assert main[3] == "hello"
        assert main[4] == 10   # input_tokens
        sub = t._session_buf[("s1", "sp-k", "subagent", "anthropic", "m")]
        assert (sub[2], sub[3]) == ("", "")
