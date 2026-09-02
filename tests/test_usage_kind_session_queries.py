# tests/test_usage_kind_session_queries.py
from __future__ import annotations
import asyncio, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.db import Database, build_database_from_config

from tests.db_test_utils import connect_test_database


class Queries(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = build_database_from_config(database_url="", db_path=f"{self._tmp.name}/k.db")
        await self.db.connect()
        await self.db.add_proxy_key("sp-alpha-key", "Petya")
        await self.db.upsert_usage_kind_batch([
            ("2026-07-18", "sp-alpha-key", "subagent", "anthropic", "m", 100, 0, 0, 0, 0, 0, 0, 3),
            ("2026-07-18", "sp-alpha-key", "main", "anthropic", "m", 50, 0, 0, 0, 0, 0, 0, 2),
        ])
        await self.db.upsert_usage_session_batch([
            ("sess-big", "sp-alpha-key", "main", "anthropic", "m", "2026-07-10", "2026-07-18", "", "", 999, 0, 0, 0, 0, 0, 0, 40),
            ("sess-small", "sp-alpha-key", "main", "anthropic", "m", "2026-07-18", "2026-07-18", "", "", 5, 0, 0, 0, 0, 0, 0, 1),
        ])

    async def asyncTearDown(self):
        await self.db.close()
        self._tmp.cleanup()

    async def test_by_kind_joins_key_name(self):
        rows = await self.db.query_usage_by_kind("2026-07-01", "2026-07-31")
        kinds = {r["request_kind"]: r for r in rows}
        self.assertEqual(kinds["subagent"]["key_name"], "Petya")
        self.assertEqual(int(kinds["subagent"]["input_tokens"]), 100)

    async def test_top_sessions_ordered_and_named(self):
        rows = await self.db.query_top_sessions(limit=10)
        self.assertEqual(rows[0]["session_id"], "sess-big")
        self.assertEqual(rows[0]["key_name"], "Petya")
        self.assertGreater(int(rows[0]["input_tokens"]), int(rows[1]["input_tokens"]))

    async def test_top_sessions_ranks_whole_sessions_not_per_model_rows(self):
        """LIMIT must bound sessions, not per-model rows.

        ``sess-multi`` uses two models whose individual token rows (600 and
        550) are each smaller than ``sess-single-large``'s one row (1000),
        but its combined total (1150) is the largest of all sessions
        seeded (including the "sess-big"/"sess-small" rows from
        asyncSetUp, total 999 and 5). With limit=2, the correct top-2
        SESSIONS by combined total are sess-multi (1150) and
        sess-single-large (1000) -- and sess-multi's BOTH model rows must
        come back (no truncation).

        Under the old per-row-LIMIT query, ranking by individual row token
        counts desc gives: sess-single-large (1000), sess-big (999),
        sess-multi/model-a (600), sess-multi/model-b (550), sess-tiny (10),
        sess-small (5) -- so a LIMIT 2 at the row grain would return
        {sess-single-large, sess-big} and drop sess-multi entirely, which
        is both a wrong-session-selected bug and a truncation bug.
        """
        await self.db.upsert_usage_session_batch([
            ("sess-multi", "sp-alpha-key", "main", "anthropic", "model-a",
             "2026-07-01", "2026-07-02", "", "", 600, 0, 0, 0, 0, 0, 0, 5),
            ("sess-multi", "sp-alpha-key", "main", "anthropic", "model-b",
             "2026-07-01", "2026-07-02", "", "", 550, 0, 0, 0, 0, 0, 0, 5),
            ("sess-single-large", "sp-alpha-key", "main", "anthropic", "model-a",
             "2026-07-01", "2026-07-02", "", "", 1000, 0, 0, 0, 0, 0, 0, 10),
            ("sess-tiny", "sp-alpha-key", "main", "anthropic", "model-a",
             "2026-07-01", "2026-07-02", "", "", 10, 0, 0, 0, 0, 0, 0, 1),
        ])

        rows = await self.db.query_top_sessions(limit=2)

        sessions: dict[str, list[dict]] = {}
        for row in rows:
            sessions.setdefault(row["session_id"], []).append(row)

        self.assertEqual(set(sessions.keys()), {"sess-multi", "sess-single-large"})
        # Both of sess-multi's per-model rows must be present -- no
        # row-level LIMIT truncation.
        self.assertEqual(len(sessions["sess-multi"]), 2)
        self.assertEqual(
            {row["model"] for row in sessions["sess-multi"]},
            {"model-a", "model-b"},
        )
        multi_total = sum(int(row["input_tokens"]) for row in sessions["sess-multi"])
        self.assertEqual(multi_total, 1150)

    def test_top_sessions_returns_per_kind_rows_with_label(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                db = Database(str(Path(td) / "t.db"))
                await db.connect()
                await db.upsert_usage_session_batch([
                    ("s1","sp-k","main","anthropic","m","2026-07-01","2026-07-02","smart-proxy","opening ask",10,0,0,0,0,0,0,4),
                    ("s1","sp-k","subagent","anthropic","m","2026-07-01","2026-07-01","","",100,0,0,0,0,0,0,20),
                ])
                rows = await db.query_top_sessions(50)
                kinds = sorted(r["request_kind"] for r in rows)
                assert kinds == ["main", "subagent"]
                main = next(r for r in rows if r["request_kind"] == "main")
                assert main["project"] == "smart-proxy"
                assert main["title"] == "opening ask"
                await db.close()
        asyncio.run(run())


class BuildSessionsJsonTests(unittest.TestCase):
    def test_build_sessions_json_groups_kinds_and_label(self):
        from smart_proxy.usage_dashboard import build_sessions_json
        rows = [
            {"session_id": "s1", "proxy_key": "sp-k", "key_name": "nikolai",
             "request_kind": "main", "provider": "anthropic", "model": "m",
             "project": "smart-proxy", "title": "opening ask",
             "first_date": "2026-07-01", "last_date": "2026-07-02",
             "input_tokens": 10, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 4},
            {"session_id": "s1", "proxy_key": "sp-k", "key_name": "nikolai",
             "request_kind": "subagent", "provider": "anthropic", "model": "m",
             "project": "", "title": "",
             "first_date": "2026-07-01", "last_date": "2026-07-01",
             "input_tokens": 100, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "cache_creation_5m_tokens": 0,
             "cache_creation_1h_tokens": 0, "web_search_requests": 0, "requests": 20},
        ]
        from smart_proxy.usage import build_price_lookup
        prices = build_price_lookup([])  # unpriced ⇒ cost unknown; we only assert structure here
        out = build_sessions_json(rows, prices)
        assert len(out) == 1
        s = out[0]
        assert s["project"] == "smart-proxy"
        assert s["title"] == "opening ask"
        assert s["requests"] == 24
        kinds = {k["request_kind"]: k for k in s["kinds"]}
        assert set(kinds) == {"main", "subagent"}
        assert kinds["main"]["requests"] == 4
        assert kinds["subagent"]["input_tokens"] == 100

    def test_build_sessions_json_lenient_missing_kind(self):
        from smart_proxy.usage_dashboard import build_sessions_json
        from smart_proxy.usage import build_price_lookup
        rows = [{"session_id": "s2", "proxy_key": "k", "model": "m",
                 "input_tokens": 1, "requests": 1}]  # no request_kind
        out = build_sessions_json(rows, build_price_lookup([]))
        assert out[0]["kinds"][0]["request_kind"] == "unknown"


class QueryTopSessionsPostgresTests(unittest.IsolatedAsyncioTestCase):
    """Postgres-backend regression for the new ``query_top_sessions`` JOIN.

    The rewritten query joins ``usage_session`` to a grouped-and-LIMITed
    subquery (rather than a row-value ``(session_id, proxy_key) IN (...)``
    predicate, which isn't portable) and adds ``top.total_tokens`` to the
    outer GROUP BY so the ORDER BY can reference it. Postgres -- unlike
    sqlite -- strictly enforces that every non-aggregated column referenced
    outside GROUP BY be functionally dependent on it, so this is the
    backend most likely to reject a subtly wrong version of this query.
    Skips gracefully if no local Postgres is configured.
    """

    _SESSION_PREFIX = "sess-pg-top-sessions-regression"
    _PROXY_KEY = "sp-pg-top-sessions-regression"

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = await connect_test_database(
            sqlite_fallback_path=f"{self._tmp.name}/k.db"
        )
        if self.db._backend != "postgres":
            self.skipTest(
                "no local Postgres configured (TEST_DATABASE_URL unset) -- this "
                "test needs the real Postgres GROUP BY/ORDER BY rules to "
                "exercise the rewritten query_top_sessions subquery"
            )
        await self.db.db.execute(
            "DELETE FROM usage_session WHERE session_id LIKE ?",
            (f"{self._SESSION_PREFIX}%",),
        )
        await self.db.db.commit()

    async def asyncTearDown(self) -> None:
        if getattr(self, "db", None) is not None and self.db._backend == "postgres":
            await self.db.db.execute(
                "DELETE FROM usage_session WHERE session_id LIKE ?",
                (f"{self._SESSION_PREFIX}%",),
            )
            await self.db.db.commit()
        if getattr(self, "db", None) is not None:
            await self.db.close()
        self._tmp.cleanup()

    async def test_top_sessions_ranks_whole_sessions_on_postgres(self) -> None:
        """Exercise the rewritten JOIN-to-subquery + GROUP BY/ORDER BY on a
        real Postgres server (syntax and column-dependency rules differ
        from sqlite's lenient GROUP BY), and confirm a multi-model session
        that falls within the (generous) LIMIT comes back with ALL of its
        per-model rows -- no truncation.

        Uses a large limit rather than asserting an exact top-N selection:
        this runs against a shared ``smart_proxy_tests`` Postgres database that
        may carry sessions from other tests/runs, so ranking this test's
        three sessions against unknown pre-existing totals would be flaky.
        The row-level LIMIT-truncation behavior (this finding's core bug)
        is already locked deterministically by the sqlite test above; this
        test's job is just to prove the same query is valid, portable SQL
        on Postgres.
        """
        multi_id = f"{self._SESSION_PREFIX}-multi"
        single_id = f"{self._SESSION_PREFIX}-single-large"
        tiny_id = f"{self._SESSION_PREFIX}-tiny"
        await self.db.upsert_usage_session_batch([
            (multi_id, self._PROXY_KEY, "main", "anthropic", "model-a",
             "2026-07-01", "2026-07-02", "", "", 600, 0, 0, 0, 0, 0, 0, 5),
            (multi_id, self._PROXY_KEY, "main", "anthropic", "model-b",
             "2026-07-01", "2026-07-02", "", "", 550, 0, 0, 0, 0, 0, 0, 5),
            (single_id, self._PROXY_KEY, "main", "anthropic", "model-a",
             "2026-07-01", "2026-07-02", "", "", 1000, 0, 0, 0, 0, 0, 0, 10),
            (tiny_id, self._PROXY_KEY, "main", "anthropic", "model-a",
             "2026-07-01", "2026-07-02", "", "", 10, 0, 0, 0, 0, 0, 0, 1),
        ])

        rows = await self.db.query_top_sessions(limit=100_000)
        rows = [r for r in rows if str(r["session_id"]).startswith(self._SESSION_PREFIX)]

        sessions: dict[str, list[dict]] = {}
        for row in rows:
            sessions.setdefault(row["session_id"], []).append(row)

        self.assertEqual(set(sessions.keys()), {multi_id, single_id, tiny_id})
        # The multi-model session's rows must both be present, unaggregated
        # away and unmerged into a single row -- the query returns one row
        # per (session, proxy_key, provider, model) so build_sessions_json
        # can still compute per-model cost.
        self.assertEqual(len(sessions[multi_id]), 2)
        self.assertEqual(
            {row["model"] for row in sessions[multi_id]},
            {"model-a", "model-b"},
        )
        self.assertEqual(
            sum(int(row["input_tokens"]) for row in sessions[multi_id]), 1150
        )
        self.assertEqual(len(sessions[single_id]), 1)
        self.assertEqual(int(sessions[single_id][0]["input_tokens"]), 1000)
        self.assertEqual(sessions[single_id][0]["key_name"], "")
