from __future__ import annotations

import os

from smart_proxy.db import Database, build_database_from_config
from smart_proxy.db_migrations import run_postgres_migrations


def get_test_database_url() -> str:
    """Postgres URL for backend-specific tests, or "" to use the SQLite default.

    Deliberately reads one variable and nothing else. Falling back to
    ``DATABASE_URL`` would point the suite at a ``_tests`` database next to
    whatever the developer runs locally -- and ``connect_test_database`` wipes
    what it connects to.
    """
    return os.environ.get("TEST_DATABASE_URL", "").strip()


async def connect_test_database(*, sqlite_fallback_path: str) -> Database:
    database_url = get_test_database_url()
    db = build_database_from_config(
        database_url=database_url,
        db_path=sqlite_fallback_path,
    )
    if database_url:
        await run_postgres_migrations(database_url)
    await db.connect()
    if database_url:
        await db.replace_snapshot({})
        await db._seed_model_prices()
    return db
