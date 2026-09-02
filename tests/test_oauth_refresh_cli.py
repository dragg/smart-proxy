from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from smart_proxy import oauth_refresh_cli


class _FakeAsyncClientContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False


class OAuthRefreshCliTests(unittest.TestCase):
    def test_from_db_refresh_notifies_running_proxy_after_db_update(self) -> None:
        fake_db = MagicMock()
        fake_db.connect = AsyncMock()
        fake_db.close = AsyncMock()
        fake_db.get_active_anthropic_keys = AsyncMock(
            return_value=[
                {
                    "id": "abc12345-key",
                    "key_type": "oauth",
                    "status": "active",
                    "refresh_token": "old-refresh",
                    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                    "scopes": '["user:profile","user:inference"]',
                }
            ]
        )
        fake_db.update_anthropic_oauth_tokens = AsyncMock()

        with (
            patch("smart_proxy.oauth_refresh_cli.httpx.AsyncClient", return_value=_FakeAsyncClientContext()),
            patch("smart_proxy.oauth_refresh_cli.refresh_oauth_token", new=AsyncMock(return_value=("new-access", 1234567890, "new-refresh"))),
            patch("smart_proxy.oauth_refresh_cli.build_database", return_value=fake_db),
            patch("smart_proxy.oauth_refresh_cli.get_settings", return_value=MagicMock()),
            patch("smart_proxy.oauth_refresh_cli._notify_running_proxy_reload", new=AsyncMock()) as notify_reload,
        ):
            oauth_refresh_cli.main(["--from-db", "--id-prefix", "abc123", "--skip-activation"])

        fake_db.update_anthropic_oauth_tokens.assert_awaited_once_with(
            "abc12345-key",
            "new-access",
            1234567890,
            "new-refresh",
        )
        notify_reload.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
