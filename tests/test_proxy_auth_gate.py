"""Who is allowed through the proxy's front door.

``check_auth`` used to let two things past that are not proxy keys: any string
merely *starting* with ``sk-ant-`` (a leftover from a passthrough mode that was
never implemented -- the client's token is not forwarded upstream, a pool key
is), and every request at all while no ``sp-`` key existed yet. Either one let
an unauthenticated caller spend the pool's Anthropic subscription, and read the
dashboard, because the same gate guards ``/v1/*`` and the read endpoints.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from smart_proxy.anthropic_proxy import AnthropicKeyPool, create_app
from smart_proxy.config import Settings

SP = "sp-0123456789abcdef0123456789ffee11"


def _pool(*proxy_keys: str) -> AnthropicKeyPool:
    pool = AnthropicKeyPool(MagicMock())
    pool._proxy_keys = set(proxy_keys)
    return pool


class CheckAuthTests(unittest.TestCase):
    def test_configured_proxy_key_is_accepted(self) -> None:
        self.assertTrue(_pool(SP).check_auth(SP))

    def test_unknown_proxy_key_is_rejected(self) -> None:
        self.assertFalse(_pool(SP).check_auth("sp-not-a-real-key"))

    def test_sk_ant_prefix_is_not_a_pass(self) -> None:
        # Not a credential -- just a string with the right first seven bytes.
        self.assertFalse(_pool(SP).check_auth("sk-ant-definitely-not-a-key"))

    def test_sk_ant_prefix_is_not_a_pass_when_no_keys_configured(self) -> None:
        self.assertFalse(_pool().check_auth("sk-ant-definitely-not-a-key"))

    def test_no_configured_keys_does_not_open_the_proxy(self) -> None:
        # A fresh install has no sp- key yet. The first one is minted with
        # `smart-proxy proxy-key add`, not by leaving the door open.
        self.assertFalse(_pool().check_auth(""))
        self.assertFalse(_pool().check_auth("anything"))

    def test_empty_token_is_rejected(self) -> None:
        self.assertFalse(_pool(SP).check_auth(""))


class IsProxyKeyTests(unittest.TestCase):
    def test_matches_check_auth_now_that_both_are_strict(self) -> None:
        pool = _pool(SP)
        for token in (SP, "sp-nope", "sk-ant-x", ""):
            self.assertEqual(
                pool.check_auth(token), pool.is_proxy_key(token), token
            )


class OAuthUsageAuthDefaultTests(unittest.TestCase):
    """`/_oauth_usage` reports account ids and quota state for every OAuth row.

    It used to be open to the world by default, on a service that binds
    0.0.0.0. Opting *in* to auth is the wrong direction for that.
    """

    def test_settings_require_auth_by_default(self) -> None:
        self.assertTrue(Settings(_env_file=None).anthropic_oauth_usage_require_auth)

    def test_create_app_requires_auth_by_default(self) -> None:
        import inspect

        sig = inspect.signature(create_app)
        self.assertIs(sig.parameters["oauth_usage_require_auth"].default, True)


if __name__ == "__main__":
    unittest.main()


class DashboardSecretWiringTests(unittest.TestCase):
    def test_settings_field_defaults_to_empty(self) -> None:
        self.assertEqual(Settings(_env_file=None).anthropic_proxy_dashboard_secret, "")

    def test_create_app_publishes_the_secret(self) -> None:
        import inspect

        self.assertIn("dashboard_secret", inspect.signature(create_app).parameters)


class DashboardSecretValidationTests(unittest.TestCase):
    """A weak or mistyped secret should stop the process, not quietly protect
    nothing. It is set once, by hand, so failing loudly costs an operator a
    minute and saves them a false sense of security."""

    def test_empty_is_allowed(self) -> None:
        from smart_proxy.anthropic_proxy import validate_dashboard_secret

        self.assertIsNone(validate_dashboard_secret(""))

    def test_good_secret_is_allowed(self) -> None:
        from smart_proxy.anthropic_proxy import validate_dashboard_secret

        self.assertIsNone(validate_dashboard_secret("correct-horse-battery-staple"))

    def test_short_secret_is_rejected(self) -> None:
        from smart_proxy.anthropic_proxy import validate_dashboard_secret

        self.assertIn("16", validate_dashboard_secret("hunter2") or "")

    def test_secret_shaped_like_a_credential_is_rejected(self) -> None:
        from smart_proxy.anthropic_proxy import validate_dashboard_secret

        for bad in ("sp-0123456789abcdef0123456789ffee11", "sk-ant-0123456789abcdef"):
            self.assertTrue(validate_dashboard_secret(bad), bad)


class LegacyBrowserLoginRemovedTests(unittest.TestCase):
    """The server-rendered OAuth login pages are gone.

    ``GET /_oauth/login`` gated on ``ANTHROPIC_OAUTH_LOGIN_SECRET``, which was
    empty by default -- and empty meant "do not check", not "do not allow". Its
    partner ``POST /_oauth/submit`` had no gate at all, so anyone who could
    reach the proxy could attach their own Anthropic account to the pool as an
    active primary. The dashboard does the same job behind the admin secret.

    ``/callback`` stays: Anthropic redirects there, and it only completes a
    ``state`` that an authorised start created.
    """

    _cached: set[str] | None = None

    @classmethod
    def _canonicals(cls) -> set[str]:
        if cls._cached is None:
            app = create_app("./ignored.db", oauth_smoke_enabled=False)
            cls._cached = {r.canonical for r in app.router.resources()}
        return cls._cached

    def test_legacy_login_routes_are_gone(self) -> None:
        canonicals = self._canonicals()
        self.assertNotIn("/_oauth/login", canonicals)
        self.assertNotIn("/_oauth/submit", canonicals)

    def test_callback_routes_remain(self) -> None:
        canonicals = self._canonicals()
        self.assertIn("/callback", canonicals)
        self.assertIn("/_oauth/callback", canonicals)

    def test_login_secret_setting_is_gone(self) -> None:
        self.assertFalse(
            hasattr(Settings(_env_file=None), "anthropic_oauth_login_secret")
        )

    def test_handlers_are_gone(self) -> None:
        import smart_proxy.anthropic_proxy as ap

        self.assertFalse(hasattr(ap, "_oauth_login_start"))
        self.assertFalse(hasattr(ap, "_oauth_manual_submit"))
