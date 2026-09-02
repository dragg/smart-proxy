from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.db_test_utils import get_test_database_url


class DatabaseTestUtilsTests(unittest.TestCase):
    def test_get_test_database_url_ignores_database_url(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TEST_DATABASE_URL": "postgresql://postgres@127.0.0.1:5434/smart_proxy_tests",
                "DATABASE_URL": "postgresql://postgres@127.0.0.1:5434/keys",
            },
            clear=False,
        ):
            self.assertEqual(
                get_test_database_url(),
                "postgresql://postgres@127.0.0.1:5434/smart_proxy_tests",
            )

    def test_get_test_database_url_returns_empty_without_explicit_env(self) -> None:
        # A DATABASE_URL meant for local development must never drag the suite
        # onto a real server: no TEST_DATABASE_URL means the SQLite default.
        with patch.dict(
            os.environ,
            {
                "TEST_DATABASE_URL": "",
                "DATABASE_URL": "postgresql://postgres@127.0.0.1:5434/keys",
            },
            clear=False,
        ):
            self.assertEqual(get_test_database_url(), "")


if __name__ == "__main__":
    unittest.main()
