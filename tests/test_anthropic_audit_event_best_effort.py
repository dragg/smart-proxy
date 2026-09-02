# tests/test_anthropic_audit_event_best_effort.py
"""The audit trail must never be able to fail a live request.

`_record_anthropic_event` writes to `anthropic_key_events` purely for
observability, but it was called unguarded from `AnthropicKeyPool._refresh_locked`
*before* the upstream `/token` call. So any DB error on that insert -- a full
disk, a sequence left behind by a restore, a transient connection blip -- raised
straight out of `ensure_valid_token()` into the aiohttp handler, which answered
500. Worse, it did so before the refresh was even attempted: the key never got a
fresh token, so every following request failed the same way and no upstream call
was ever made.

Token persistence (`update_anthropic_oauth_tokens`) keeps its own durability
guarantee and is deliberately untouched here -- only the standalone
observability writes become best-effort.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import AnthropicKeyPool, _record_anthropic_event
from tests.db_test_utils import connect_test_database


class _BoomDb:
    """Stands in for a database whose audit insert fails."""

    def __init__(self) -> None:
        self.attempts = 0

    async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
        del kwargs
        self.attempts += 1
        raise RuntimeError("duplicate key value violates unique constraint")


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.content = b"{}"

    def json(self) -> dict:
        return self._data

    async def aclose(self) -> None:
        return None


class _FakeAsyncClient:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.token_calls = 0

    async def post(self, url: str, **kwargs) -> _FakeResponse:
        del url, kwargs
        self.token_calls += 1
        return _FakeResponse(self._data)


class AuditEventBestEffortTests(unittest.TestCase):
    def test_record_anthropic_event_swallows_db_errors(self) -> None:
        async def run() -> None:
            db = _BoomDb()
            with self.assertLogs("anthropic_proxy", level=logging.WARNING) as logs:
                await _record_anthropic_event(
                    db, key_id="k", event_type="refresh_attempt", source="proxy_request",
                )
            self.assertEqual(db.attempts, 1)
            # The failure must still be visible to an operator.
            self.assertTrue(
                any("refresh_attempt" in line for line in logs.output),
                logs.output,
            )

        asyncio.run(run())

    def test_refresh_survives_a_failing_audit_write(self) -> None:
        """A broken audit table must not stop a token refresh or 500 the request."""

        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(
                    sqlite_fallback_path=str(Path(td) / "test.db")
                )
                try:
                    await db.insert_anthropic_key(
                        id="key-audit-boom", key_type="oauth",
                        access_token="old-access", refresh_token="old-refresh",
                        client_id="client-id-123", expires_at=0,
                        scopes='["user:profile","user:inference"]',
                        name="audit-boom",
                    )
                    pool = AnthropicKeyPool(db)
                    await pool.reload()
                    key = next(
                        (k for k in pool._keys if k.key_id == "key-audit-boom"), None
                    )
                    assert key is not None

                    # Every standalone audit write raises from here on.
                    async def boom(**kwargs) -> int:  # noqa: ANN003
                        del kwargs
                        raise RuntimeError("audit insert failed")

                    original = db.record_anthropic_key_event
                    db.record_anthropic_key_event = boom  # type: ignore[method-assign]
                    try:
                        client = _FakeAsyncClient({
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_in": 28800,
                        })
                        token = await pool.ensure_valid_token(
                            key, client,
                            audit_op_id="op-1", audit_source="proxy_request",
                            audit_path="/v1/messages", audit_model="claude-opus-5",
                            activate=False,
                        )
                    finally:
                        db.record_anthropic_key_event = original  # type: ignore[method-assign]

                    # The refresh went through despite the dead audit table...
                    self.assertEqual(client.token_calls, 1)
                    self.assertEqual(token, "new-access")
                    # ...and the rotated refresh token is durable, which is the
                    # part that must never be sacrificed to keep serving.
                    row = await db.get_anthropic_key("key-audit-boom")
                    assert row is not None
                    self.assertEqual(row["access_token"], "new-access")
                    self.assertEqual(row["refresh_token"], "new-refresh")
                finally:
                    # usage_* aside, the shared Postgres test DB is not reset
                    # between runs -- drop this key and its audit rows by hand.
                    await db.db.execute(
                        "DELETE FROM anthropic_key_events WHERE key_id = ?",
                        ("key-audit-boom",),
                    )
                    await db.db.execute(
                        "DELETE FROM anthropic_keys WHERE id = ?",
                        ("key-audit-boom",),
                    )
                    await db.db.commit()
                    await db.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
