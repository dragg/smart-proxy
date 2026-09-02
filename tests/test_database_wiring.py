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
