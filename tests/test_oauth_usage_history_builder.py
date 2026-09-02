# tests/test_oauth_usage_history_builder.py
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import build_oauth_usage_history


class BuildOAuthUsageHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_oauth_keys_returns_empty(self) -> None:
        db = MagicMock()
        db.get_all_model_prices = AsyncMock(return_value=[])
        db.list_anthropic_keys = AsyncMock(return_value=[{"id": 1, "key_type": "api_key"}])
        out = await build_oauth_usage_history(db, kind_filter=None, per_kind_limit=50)
        self.assertEqual(out, [])

    async def test_oauth_key_without_windows(self) -> None:
        db = MagicMock()
        db.get_all_model_prices = AsyncMock(return_value=[])
        db.list_anthropic_keys = AsyncMock(
            return_value=[{"id": 7, "key_type": "oauth", "name": "acc", "status": "active"}]
        )
        db.list_oauth_window_log = AsyncMock(return_value=[])
        db.list_oauth_window_usage = AsyncMock(return_value=[])
        db.list_oauth_window_drops = AsyncMock(return_value=[])
        db.list_oauth_limit_wipes = AsyncMock(return_value=[])
        db.list_oauth_window_usage_pending = AsyncMock(return_value=[])

        out = await build_oauth_usage_history(db, kind_filter=None, per_kind_limit=50)

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "7")
        self.assertEqual(out[0]["name"], "acc")
        self.assertEqual(out[0]["windows"], {})
