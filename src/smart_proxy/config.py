from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    claude_like: bool = False
    anthropic_proxy_disable_1m_context: bool = False
    anthropic_oauth_smoke_enabled: bool = True
    anthropic_oauth_smoke_morning_window: str = "08:00-09:00"
    anthropic_oauth_smoke_midday_window: str = "14:00-15:00"
    # Continuous keep-warm for a standby OAuth key: refresh it this many minutes before
    # its access token expires, so a failover promotes an already-valid standby with no
    # synchronous (itself-fallible) refresh. Independent of primary state / cooldowns.
    anthropic_standby_keepwarm_enabled: bool = True
    anthropic_standby_keepwarm_buffer_minutes: int = 120
    # Telegram alerts on OAuth refresh failure (brick / transient) and recovery.
    # Both must be set to enable; unset → alerts off.
    anthropic_telegram_bot_token: str = ""
    anthropic_telegram_chat_id: str = ""
    anthropic_proxy_strip_system_phrase: str = ""
    # Upgrade Claude Code CLI's 5-minute prompt-cache breakpoints to a 1-hour TTL.
    # Only applied to requests whose original User-Agent is claude-cli/*.
    anthropic_proxy_upgrade_cache_ttl: bool = True
    # Max seconds the SSE pre-commit buffer holds the stream head waiting for a
    # commit/error marker. On timeout it flushes and streams live (forwarding
    # Anthropic's keep-alive pings) so a Cloudflare edge doesn't 524. <=0 disables.
    anthropic_proxy_precommit_timeout_seconds: float = 10.0
    # Auth is required by default: /_oauth_usage reports account ids and quota
    # state for every OAuth row, and the proxy binds 0.0.0.0.
    anthropic_oauth_usage_require_auth: bool = True
    anthropic_oauth_usage_cache_seconds: int = 60
    # Public URL of this proxy (e.g. https://proxy.example.com). Only used to
    # derive the OAuth redirect port when it is not set explicitly below.
    anthropic_oauth_login_base_url: str = ""
    # OAuth redirect_uri sent to Anthropic is always http://localhost:<port>/callback except
    # when anthropic_oauth_login_redirect_uri is set. Port from this string if numeric,
    # else parsed from anthropic_oauth_login_base_url, else ANTHROPIC_PROXY_PORT.
    anthropic_oauth_login_redirect_port: str = ""
    # Full redirect_uri override (rare); must match whitelist (typically localhost/callback).
    anthropic_oauth_login_redirect_uri: str = ""
    # Optional sp-* appended to POST /_reload from CLI after key changes when proxy auth is enforced.
    anthropic_proxy_reload_key: str = ""
    # Operator credential for the /_app/ dashboard. Reads accept a caller's sp-*
    # key; every mutation (attach or delete an OAuth account, change a role,
    # edit a spend limit) requires this. Unset means no one can administer.
    anthropic_proxy_dashboard_secret: str = ""
    # OpenAI-compatible endpoint (/v1/chat/completions) on the anthropic proxy.
    anthropic_proxy_openai_compat_enabled: bool = True
    anthropic_proxy_openai_compat_default_max_tokens: int = 8192
    # Inject cache_control breakpoints (system + last message) into translated
    # requests; the OpenAI protocol has no prompt caching of its own.
    anthropic_proxy_openai_compat_auto_cache: bool = True
    # TTL for injected breakpoints: "1h" (default; interactive agents pause
    # longer than 5m) or "5m" (cheaper cache writes).
    anthropic_proxy_openai_compat_cache_ttl: str = "1h"
    # Local timezone whose midnight starts the per-key spend-limit window.
    anthropic_proxy_limit_window_tz: str = "Europe/Paris"

    database_url: str = ""
    db_path: str = "./smart-proxy.db"
    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()
