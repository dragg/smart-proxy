# tests/test_anthropic_pool_reconcile.py
"""A recovered database must not overwrite a fresher in-memory token.

Refusing to refresh while the database is down (slice 2) leaves one residual
window: the database can die *between* the availability check and the persist.
When that happens the rotated token — the only living one, since Anthropic
retired its predecessor the moment it was issued — exists solely in memory.

Two things would then throw it away:

* `reload()` rebuilds every key object from the database and would swap the
  living token for the retired one;
* `_reread_token_if_changed` adopts the row on *any* difference, so after a
  failed persist it adopts precisely the dead token it exists to avoid.

Both now compare expiries: Anthropic rotates on every refresh, so a strictly
newer expiry identifies the strictly newer, and only living, token. On top of
that the pool re-persists what memory knows as soon as the database returns.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_proxy import _AnthropicKey, AnthropicKeyPool

_NEWER = 1_800_000_000_000
_OLDER = 1_700_000_000_000


def _row(*, expires_at: int, access: str, refresh: str) -> dict:
    return {
        "id": "key-1", "key_type": "oauth", "status": "active", "api_key": None,
        "access_token": access, "refresh_token": refresh,
        "client_id": "c", "expires_at": expires_at, "scopes": "[]",
        "subscription_type": "", "rate_limit_tier": "", "name": "k",
        "role": "primary", "allowed_proxy_keys": "[]",
    }


def _key(*, expires_at: int, access: str, refresh: str) -> _AnthropicKey:
    return _AnthropicKey(
        key_id="key-1", key_type="oauth", status="active", api_key=None,
        access_token=access, refresh_token=refresh, client_id="c",
        expires_at=expires_at, scopes="[]",
    )


class _Db:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.persists: list[dict] = []
        self.fail_persist = False

    def is_available(self) -> bool:
        return True

    async def get_active_anthropic_keys(self) -> list[dict]:
        return [self.row]

    async def get_active_proxy_key_names(self) -> dict[str, str]:
        return {}

    async def get_anthropic_key(self, key_id: str) -> dict | None:
        return self.row

    async def record_anthropic_key_event(self, **kwargs) -> int:  # noqa: ANN003
        return 1

    async def update_anthropic_oauth_tokens(self, key_id, access, expires,
                                            refresh, **kwargs):  # noqa: ANN001, ANN003
        if self.fail_persist:
            raise RuntimeError("db down")
        self.persists.append({
            "key_id": key_id, "access": access, "expires": expires,
            "refresh": refresh, "event": kwargs.get("audit_event_type"),
        })
        self.row = _row(expires_at=expires, access=access, refresh=refresh)
        return None


class ReloadFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_staler_row_does_not_replace_the_living_token(self) -> None:
        db = _Db(_row(expires_at=_OLDER, access="dead-access", refresh="dead-refresh"))
        pool = AnthropicKeyPool(db)
        pool._keys = [_key(expires_at=_NEWER, access="live-access", refresh="live-refresh")]

        await pool.reload()

        key = pool._keys[0]
        self.assertEqual(key.refresh_token, "live-refresh")
        self.assertEqual(key.expires_at, _NEWER)
        # ...and the pool takes the chance to write it back.
        self.assertEqual(len(db.persists), 1)
        self.assertEqual(db.persists[0]["refresh"], "live-refresh")

    async def test_a_fresher_row_still_wins(self) -> None:
        """The normal case: another writer rotated and persisted."""
        db = _Db(_row(expires_at=_NEWER, access="new-access", refresh="new-refresh"))
        pool = AnthropicKeyPool(db)
        pool._keys = [_key(expires_at=_OLDER, access="old-access", refresh="old-refresh")]

        await pool.reload()

        key = pool._keys[0]
        self.assertEqual(key.refresh_token, "new-refresh")
        self.assertEqual(db.persists, [])


class RereadFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_staler_row_is_not_adopted(self) -> None:
        """Regression: this is how the dead token used to be adopted."""
        db = _Db(_row(expires_at=_OLDER, access="dead-access", refresh="dead-refresh"))
        pool = AnthropicKeyPool(db)
        key = _key(expires_at=_NEWER, access="live-access", refresh="live-refresh")

        changed = await pool._reread_token_if_changed(key)

        self.assertFalse(changed)
        self.assertEqual(key.refresh_token, "live-refresh")

    async def test_a_fresher_row_is_adopted(self) -> None:
        db = _Db(_row(expires_at=_NEWER, access="new-access", refresh="new-refresh"))
        pool = AnthropicKeyPool(db)
        key = _key(expires_at=_OLDER, access="old-access", refresh="old-refresh")

        changed = await pool._reread_token_if_changed(key)

        self.assertTrue(changed)
        self.assertEqual(key.refresh_token, "new-refresh")


class ReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_keys_memory_is_fresher_about_are_repersisted(self) -> None:
        db = _Db(_row(expires_at=_OLDER, access="dead-access", refresh="dead-refresh"))
        pool = AnthropicKeyPool(db)
        pool._keys = [_key(expires_at=_NEWER, access="live-access", refresh="live-refresh")]

        count = await pool.reconcile_tokens()

        self.assertEqual(count, 1)
        self.assertEqual(db.persists[0]["refresh"], "live-refresh")
        self.assertEqual(db.persists[0]["event"], "recovery_repersist")

    async def test_nothing_to_do_when_the_db_is_already_current(self) -> None:
        db = _Db(_row(expires_at=_NEWER, access="a", refresh="r"))
        pool = AnthropicKeyPool(db)
        pool._keys = [_key(expires_at=_NEWER, access="a", refresh="r")]

        self.assertEqual(await pool.reconcile_tokens(), 0)
        self.assertEqual(db.persists, [])

    async def test_one_failing_key_does_not_stop_the_others(self) -> None:
        db = _Db(_row(expires_at=_OLDER, access="dead", refresh="dead"))
        db.fail_persist = True
        pool = AnthropicKeyPool(db)
        pool._keys = [_key(expires_at=_NEWER, access="live", refresh="live")]

        count = await pool.reconcile_tokens()      # must not raise

        self.assertEqual(count, 0)


class IncidentReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_token_rotated_during_an_outage_survives_recovery(self) -> None:
        """The whole failure chain, end to end."""
        db = _Db(_row(expires_at=_OLDER, access="old-access", refresh="old-refresh"))
        pool = AnthropicKeyPool(db)
        key = _key(expires_at=_OLDER, access="old-access", refresh="old-refresh")
        pool._keys = [key]

        # A refresh lands while the DB is down: memory advances, persist fails.
        db.fail_persist = True
        key.access_token, key.refresh_token, key.expires_at = (
            "rotated-access", "rotated-refresh", _NEWER,
        )
        try:
            await db.update_anthropic_oauth_tokens(
                key.key_id, key.access_token, key.expires_at, key.refresh_token,
            )
        except RuntimeError:
            pass

        # DB comes back; the breaker's close hook reconciles.
        db.fail_persist = False
        self.assertEqual(await pool.reconcile_tokens(), 1)

        # A later reload must now return the living token, not the retired one.
        await pool.reload()
        self.assertEqual(pool._keys[0].refresh_token, "rotated-refresh")


if __name__ == "__main__":
    unittest.main()
