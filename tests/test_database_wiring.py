from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.config import Settings
from smart_proxy.db import Database, build_database
from smart_proxy.db_postgres import PostgresDatabase


class DatabaseWiringTests(unittest.TestCase):
    def test_build_database_uses_sqlite_backend_by_default(self) -> None:
        settings = Settings(_env_file=None)

        db = build_database(settings)

        self.assertIs(type(db), Database)

    def test_build_database_uses_postgres_backend_when_database_url_present(self) -> None:
        settings = Settings(
            database_url="postgresql://user:pass@localhost:5432/smart_proxy",
            _env_file=None,
        )

        db = build_database(settings)

        self.assertIsInstance(db, PostgresDatabase)


if __name__ == "__main__":
    unittest.main()


class MigrationBootGuardTests(unittest.IsolatedAsyncioTestCase):
    """Serving on an unmigrated Postgres schema fails every flush, silently.

    Migrations run only from `smart-proxy db migrate`, and connecting checks
    nothing, so "restart now, migrate later" used to start a proxy that looked
    healthy while writing nothing.
    """

    async def test_guard_is_a_noop_on_sqlite(self) -> None:
        from smart_proxy.anthropic_proxy import _require_migrations_applied

        class _Sqlite:
            _backend = "sqlite"

        # Must not raise, and must not touch any migration API.
        await _require_migrations_applied(_Sqlite())

    async def test_guard_names_the_pending_migrations(self) -> None:
        from smart_proxy.anthropic_proxy import _require_migrations_applied
        from smart_proxy.db_migrations import POSTGRES_MIGRATIONS

        all_names = {name for name, _ in POSTGRES_MIGRATIONS}
        latest = POSTGRES_MIGRATIONS[-1][0]

        class _Postgres:
            _backend = "postgres"

            async def ensure_migration_table(self):
                return None

            async def get_applied_migrations(self):
                return all_names - {latest}

        with self.assertRaises(RuntimeError) as ctx:
            await _require_migrations_applied(_Postgres())
        self.assertIn(latest, str(ctx.exception))
        self.assertIn("smart-proxy db migrate", str(ctx.exception))

    async def test_guard_passes_when_everything_is_applied(self) -> None:
        from smart_proxy.anthropic_proxy import _require_migrations_applied
        from smart_proxy.db_migrations import POSTGRES_MIGRATIONS

        class _Postgres:
            _backend = "postgres"

            async def ensure_migration_table(self):
                return None

            async def get_applied_migrations(self):
                return {name for name, _ in POSTGRES_MIGRATIONS}

        await _require_migrations_applied(_Postgres())
