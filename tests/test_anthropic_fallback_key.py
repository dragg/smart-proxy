"""Scoped paid-fallback Anthropic keys.

A ``role='fallback'`` key is a paid ``sk-ant-`` credential that may serve a
request only when (a) the subscription tier has nothing pickable for it, (b) the
caller's ``sp-`` proxy key is listed in the key's scope, and (c) the request
carries no Claude Code fingerprint. It is never promoted and never picked by
anything else.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from smart_proxy import dashboard_api
from smart_proxy.anthropic_proxy import (
    _AnthropicKey,
    _anthropic_key_from_row,
    _claude_code_signal,
    _fallback_admission,
    AnthropicKeyPool,
)
from smart_proxy.db import parse_allowed_proxy_keys

SP = "sp-consumer-full-key"
OTHER_SP = "sp-someone-else"


def _oauth(key_id: str = "oauth-1", status: str = "active") -> _AnthropicKey:
    return _AnthropicKey(
        key_id=key_id, key_type="oauth", status=status, api_key=None,
        access_token="tok", refresh_token="ref", client_id="c",
        expires_at=9999999999999, name=key_id,
    )


def _fallback(key_id: str = "paid-1", scope: set[str] | None = None) -> _AnthropicKey:
    return _AnthropicKey(
        key_id=key_id, key_type="api_key", status="active", api_key="sk-ant-paid",
        access_token=None, refresh_token=None, client_id="c", expires_at=None,
        name=key_id, role="fallback",
        allowed_proxy_keys=frozenset(scope if scope is not None else {SP}),
    )


def _pool(keys: list[_AnthropicKey]) -> AnthropicKeyPool:
    pool = AnthropicKeyPool(MagicMock())
    pool._keys = keys
    return pool


class ScopeParsingTests(unittest.TestCase):
    def test_parses_json_array(self) -> None:
        self.assertEqual(parse_allowed_proxy_keys('["sp-a", "sp-b"]'), frozenset({"sp-a", "sp-b"}))

    def test_fail_closed_on_missing_empty_and_malformed(self) -> None:
        # None covers code reading rows written before the migration.
        for raw in (None, "", "   ", "not json", '{"sp-a": 1}', "[1, 2]", '"sp-a"'):
            self.assertEqual(parse_allowed_proxy_keys(raw), frozenset(), raw)

    def test_row_without_column_yields_empty_scope(self) -> None:
        key = _anthropic_key_from_row({
            "id": "k", "key_type": "api_key", "status": "active", "api_key": "sk",
            "access_token": None, "refresh_token": None, "client_id": "c",
            "expires_at": None, "role": "fallback",
        })
        self.assertEqual(key.allowed_proxy_keys, frozenset())


class FallbackPickTests(unittest.TestCase):
    def test_no_fallback_keys_behaves_exactly_as_before(self) -> None:
        """Regression lock: passing fallback_for changes nothing without a fallback key."""
        pool = _pool([_oauth("a"), _oauth("b")])
        picked = [pool.pick(fallback_for=SP).key_id for _ in range(4)]
        self.assertEqual(picked, ["a", "a", "a", "a"])
        pool.cooldown(pool._keys[0], 60)
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "b")

    def test_not_picked_while_a_subscription_key_is_available(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        for _ in range(3):
            self.assertEqual(pool.pick(fallback_for=SP).key_id, "a")

    def test_serves_when_primary_is_cooled(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        pool.cooldown(pool._keys[0], 300)
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "paid-1")

    def test_serves_when_primary_is_low_balance(self) -> None:
        pool = _pool([_oauth("a", status="low_balance"), _fallback()])
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "paid-1")

    def test_serves_when_primary_is_deactivated(self) -> None:
        pool = _pool([_oauth("a", status="inactive"), _fallback()])
        pool._banned.add("a")
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "paid-1")

    def test_model_scoped_cooldown_on_the_primary_escalates_only_that_model(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        pool.cooldown(pool._keys[0], 300, model="claude-opus-5")
        self.assertEqual(pool.pick(model="claude-opus-5", fallback_for=SP).key_id, "paid-1")
        self.assertEqual(pool.pick(model="claude-haiku-4-5", fallback_for=SP).key_id, "a")

    def test_out_of_scope_proxy_key_gets_nothing(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        pool.cooldown(pool._keys[0], 300)
        self.assertIsNone(pool.pick(fallback_for=OTHER_SP))

    def test_empty_scope_serves_nobody(self) -> None:
        pool = _pool([_oauth("a"), _fallback(scope=set())])
        pool.cooldown(pool._keys[0], 300)
        self.assertIsNone(pool.pick(fallback_for=SP))

    def test_no_fallback_for_means_subscription_tier_only(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        pool.cooldown(pool._keys[0], 300)
        self.assertIsNone(pool.pick())

    def test_cooled_fallback_is_not_picked(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        pool.cooldown(pool._keys[0], 300)
        pool.cooldown(pool._keys[1], 30)
        self.assertIsNone(pool.pick(fallback_for=SP))

    def test_fallback_never_counts_as_an_alive_primary(self) -> None:
        """A standby must still take over when the only other key is a fallback."""
        standby = _oauth("s")
        standby.role = "standby"
        pool = _pool([standby, _fallback()])
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "s")

    def test_api_key_to_oauth_redirect_never_reaches_the_fallback_tier(self) -> None:
        """The pass-1 redirect prefers a free oauth key; it must not see paid keys."""
        api_primary = _AnthropicKey(
            key_id="api-primary", key_type="api_key", status="active",
            api_key="sk-ant-other", access_token=None, refresh_token=None,
            client_id="c", expires_at=None,
        )
        pool = _pool([api_primary, _fallback()])
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "api-primary")

    def test_picking_the_fallback_leaves_the_sticky_index_alone(self) -> None:
        pool = _pool([_oauth("a"), _oauth("b"), _fallback()])
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "a")
        index_before = pool._index
        pool.cooldown(pool._keys[0], 300)
        pool.cooldown(pool._keys[1], 300)
        self.assertEqual(pool.pick(fallback_for=SP).key_id, "paid-1")
        self.assertEqual(pool._index, index_before)

    def test_promote_to_primary_is_a_no_op_for_a_fallback_key(self) -> None:
        pool = _pool([_fallback()])
        asyncio.run(pool.promote_to_primary(pool._keys[0]))
        self.assertEqual(pool._keys[0].role, "fallback")

    def test_has_scoped_fallback(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        self.assertTrue(pool.has_scoped_fallback(SP))
        self.assertFalse(pool.has_scoped_fallback(OTHER_SP))
        self.assertFalse(pool.has_scoped_fallback(""))


class NextAvailableInTests(unittest.TestCase):
    def test_reports_the_soonest_across_both_tiers(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        pool.cooldown(pool._keys[0], 600)
        pool.cooldown(pool._keys[1], 30)
        self.assertLessEqual(pool.next_available_in(fallback_for=SP), 31)
        # Without paid-tier access the client must be told the subscription's wait.
        self.assertGreater(pool.next_available_in(), 500)

    def test_ignores_a_fallback_the_caller_may_not_use(self) -> None:
        pool = _pool([_oauth("a"), _fallback()])
        pool.cooldown(pool._keys[0], 600)
        pool.cooldown(pool._keys[1], 30)
        self.assertGreater(pool.next_available_in(fallback_for=OTHER_SP), 500)


def _request(headers: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(headers=headers or {})


class _Limiter:
    def __init__(self, limits: dict[str, dict]) -> None:
        self._limits = limits

    def limits_for(self, proxy_key: str) -> dict:
        return dict(self._limits.get(proxy_key) or {})


_LIMITED = _Limiter({SP: {"daily_usd": 50.0}})


class ClaudeCodeGateTests(unittest.TestCase):
    def test_plain_sdk_request_is_admitted(self) -> None:
        body = {"model": "m", "messages": [], "system": "You are a helpful assistant."}
        self.assertEqual(
            _fallback_admission(_request({"User-Agent": "anthropic-sdk-python/1.2"}), body, SP, _LIMITED),
            (SP, ""),
        )

    def test_each_claude_code_signal_blocks(self) -> None:
        cases = {
            "user_agent": (_request({"User-Agent": "claude-cli/2.1.92 (external, cli)"}), {}),
            "agent_id_header": (_request({"x-claude-code-agent-id": "abc"}), {}),
            "x_app": (_request({"x-app": "cli"}), {}),
            "oauth_beta": (_request({"anthropic-beta": "foo,oauth-2025-04-20"}), {}),
            "system_prompt": (
                _request(),
                {"system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI"}]},
            ),
            "session_metadata": (
                _request(),
                {"metadata": {"user_id": json.dumps({"session_id": "s-1"})}},
            ),
        }
        for expected, (req, body) in cases.items():
            with self.subTest(expected):
                self.assertEqual(_claude_code_signal(req, body), expected)
                self.assertEqual(
                    _fallback_admission(req, body, SP, _LIMITED),
                    (None, f"claude_code:{expected}"),
                )

    def test_billing_block_alone_does_not_look_like_claude_code(self) -> None:
        """The proxy prepends this block itself; req_body is read before that, but
        a client echoing it back must not be mistaken for the marker."""
        body = {"system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=1; cch=0;"},
            {"type": "text", "text": "You are a translation service."},
        ]}
        self.assertEqual(_claude_code_signal(_request(), body), "")

    def test_passthrough_bucket_can_never_reach_a_paid_key(self) -> None:
        self.assertEqual(
            _fallback_admission(_request(), {}, "claude-passthrough", _LIMITED),
            (None, "not_a_proxy_key"),
        )

    def test_proxy_key_without_a_spend_limit_is_refused_at_serve_time(self) -> None:
        self.assertEqual(
            _fallback_admission(_request(), {}, OTHER_SP, _LIMITED),
            (None, "no_spend_limit"),
        )

    def test_missing_limiter_is_fail_closed(self) -> None:
        self.assertEqual(
            _fallback_admission(_request(), {}, SP, None), (None, "no_spend_limit")
        )

    def test_non_dict_body_is_admitted_on_the_header_signals_alone(self) -> None:
        self.assertEqual(_claude_code_signal(_request(), None), "")


# These endpoints mutate the pool, so the credential is the dashboard admin
# secret rather than a caller's sp- key.
ADMIN = "test-dashboard-admin-secret"
AUTH = {"Authorization": f"Bearer {ADMIN}"}


def _dash_app(db, *, limiter=None, pool=None):
    return {
        "db": db,
        "key_limiter": limiter,
        "dashboard_secret": ADMIN,
        "anthropic_pool": pool or SimpleNamespace(
            is_proxy_key=lambda t: True, reload=AsyncMock()),
    }


def _post(app, body: dict):
    from aiohttp.test_utils import make_mocked_request

    req = make_mocked_request("POST", "/api/anthropic/keys/scope", app=app, headers=AUTH)
    req.json = AsyncMock(return_value=body)
    return req


class ScopeEndpointTests(unittest.TestCase):
    def _db(self, key_type: str = "api_key", role: str = "fallback"):
        db = MagicMock()
        db.get_anthropic_key = AsyncMock(return_value={
            "id": "paid-1", "key_type": key_type, "status": "active", "role": role,
            "allowed_proxy_keys": "[]",
        })
        db.get_proxy_key_by_created_at = AsyncMock(
            side_effect=lambda ts: SP if ts == "2026-01-01T00:00:00" else None)
        db.set_anthropic_key_scope = AsyncMock(return_value=True)
        return db

    def test_resolves_created_at_to_the_full_key_and_stores_it(self) -> None:
        db = self._db()
        pool = SimpleNamespace(is_proxy_key=lambda t: True, reload=AsyncMock())
        app = _dash_app(db, limiter=_LIMITED, pool=pool)
        resp = asyncio.run(dashboard_api._api_anthropic_key_scope(
            _post(app, {"id": "paid-1", "proxy_keys": ["2026-01-01T00:00:00"]})))
        self.assertEqual(resp.status, 200)
        self.assertEqual(db.set_anthropic_key_scope.await_args.args[1], [SP])
        pool.reload.assert_awaited()

    def test_rejects_a_proxy_key_without_a_spend_limit(self) -> None:
        db = self._db()
        app = _dash_app(db, limiter=_Limiter({}))
        resp = asyncio.run(dashboard_api._api_anthropic_key_scope(
            _post(app, {"id": "paid-1", "proxy_keys": ["2026-01-01T00:00:00"]})))
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_scope.assert_not_awaited()

    def test_rejects_an_unknown_proxy_key(self) -> None:
        db = self._db()
        app = _dash_app(db, limiter=_LIMITED)
        resp = asyncio.run(dashboard_api._api_anthropic_key_scope(
            _post(app, {"id": "paid-1", "proxy_keys": ["nope"]})))
        self.assertEqual(resp.status, 404)

    def test_rejects_a_non_list_payload(self) -> None:
        app = _dash_app(self._db(), limiter=_LIMITED)
        resp = asyncio.run(dashboard_api._api_anthropic_key_scope(
            _post(app, {"id": "paid-1", "proxy_keys": "sp-x"})))
        self.assertEqual(resp.status, 400)

    def test_empty_list_clears_the_scope(self) -> None:
        db = self._db()
        app = _dash_app(db, limiter=_LIMITED)
        resp = asyncio.run(dashboard_api._api_anthropic_key_scope(
            _post(app, {"id": "paid-1", "proxy_keys": []})))
        self.assertEqual(resp.status, 200)
        self.assertEqual(db.set_anthropic_key_scope.await_args.args[1], [])

    def test_a_caller_key_cannot_edit_the_scope(self) -> None:
        from aiohttp.test_utils import make_mocked_request

        app = _dash_app(self._db(), limiter=_LIMITED,
                        pool=SimpleNamespace(is_proxy_key=lambda t: True,
                                             check_auth=lambda t: True,
                                             reload=AsyncMock()))
        req = make_mocked_request("POST", "/api/anthropic/keys/scope", app=app,
                                  headers={"Authorization": "Bearer sp-consumer"})
        req.json = AsyncMock(return_value={"id": "paid-1", "proxy_keys": []})
        resp = asyncio.run(dashboard_api._api_anthropic_key_scope(req))
        # Authenticated, but scoping a paid key is administration.
        self.assertEqual(resp.status, 403)


class ApiKeyCreateEndpointTests(unittest.TestCase):
    def _db(self):
        db = MagicMock()
        db.get_proxy_key_by_created_at = AsyncMock(
            side_effect=lambda ts: SP if ts == "2026-01-01T00:00:00" else None)
        db.insert_anthropic_key = AsyncMock()
        return db

    def _run(self, body: dict, *, limiter=_LIMITED, pool=None, headers=None):
        from aiohttp.test_utils import make_mocked_request

        db = self._db()
        app = _dash_app(db, limiter=limiter, pool=pool)
        req = make_mocked_request(
            "POST", "/api/anthropic/keys/apikey", app=app, headers=headers or AUTH)
        req.json = AsyncMock(return_value=body)
        return db, app, asyncio.run(dashboard_api._api_anthropic_apikey_create(req))

    def test_creates_a_scoped_fallback_key_in_one_insert(self) -> None:
        """Role and scope must be part of the INSERT: a paid key that exists as a
        default 'primary' even briefly is live in the general pool."""
        pool = SimpleNamespace(is_proxy_key=lambda t: True, reload=AsyncMock())
        db, _, resp = self._run({
            "api_key": "sk-ant-secret", "name": "backup",
            "role": "fallback", "proxy_keys": ["2026-01-01T00:00:00"],
        }, pool=pool)
        self.assertEqual(resp.status, 200)
        kwargs = db.insert_anthropic_key.await_args.kwargs
        self.assertEqual(kwargs["key_type"], "api_key")
        self.assertEqual(kwargs["api_key"], "sk-ant-secret")
        self.assertEqual(kwargs["role"], "fallback")
        self.assertEqual(kwargs["allowed_proxy_keys"], [SP])
        self.assertEqual(kwargs["name"], "backup")
        pool.reload.assert_awaited()
        self.assertNotIn("sk-ant-secret", resp.text)

    def test_defaults_to_fallback_with_a_derived_label(self) -> None:
        db, _, resp = self._run({"api_key": "sk-ant-abcdefghij"})
        self.assertEqual(resp.status, 200)
        kwargs = db.insert_anthropic_key.await_args.kwargs
        self.assertEqual(kwargs["role"], "fallback")
        self.assertEqual(kwargs["allowed_proxy_keys"], [])
        self.assertEqual(kwargs["name"], "apikey-sk-ant-abcde")  # matches the CLI's label

    def test_rejects_a_key_that_is_not_an_anthropic_api_key(self) -> None:
        for value in ("", "   ", "sp-not-this", "oauth-token"):
            db, _, resp = self._run({"api_key": value})
            self.assertEqual(resp.status, 400, value)
            db.insert_anthropic_key.assert_not_awaited()

    def test_rejects_an_unknown_role(self) -> None:
        db, _, resp = self._run({"api_key": "sk-ant-x", "role": "standby"})
        self.assertEqual(resp.status, 400)
        db.insert_anthropic_key.assert_not_awaited()

    def test_rejects_a_scope_on_a_primary_key(self) -> None:
        db, _, resp = self._run({
            "api_key": "sk-ant-x", "role": "primary",
            "proxy_keys": ["2026-01-01T00:00:00"],
        })
        self.assertEqual(resp.status, 400)
        db.insert_anthropic_key.assert_not_awaited()

    def test_rejects_a_consumer_without_a_spend_limit(self) -> None:
        db, _, resp = self._run(
            {"api_key": "sk-ant-x", "proxy_keys": ["2026-01-01T00:00:00"]},
            limiter=_Limiter({}))
        self.assertEqual(resp.status, 400)
        db.insert_anthropic_key.assert_not_awaited()

    def test_rejects_an_unknown_consumer(self) -> None:
        db, _, resp = self._run({"api_key": "sk-ant-x", "proxy_keys": ["nope"]})
        self.assertEqual(resp.status, 404)
        db.insert_anthropic_key.assert_not_awaited()

    def test_an_unauthenticated_caller_cannot_add_a_paid_key(self) -> None:
        db, _, resp = self._run(
            {"api_key": "sk-ant-x"},
            pool=SimpleNamespace(is_proxy_key=lambda t: False,
                                 check_auth=lambda t: False,
                                 reload=AsyncMock()),
            headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(resp.status, 401)
        db.insert_anthropic_key.assert_not_awaited()


class RoleEndpointTests(unittest.TestCase):
    def _run(self, key_type: str, role: str, *, actives: list[dict] | None = None):
        from aiohttp.test_utils import make_mocked_request

        db = MagicMock()
        db.get_anthropic_key = AsyncMock(return_value={
            "id": "k1", "key_type": key_type, "status": "active", "role": "primary",
        })
        db.get_active_anthropic_keys = AsyncMock(return_value=actives if actives is not None else [
            {"id": "k1", "role": "primary", "status": "active"},
            {"id": "k2", "role": "primary", "status": "active"},
        ])
        db.set_anthropic_key_role = AsyncMock(return_value=True)
        app = _dash_app(db)
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "k1", "role": role})
        return db, asyncio.run(dashboard_api._api_anthropic_key_role(req))

    def test_api_key_can_be_made_fallback(self) -> None:
        db, resp = self._run("api_key", "fallback")
        self.assertEqual(resp.status, 200)
        db.set_anthropic_key_role.assert_awaited()

    def test_api_key_cannot_be_made_standby(self) -> None:
        """Standby auto-promotes on first serve — a paid key would take everything."""
        db, resp = self._run("api_key", "standby")
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()

    def test_oauth_key_cannot_be_made_fallback(self) -> None:
        db, resp = self._run("oauth", "fallback")
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()

    def test_unknown_role_rejected(self) -> None:
        db, resp = self._run("api_key", "whatever")
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()

    def test_cannot_demote_the_last_active_primary_to_fallback(self) -> None:
        db, resp = self._run("api_key", "fallback", actives=[
            {"id": "k1", "role": "primary", "status": "active"},
        ])
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
