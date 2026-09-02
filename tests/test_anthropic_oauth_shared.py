from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_oauth import (
    build_activation_requests,
    build_refresh_payload,
    normalize_scope,
)


class AnthropicOAuthSharedTests(unittest.TestCase):
    def test_normalize_scope_from_json_list(self) -> None:
        raw = '["user:profile","user:inference","user:sessions:claude_code"]'
        self.assertEqual(
            normalize_scope(raw),
            "user:profile user:inference user:sessions:claude_code",
        )

    def test_build_refresh_payload_includes_scope_when_present(self) -> None:
        payload = build_refresh_payload(
            refresh_token="rt",
            client_id="cid",
            scope="user:profile user:inference",
        )
        self.assertEqual(payload["grant_type"], "refresh_token")
        self.assertEqual(payload["refresh_token"], "rt")
        self.assertEqual(payload["client_id"], "cid")
        self.assertEqual(payload["scope"], "user:profile user:inference")

    def test_build_activation_requests_contains_expected_endpoints(self) -> None:
        reqs = build_activation_requests(
            base_url="https://api.anthropic.com",
            access_token="tok-123",
        )
        self.assertEqual(len(reqs), 6)
        self.assertEqual(reqs[0]["method"], "GET")
        self.assertEqual(reqs[0]["path"], "/api/claude_code_penguin_mode")
        self.assertEqual(reqs[0]["headers"]["Authorization"], "Bearer tok-123")
        self.assertEqual(reqs[0]["headers"]["Host"], "api.anthropic.com")
        # mcp-registry step is intentionally unauthenticated in captured flow.
        self.assertNotIn("Authorization", reqs[3]["headers"])


if __name__ == "__main__":
    unittest.main()
