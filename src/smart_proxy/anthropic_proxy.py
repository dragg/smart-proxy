"""Anthropic reverse proxy with key rotation and automatic OAuth token refresh.

Listens on ANTHROPIC_PROXY_PORT (default 8090).  Authenticates callers via
sp-* proxy keys (same table as smart_proxy).  Picks an active Anthropic key
from the ``anthropic_keys`` DB table, injects the ``x-api-key`` header, and
forwards to api.anthropic.com.

For OAuth keys the proxy checks ``expires_at`` before each request and
transparently refreshes the access token when it is close to expiry.

Also exposes an OpenAI-compatible POST /v1/chat/completions (see smart_proxy.openai_compat);
translated traffic is marked with the x-smart-proxy-openai-compat response header and
counted at GET /_openai_compat_stats.
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from dataclasses import dataclass, field
from uuid import uuid4
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
from aiohttp import web

from smart_proxy.claude_code_identity import (
    DEFAULT_CLAUDE_CODE_VERSION,
    ClaudeCodeVersion,
    render_billing_header,
    render_cli_user_agent,
    render_code_user_agent,
)
from smart_proxy.anthropic_oauth import (
    CLAUDE_OAUTH_CLIENT_ID,
    OAuthRefreshError,
    activate_oauth_access_token,
    build_claude_authorize_url,
    exchange_authorization_code,
    generate_pkce_pair,
    normalize_scope,
    refresh_oauth_token,
)
from smart_proxy.dashboard_api import register_dashboard_api
from smart_proxy.db import (
    Database,
    RESETS_AT_JITTER_TOLERANCE_MINUTES,
    UTILIZATION_DROP_THRESHOLD_PP,
    WINDOW_USAGE_COUNTERS,
    build_database_from_config,
    canonical_window_kind,
    is_weekly_window_kind,
    parse_allowed_proxy_keys,
)
from smart_proxy.key_limits import DEFAULT_WINDOW_TZ, KeyLimiter
from smart_proxy.notifier import AlertThrottle, TelegramNotifier
from smart_proxy.openai_compat import setup_openai_compat
from smart_proxy.request_classify import _session_id, _system_text, classify_request
from smart_proxy.usage import (
    UsageFlushError, UsageTracker, extract_usage, extract_usage_from_sse,
    _TAIL_BUF_MAX, build_price_lookup, estimate_cost_with_cache,
)
from smart_proxy.usage_dashboard import register_usage_dashboard

logger = logging.getLogger("anthropic_proxy")

PROXY_PORT = int(os.environ.get("ANTHROPIC_PROXY_PORT", "8090"))
UPSTREAM_BASE = os.environ.get(
    "ANTHROPIC_PROXY_UPSTREAM", "https://api.anthropic.com"
).rstrip("/")
TOKEN_URL = os.environ.get(
    "ANTHROPIC_OAUTH_TOKEN_URL",
    "https://platform.claude.com/v1/oauth/token",
)

_UPSTREAM_TIMEOUT = httpx.Timeout(600.0, connect=30.0)
_REFRESH_BUFFER_MS = 5 * 60 * 1000  # refresh 5 min before expiry
# Upper bound for any cooldown. Anthropic's weekly (seven_day) limits return a
# retry-after of up to ~25h; honouring that verbatim would park a model (or key)
# for a whole day. We cap it so a single 429 can't disable something for longer
# than an hour — after the cap we retry once and re-cool if the limit is still live.
_MAX_COOLDOWN_SECONDS = 3600

_REFRESH_RETRY_BACKOFF_SECONDS = 60   # after a failed refresh with a still-valid token
_MAX_TRANSIENT_REFRESH_FAILS = 5      # consecutive transient failures while expired → deactivate
_TOKEN_VALID_FLOOR_MS = 30_000        # only "keep serving" a token with >30s of real life left

# Continuous standby keep-warm: refresh a dormant standby's OAuth token well before
# it expires, so a failover (primary deactivated on a failed refresh) can promote the
# standby and serve immediately — without a synchronous, itself-fallible refresh at the
# worst moment. Independent of primary state; never triggered by a mere cooldown.
_DEFAULT_STANDBY_KEEPWARM_BUFFER_MS = 120 * 60 * 1000  # refresh when <2h of token life remains
_STANDBY_KEEPWARM_FLOOR_S = 60        # min sleep between iterations — no busy-spin on a deferred refresh
_STANDBY_KEEPWARM_CAP_S = 600         # max sleep — re-check the pool at least every 10 min

_ALERT_TRANSIENT_WINDOW_S = 30 * 60   # throttle repeated transient (rate-limit/network) alerts per key
# One wipe is observed through two channels (poll and response headers) and can
# repeat across the keys sharing a subscription; throttle per key and counter.
_WIPE_ALERT_WINDOW_S = 15 * 60
# Header utilization is a two-decimal fraction, so its resolution is a whole
# percentage point; below this a fall to zero is quantization noise, not a wipe.
_HEADER_WIPE_FLOOR_PP = UTILIZATION_DROP_THRESHOLD_PP
_FALLBACK_ALERT_WINDOW_S = 30 * 60    # throttle "paid fallback engaged" alerts per (key, proxy key)

_DROP_REQUEST_HEADERS = frozenset({
    "host", "connection", "content-length", "transfer-encoding", "te",
    "trailer", "proxy-connection", "keep-alive", "upgrade",
    # Internal loopback marker from the OpenAI-compat layer — read for usage
    # attribution, never forwarded to the Anthropic upstream.
    "x-smart-proxy-openai-compat",
})
_STRIP_RESPONSE_HEADERS = frozenset({
    "transfer-encoding", "connection", "content-length", "content-encoding",
})


# ---------------------------------------------------------------------------
# OAuth token refresh
# ---------------------------------------------------------------------------

async def _refresh_oauth_token(
    client: httpx.AsyncClient,
    refresh_token: str,
    client_id: str,
    scope: str | None = None,
) -> tuple[str, int, str | None]:
    """Compatibility wrapper around shared OAuth refresh implementation."""
    return await refresh_oauth_token(
        client,
        token_url=TOKEN_URL,
        refresh_token=refresh_token,
        client_id=client_id,
        scope=scope,
    )


def _is_auth_fatal(exc: Exception) -> bool:
    """A refresh failure that means the refresh token itself is gone: a 4xx from
    /token rejected it, or a 2xx spent it and we could not read the replacement.
    Either way a retry can only fail — terminal when the access token is also
    expired."""
    if not isinstance(exc, OAuthRefreshError):
        return False
    return exc.token_consumed or 400 <= exc.status_code < 500


def _parse_retry_after(exc: httpx.HTTPStatusError, default: int) -> int:
    try:
        return int(exc.response.headers.get("retry-after", str(default)))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Key pool
# ---------------------------------------------------------------------------

@dataclass
class _AnthropicKey:
    key_id: str
    key_type: str  # 'oauth' or 'api_key'
    status: str    # 'active' or 'low_balance'
    api_key: str | None
    access_token: str | None
    refresh_token: str | None
    client_id: str
    expires_at: int | None  # epoch ms
    scopes: str | None = None
    name: str | None = None
    role: str = "primary"
    # Only meaningful for role='fallback': the full proxy keys allowed to
    # escalate onto this credential. Empty means nobody (fail-closed).
    allowed_proxy_keys: frozenset[str] = frozenset()

    def effective_token(self) -> str | None:
        if self.key_type == "api_key":
            return self.api_key
        return self.access_token

    def is_expired(self, buffer_ms: int = _REFRESH_BUFFER_MS) -> bool:
        if self.key_type != "oauth" or self.expires_at is None:
            return False
        return int(time.time() * 1000) >= (self.expires_at - buffer_ms)


def _humanize_seconds(seconds: int) -> str:
    """Compact human-readable duration for user-facing retry hints: shows the two
    largest non-zero units, e.g. 90062 -> '1d 1h', 4500 -> '1h 15m', 45 -> '45s'."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = [(days, "d"), (hours, "h"), (minutes, "m"), (secs, "s")]
    nonzero = [f"{value}{label}" for value, label in parts if value]
    return " ".join(nonzero[:2]) if nonzero else "0s"


_ALERT_EMOJI = {"brick": "🔴", "transient": "🟡", "recovered": "✅"}
_SERVICE_NAME = "SmartProxy"


def _format_refresh_alert(
    key: _AnthropicKey,
    *,
    category: str,
    exc: Exception | None = None,
    deactivated: bool = False,
    valid_ms_left: int = 0,
    code: str | None = None,
    http_status: int | None = None,
) -> str:
    """Human-readable Telegram body for a refresh-failure / recovery alert.

    ``category`` is ``brick`` (fatal 4xx — refresh token dead, needs re-auth),
    ``transient`` (rate-limit / network — may self-heal), or ``recovered``.
    ``code``/``http_status`` override the values derived from ``exc`` when given."""
    emoji = _ALERT_EMOJI.get(category, "ℹ️")
    who = f"{key.role} «{key.name or '—'}» ({key.key_id[:8]})"
    prefix = f"{emoji} {_SERVICE_NAME} ·"
    if category == "recovered":
        return f"{prefix} Anthropic refresh recovered — {who}: key re-authorized."

    code = code or getattr(exc, "error_code", None) or (type(exc).__name__ if exc else "unknown")
    status = http_status if http_status is not None else getattr(exc, "status_code", None)
    status_part = f" HTTP {status}" if status else ""
    left = _humanize_seconds(max(0, valid_ms_left) // 1000)

    if category == "brick":
        if deactivated:
            return (
                f"{prefix} Anthropic key DEACTIVATED — {who}: "
                f"refresh is dead ({code}{status_part}), token expired."
            )
        return (
            f"{prefix} Anthropic refresh DEAD — {who}: {code}{status_part}. "
            f"Access token still valid for ~{left}. Key latched — re-authorization required."
        )

    # transient
    if deactivated:
        return (
            f"{prefix} Anthropic refresh failed (retries exhausted) — {who}: "
            f"{code}{status_part}, key deactivated."
        )
    return (
        f"{prefix} Anthropic refresh temporarily failed — {who}: {code}{status_part}. "
        f"Token still valid for ~{left}, retrying."
    )


def _anthropic_key_from_row(row: dict) -> _AnthropicKey:
    return _AnthropicKey(
        key_id=row["id"],
        key_type=row["key_type"],
        status=row["status"],
        api_key=row["api_key"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        client_id=row["client_id"] or "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        expires_at=row["expires_at"],
        scopes=row.get("scopes"),
        name=row.get("name"),
        role=row.get("role") or "primary",
        allowed_proxy_keys=parse_allowed_proxy_keys(row.get("allowed_proxy_keys")),
    )


class AnthropicKeyPool:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._keys: list[_AnthropicKey] = []
        self._index = 0
        self._cooldowns: dict[str, float] = {}  # key_id → monotonic deadline
        # (key_id, model) → monotonic deadline. A model-scoped cooldown parks only
        # that model on that key (e.g. a per-model weekly rate limit), leaving the
        # key usable for every other model.
        self._model_cooldowns: dict[tuple[str, str], float] = {}
        self._banned: set[str] = set()
        self._proxy_keys: set[str] = set()
        self._proxy_key_names: dict[str, str] = {}
        self._refresh_lock = asyncio.Lock()
        self._refresh_backoff: dict[str, float] = {}       # key_id → monotonic deadline
        self._transient_refresh_fails: dict[str, int] = {}  # key_id → consecutive transient fails (expired)
        self._refresh_tasks: set[asyncio.Task] = set()      # strong refs to shielded refresh tasks
        # A refresh token that returned a fatal 4xx (invalid_grant) is single-use-dead:
        # retrying it is futile and only hammers /token. Latch the key so NO refresher
        # (serving / poller / keep-warm / smoke) retries until re-auth lands a new token.
        self._refresh_dead: dict[str, str] = {}             # key_id → the dead refresh_token
        self._notifier = None                               # optional TelegramNotifier
        self._alerted_brick: set[str] = set()               # key_ids already brick-alerted (1×/latch)
        self._alert_transient_at: dict[str, float] = {}     # key_id → monotonic of last transient alert
        self._alert_tasks: set[asyncio.Task] = set()        # strong refs to fire-and-forget alert tasks
        # (fallback key_id, proxy key) → monotonic of the last "paid key engaged"
        # alert. Engagement is per-request but the alert is per outage.
        self._fallback_alert_at: dict[tuple[str, str], float] = {}
        # (key_id, logical window kind) → last observation. Held in memory so a
        # limit wipe is still detected and alerted with the database down.
        self._window_state: dict[tuple[str, str], _WindowState] = {}
        self._last_snapshot_id: dict[str, int] = {}
        self._wipe_alert_at: dict[tuple[str, str], float] = {}
        # key_id → (7d utilization %, 7d reset epoch, seen_at, wipe latched).
        # Feeds the per-request wipe channel: denser than polling, and the only
        # one carrying a request-id.
        self._unified_state: dict[str, tuple[float, str, str, bool]] = {}

    async def reload(self) -> None:
        old_cooldowns = self._cooldowns
        old_model_cooldowns = self._model_cooldowns
        old_by_id = {key.key_id: key for key in self._keys}
        rows = await self._db.get_active_anthropic_keys()
        self._keys = [_anthropic_key_from_row(r) for r in rows]
        # A reload rebuilds every key from the database, which would discard a
        # token that was rotated while the database was unreachable — the only
        # living one, since its predecessor was retired the moment it was
        # issued. Keep whichever side is newer, and write memory back when it
        # wins so the row stops being a trap.
        stale_rows = []
        for key in self._keys:
            previous = old_by_id.get(key.key_id)
            if previous is None:
                continue
            if _memory_is_fresher(previous.expires_at, key.expires_at):
                logger.warning(
                    "DB row for key %s is staler than memory — keeping in-memory tokens",
                    key.key_id[:12],
                )
                key.access_token = previous.access_token
                key.refresh_token = previous.refresh_token
                key.expires_at = previous.expires_at
                stale_rows.append(key)
        for key in stale_rows:
            await self._repersist_tokens(key, reason="reload_staler_row")
        self._banned.clear()
        now = time.monotonic()
        self._cooldowns = {
            key.key_id: deadline
            for key in self._keys
            if (deadline := old_cooldowns.get(key.key_id, 0)) > now
        }
        live_key_ids = {key.key_id for key in self._keys}
        self._model_cooldowns = {
            (key_id, model): deadline
            for (key_id, model), deadline in old_model_cooldowns.items()
            if key_id in live_key_ids and deadline > now
        }
        self._refresh_backoff = {
            key_id: deadline
            for key_id, deadline in self._refresh_backoff.items()
            if key_id in live_key_ids and deadline > now
        }
        self._transient_refresh_fails = {
            key_id: n
            for key_id, n in self._transient_refresh_fails.items()
            if key_id in live_key_ids
        }
        # Reconcile the refresh-dead latch: a key whose refresh_token changed was
        # re-authorized → clear the latch and announce recovery; a key that vanished
        # (deactivated / deleted) just drops out silently.
        new_by_id = {k.key_id: k for k in self._keys}
        for kid in list(self._refresh_dead):
            new = new_by_id.get(kid)
            if new is None:
                self._clear_refresh_dead(kid)
            elif (new.refresh_token or "") != self._refresh_dead[kid]:
                self._clear_refresh_dead(kid)
                self._fire_alert(category="recovered", key=new)
        self._proxy_key_names = await self._db.get_active_proxy_key_names()
        self._proxy_keys = set(self._proxy_key_names)
        active = sum(1 for k in self._keys if k.status == "active")
        low_bal = sum(1 for k in self._keys if k.status == "low_balance")
        logger.info(
            "Anthropic keys loaded: %d active, %d low_balance  (proxy auth: %d sp-keys)",
            active, low_bal, len(self._proxy_keys),
        )

    def check_auth(self, token: str) -> bool:
        """True only for a configured ``sp-`` proxy key.

        Two older bypasses are deliberately gone. A token merely *starting*
        with ``sk-ant-`` used to pass as "passthrough", but the client's token
        is never forwarded upstream -- a pool key is (see ``:2214``/``:2948``),
        so it authenticated nothing and let anyone spend the subscription.
        An empty pool used to open the proxy to everyone; the first key is
        minted with ``smart-proxy proxy-key add`` instead.
        """
        return bool(token) and token in self._proxy_keys

    def is_proxy_key(self, token: str) -> bool:
        """Strict gate for privileged/mutating actions: the token must be a
        configured ``sp-*`` proxy key. Unlike :meth:`check_auth`, this grants
        access neither when no proxy keys are configured nor for ``sk-ant-*``
        passthrough tokens."""
        return bool(token) and token in self._proxy_keys

    def proxy_key_name(self, proxy_key: str) -> str:
        """Human name for a caller's sp- key, or "" when unknown."""
        return self._proxy_key_names.get(proxy_key, "")

    def _is_pickable(
        self, key: _AnthropicKey, *, model: str | None = None, now: float | None = None
    ) -> bool:
        current = time.monotonic() if now is None else now
        if key.key_id in self._banned:
            return False
        if key.status == "low_balance":
            return False
        if current < self._cooldowns.get(key.key_id, 0):
            return False
        if model is not None and current < self._model_cooldowns.get((key.key_id, model), 0):
            return False
        return True

    def _find_pickable_oauth_index(
        self, *, model: str | None, now: float, eligible_role: str,
        exclude: set[str] | None = None,
    ) -> int | None:
        for index, key in enumerate(self._keys):
            if exclude and key.key_id in exclude:
                continue
            if (key.role == eligible_role and key.key_type == "oauth"
                    and self._is_pickable(key, model=model, now=now)):
                return index
        return None

    def get_loaded_key(self, key_id: str) -> _AnthropicKey | None:
        return next((key for key in self._keys if key.key_id == key_id), None)

    def _mark_refresh_dead(self, key: _AnthropicKey) -> None:
        """Latch a key whose refresh token is fatally dead (invalid_grant / 4xx)."""
        self._refresh_dead[key.key_id] = key.refresh_token or ""

    def _clear_refresh_dead(self, key_id: str) -> None:
        self._refresh_dead.pop(key_id, None)
        self._alerted_brick.discard(key_id)
        self._alert_transient_at.pop(key_id, None)

    def _fire_alert(
        self,
        *,
        category: str,
        key: _AnthropicKey,
        exc: Exception | None = None,
        deactivated: bool = False,
        valid_ms_left: int = 0,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """Fire-and-forget an operational alert, with per-category throttling:
        ``brick`` once per key until the latch clears, ``transient`` at most once per
        30 min per key, ``recovered`` always. No-op when no notifier is configured."""
        if self._notifier is None:
            return
        kid = key.key_id
        if category == "brick":
            if kid in self._alerted_brick:
                return
            self._alerted_brick.add(kid)
        elif category == "transient":
            now = time.monotonic()
            if now - self._alert_transient_at.get(kid, float("-inf")) < _ALERT_TRANSIENT_WINDOW_S:
                return
            self._alert_transient_at[kid] = now
        text = _format_refresh_alert(
            key, category=category, exc=exc, deactivated=deactivated,
            valid_ms_left=valid_ms_left, code=code, http_status=http_status,
        )
        task = asyncio.create_task(self._notifier.notify(text))
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    def _notify(self, text: str) -> None:
        """Fire-and-forget a Telegram message, keeping a strong task ref."""
        if self._notifier is None:
            return
        task = asyncio.create_task(self._notifier.notify(text))
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    def observe_windows(
        self, key_id: str, observations: list[dict], *, seen_at: str
    ) -> list[dict]:
        """Fold one observation batch into memory, returning detected wipes.

        Detection happens before the state update and without touching the
        database, so it survives a database outage.
        """
        prev = {
            kind: state
            for (kid, kind), state in self._window_state.items()
            if kid == key_id
        }
        wipes = _detect_limit_wipes(
            prev, observations, key_id=key_id, seen_at=seen_at)
        # Precedence is within this batch only. Comparing against the stored
        # state instead would freeze it forever for a payload that carries the
        # counter solely under its limits[] alias: the alias populates the slot
        # on the first poll and is skipped as a "duplicate" on every one after,
        # so from_utilization/prev_seen_at would stay pinned to the first
        # observation ever made — the opposite of the forensic value here.
        seen_in_batch: set[str] = set()
        for observation in observations:
            kind = canonical_window_kind(observation["window_kind"])
            if kind in seen_in_batch and observation["window_kind"] != kind:
                continue  # the aliased duplicate never overwrites the primary
            seen_in_batch.add(kind)
            self._window_state[(key_id, kind)] = _WindowState(
                utilization=observation.get("utilization"),
                resets_at=observation["resets_at"],
                resets_at_raw=observation["resets_at_raw"],
                seen_at=seen_at,
            )
        return wipes

    def last_snapshot_id(self, key_id: str) -> int | None:
        return self._last_snapshot_id.get(key_id)

    def set_last_snapshot_id(self, key_id: str, snapshot_id: int) -> None:
        self._last_snapshot_id[key_id] = snapshot_id

    def alert_limit_wipe(self, wipe: dict) -> None:
        """Alert on an undeclared limit wipe, throttled per key and kind."""
        signature = (str(wipe.get("key_id", "")), str(wipe.get("window_kind", "")))
        now = time.monotonic()
        if now - self._wipe_alert_at.get(signature, float("-inf")) < _WIPE_ALERT_WINDOW_S:
            return
        self._wipe_alert_at[signature] = now
        name = self.key_name(str(wipe.get("key_id", ""))) or str(wipe.get("key_id", ""))[:12]
        hours = wipe.get("hours_before_claimed")
        parts = [
            "♻️ Limit wiped without being announced",
            f"key: {name}",
            f"counter: {wipe.get('window_kind')}",
            f"was: {wipe.get('from_utilization')}% → 0%",
            f"claimed reset: {wipe.get('resets_at_claimed')}"
            + (f" (in {hours:.1f}h)" if isinstance(hours, (int, float)) else ""),
            f"source: {wipe.get('source', 'poll')}",
        ]
        if wipe.get("five_hour_rolled"):
            early = wipe.get("five_hour_early_minutes")
            parts.append(
                "5h window restarted at the same time"
                + (f", {early:.0f} min ahead of schedule" if isinstance(early, (int, float)) else "")
            )
        self._notify("\n".join(parts))

    def key_name(self, key_id: str) -> str | None:
        for key in self._keys:
            if key.key_id == key_id:
                return getattr(key, "name", None)
        return None

    def observe_unified_headers(
        self, key_id: str, utilization_7d: float | None, reset_7d: str | None
    ) -> dict | None:
        """Per-request wipe channel from ``anthropic-ratelimit-unified-*``.

        Denser than polling under load, and the only channel carrying a
        ``request-id``. A wipe here is 7d utilization falling to zero while the
        reset epoch stays put.

        Two guards this channel needs and the poll channel does not. The header
        value is a two-decimal fraction, so its smallest step is a whole
        percentage point and a key idling near 1% flickers 0.01/0.00 between
        responses; anything under ``_HEADER_WIPE_FLOOR_PP`` is therefore not
        treated as a wipe, and a wipe that small would carry no forensic value
        anyway. And because each response is handled in its own task, responses
        reach here out of order — so once a window is reported wiped it is
        latched until the reset epoch moves, rather than re-reported every time
        a late response reinstates a nonzero reading.
        """
        if utilization_7d is None or not reset_7d:
            return None
        now = _utc_now_iso()
        previous = self._unified_state.get(key_id)
        if previous is None or previous[1] != reset_7d:
            self._unified_state[key_id] = (utilization_7d, reset_7d, now, False)
            return None
        prev_utilization, _prev_reset, prev_seen_at, already_wiped = previous
        is_wipe = (
            not already_wiped
            and utilization_7d == 0
            and prev_utilization >= _HEADER_WIPE_FLOOR_PP
        )
        self._unified_state[key_id] = (
            utilization_7d, reset_7d, now, already_wiped or is_wipe)
        if not is_wipe:
            return None
        return {
            "key_id": key_id,
            "window_kind": "seven_day",
            "observed_at": now,
            "prev_seen_at": prev_seen_at,
            "from_utilization": prev_utilization,
            "resets_at_claimed": reset_7d,
            "hours_before_claimed": None,
            "five_hour_rolled": False,
            "five_hour_early_minutes": None,
            "source": "headers",
        }

    def has_scoped_fallback(self, proxy_key: str) -> bool:
        """True when some fallback key lists ``proxy_key`` — i.e. this consumer
        would have escalated to the paid tier had it been admitted. Used only to
        make a denial explainable in the log."""
        return bool(proxy_key) and any(
            k.role == "fallback" and proxy_key in k.allowed_proxy_keys for k in self._keys
        )

    async def note_fallback_serve(
        self, key: _AnthropicKey, proxy_key: str, *, audit_op_id: str = "",
        audit_path: str = "", audit_model: str | None = None,
    ) -> None:
        """Record and announce that a paid fallback key is now serving ``proxy_key``.

        Throttled per (key, proxy key) so an hours-long outage produces one alert
        and one audit row per consumer, not one per request."""
        now = time.monotonic()
        slot = (key.key_id, proxy_key)
        if now - self._fallback_alert_at.get(slot, float("-inf")) < _FALLBACK_ALERT_WINDOW_S:
            return
        self._fallback_alert_at[slot] = now
        logger.warning(
            "Paid fallback key %s (%s) now serving proxy key %s",
            key.key_id[:12], key.name or "—", _mask(proxy_key),
        )
        self._notify(
            f"🟠 {_SERVICE_NAME} · Paid API key «{key.name or '—'}» "
            f"is serving {_mask(proxy_key)} — no subscription key is available."
        )
        try:
            await self._db.record_anthropic_key_event(
                key_id=key.key_id, event_type="fallback_serve", op_id=audit_op_id,
                source="proxy_request", decision="engage", path=audit_path,
                model=audit_model,
                error_message="Paid fallback key engaged (no subscription key available)",
                context={"proxy_key": proxy_key[:12]},
            )
        except Exception as exc:
            # Memory is already updated, so the pool behaves correctly;
            # only durability is lost. Raising here would turn a database
            # outage into a client 500 on exactly the path meant to fail over.
            logger.warning("fallback key engagement audit not persisted: %s", exc)
            _alert_failure(source="fallback key engagement audit", exc=exc)

    def _primary_alive(self) -> bool:
        return any(k.role == "primary" and k.status != "inactive" for k in self._keys)

    def _pick_fallback(
        self, *, model: str | None, now: float, fallback_for: str,
        exclude: set[str] | None = None,
    ) -> _AnthropicKey | None:
        """First pickable ``role='fallback'`` key whose scope admits ``fallback_for``.

        Deterministic order is already sticky enough for one credential, so this
        deliberately leaves ``self._index`` alone: the primary tier keeps pointing
        at whatever it was serving before the subscription went away."""
        for key in self._keys:
            if key.role != "fallback":
                continue
            if exclude and key.key_id in exclude:
                continue
            if fallback_for not in key.allowed_proxy_keys:
                continue
            if self._is_pickable(key, model=model, now=now):
                return key
        return None

    def pick(
        self, model: str | None = None, *, fallback_for: str | None = None,
        exclude: set[str] | None = None,
    ) -> _AnthropicKey | None:
        """Sticky pick: keep returning the same key until it becomes unavailable
        (banned / low_balance / cooldown).  This maximises Anthropic prompt-cache
        hits because the cache is per-API-key.

        ``exclude`` skips keys already tried in this request. Stickiness means an
        upstream failure that sets no cooldown (an overload is Anthropic-wide,
        not the key's fault) would otherwise hand back the very key that just
        failed — so without this a caller can never reach a second key.

        ``model`` scopes the pick to a model: a key that is cooled down only for
        ``model`` (e.g. a per-model weekly rate limit) is still pickable for other
        models. Pass ``None`` to consider only key-level cooldowns.

        ``fallback_for`` is the caller's proxy key, and opts this request into the
        paid ``fallback`` tier — consulted only after the normal tier yielded
        nothing, and only for keys whose scope lists that proxy key. Pass ``None``
        (the default) to keep a request on the subscription tier no matter what."""
        if not self._keys:
            return None
        now = time.monotonic()
        eligible = "primary" if self._primary_alive() else "standby"
        n = len(self._keys)
        for _ in range(n):
            current_index = self._index % n
            key = self._keys[current_index]
            if (
                key.role != eligible
                or (exclude and key.key_id in exclude)
                or not self._is_pickable(key, model=model, now=now)
            ):
                self._index = (self._index + 1) % n
                continue
            if key.key_type == "api_key":
                oauth_index = self._find_pickable_oauth_index(
                    model=model, now=now, eligible_role=eligible, exclude=exclude,
                )
                if oauth_index is not None:
                    self._index = oauth_index
                    return self._keys[oauth_index]
            return key
        if fallback_for:
            return self._pick_fallback(
                model=model, now=now, fallback_for=fallback_for, exclude=exclude,
            )
        return None

    def cooldown(
        self, key: _AnthropicKey, seconds: int | None = None, *, model: str | None = None
    ) -> None:
        """Park a key. With ``model`` set, only that model is parked on this key
        (per-model rate limit); otherwise the whole key is parked (refresh/auth/SSE
        faults that affect every model). Duration is capped at ``_MAX_COOLDOWN_SECONDS``."""
        cd = seconds if seconds and seconds > 0 else 30
        cd = min(cd, _MAX_COOLDOWN_SECONDS)
        deadline = time.monotonic() + cd
        if model is not None:
            self._model_cooldowns[(key.key_id, model)] = deadline
            logger.warning("Key %s model %s cooled down for %ds", key.key_id[:12], model, cd)
        else:
            self._cooldowns[key.key_id] = deadline
            logger.warning("Key %s cooled down for %ds", key.key_id[:12], cd)

    def cooldown_snapshot(self) -> list[dict]:
        """Currently parked keys with seconds left, soonest first.

        Read-only view for the dashboard: without it an operator sees a key
        marked active while every request against it is being turned away.
        """
        now = time.monotonic()
        parked: list[dict] = []
        for key_id, deadline in self._cooldowns.items():
            if deadline > now:
                parked.append({"key_id": key_id, "model": None, "seconds_left": int(deadline - now) + 1})
        for (key_id, model), deadline in self._model_cooldowns.items():
            if deadline > now:
                parked.append({"key_id": key_id, "model": model, "seconds_left": int(deadline - now) + 1})
        parked.sort(key=lambda entry: entry["seconds_left"])
        return parked

    def clear_cooldowns(self, *, model: str | None = None) -> int:
        """Drop rate-limit cooldowns so the next request reaches upstream at once.

        Deadlines come from upstream's ``retry-after`` but are clamped to
        ``_MAX_COOLDOWN_SECONDS``, so a limit that resets hours out re-arms a
        fresh hour every hour and the wait never visibly shrinks. This is the
        operator's way to ask upstream whether the real limit has lifted instead
        of sitting out our own clamp. It buys no quota: if the limit still
        stands, the next attempt simply re-arms the cooldown.

        Refresh backoff and deactivations are deliberately left alone — those
        guard the OAuth refresh path, where retrying too eagerly risks burning a
        single-use refresh token and bricking the key.
        """
        if model is None:
            dropped = len(self._cooldowns) + len(self._model_cooldowns)
            self._cooldowns.clear()
            self._model_cooldowns.clear()
        else:
            stale = [pair for pair in self._model_cooldowns if pair[1] == model]
            for pair in stale:
                self._model_cooldowns.pop(pair, None)
            # A key-level cooldown blocks every model, so it has to go too or
            # clearing "just this model" would not actually free the key.
            dropped = len(stale) + len(self._cooldowns)
            self._cooldowns.clear()
        if dropped:
            logger.warning(
                "Cooldowns cleared by operator (model=%s): %d dropped", model or "all", dropped
            )
        return dropped

    def _defer(self, key: _AnthropicKey, now_mono: float, *, seconds: int = _REFRESH_RETRY_BACKOFF_SECONDS) -> None:
        """Park *refresh* (not the key) briefly after a failure while the access token
        is still valid, so we keep serving it without hammering /token every request."""
        self._refresh_backoff[key.key_id] = now_mono + min(seconds, _MAX_COOLDOWN_SECONDS)
        self._transient_refresh_fails.pop(key.key_id, None)

    def _on_refresh_task_done(self, task: asyncio.Task) -> None:
        self._refresh_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("shielded refresh task failed: %r", exc)

    async def _reread_token_if_changed(self, key: _AnthropicKey) -> bool:
        """Guard against stale in-memory key objects (reload() swaps instances): if the
        DB row now holds a different token/expiry, adopt it and report True so the caller
        retries instead of deactivating a key whose stored tokens are actually good."""
        try:
            row = await self._db.get_anthropic_key(key.key_id)
        except Exception as exc:
            # With the DB down there is nothing to compare against. Returning
            # False lets the caller latch the key on a fatal 4xx, which is
            # correct regardless of DB state: invalid_grant proves the
            # in-memory refresh token is dead on its own evidence.
            logger.warning(
                "could not re-read key %s from the DB: %s", key.key_id[:12], exc,
            )
            return False
        if not row:
            return False
        if _memory_is_fresher(key.expires_at, row.get("expires_at")):
            # The row is behind memory — this is the aftermath of a failed
            # persist, and adopting it would install the retired token. Report
            # "unchanged" so a fatal 4xx still latches on its own evidence.
            return False
        if (row.get("refresh_token") != key.refresh_token
                or row.get("access_token") != key.access_token
                or row.get("expires_at") != key.expires_at):
            key.access_token = row.get("access_token")
            key.refresh_token = row.get("refresh_token")
            key.expires_at = row.get("expires_at")
            return True
        return False

    async def _repersist_tokens(self, key: _AnthropicKey, *, reason: str) -> bool:
        """Best-effort write-back of the in-memory tokens for one key."""
        try:
            await self._db.update_anthropic_oauth_tokens(
                key.key_id, key.access_token, key.expires_at, key.refresh_token,
                audit_source="db_recovery",
                audit_event_type="recovery_repersist",
                audit_decision="update_tokens",
                audit_context={"reason": reason},
            )
            return True
        except Exception as exc:
            logger.warning(
                "could not re-persist tokens for key %s: %s", key.key_id[:12], exc,
            )
            _alert_failure(
                source=f"token re-persist ({key.key_id[:12]})", exc=exc,
            )
            return False

    async def reconcile_tokens(self) -> int:
        """Write back every in-memory token the database is behind on.

        Runs when the connection breaker closes. Without it a token rotated
        during an outage survives only until the next restart or reload, which
        is exactly how a temporary outage used to become a permanent key loss.
        One key failing must not stop the rest.
        """
        reconciled = 0
        for key in list(self._keys):
            if key.key_type != "oauth" or not key.refresh_token:
                continue
            try:
                row = await self._db.get_anthropic_key(key.key_id)
            except Exception as exc:
                logger.warning(
                    "reconcile: could not read key %s: %s", key.key_id[:12], exc,
                )
                continue
            if not row or not _memory_is_fresher(key.expires_at, row.get("expires_at")):
                continue
            if await self._repersist_tokens(key, reason="breaker_closed"):
                reconciled += 1
        if reconciled:
            logger.info("Re-persisted tokens for %d key(s) after DB recovery", reconciled)
        return reconciled

    def next_available_in(
        self, model: str | None = None, *, fallback_for: str | None = None
    ) -> int:
        """Seconds until the next non-banned key becomes available (for ``model``).

        A key is available for ``model`` only once both its key-level cooldown and
        its ``(key, model)`` cooldown have elapsed.

        Mirrors :meth:`pick`'s tiering, ``fallback_for`` included: when a request
        may escalate to the paid tier, a cooled fallback key counts too, so the
        client is told the soonest either tier can serve it rather than the
        subscription's (possibly much later) reset."""
        now = time.monotonic()
        eligible = "primary" if self._primary_alive() else "standby"
        soonest = None
        for k in self._keys:
            if k.key_id in self._banned:
                continue
            if k.role == "fallback":
                if not fallback_for or fallback_for not in k.allowed_proxy_keys:
                    continue
            elif k.role != eligible:
                continue
            cd = self._cooldowns.get(k.key_id, 0)
            if model is not None:
                cd = max(cd, self._model_cooldowns.get((k.key_id, model), 0))
            if now >= cd:
                return 0
            remaining = cd - now
            if soonest is None or remaining < soonest:
                soonest = remaining
        return int(soonest) + 1 if soonest is not None else 0

    async def deactivate(
        self,
        key: _AnthropicKey,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        audit_http_status: int | None = None,
        audit_request_id: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_retry_after: int | None = None,
        audit_context: dict | None = None,
    ) -> None:
        self._banned.add(key.key_id)
        key.status = "inactive"
        if key.role == "fallback":
            # A dead backup is invisible otherwise: it takes no traffic until the
            # next outage, and by then it is too late to notice it was revoked.
            self._notify(
                f"🔴 {_SERVICE_NAME} · Paid fallback key «{key.name or '—'}» "
                f"deactivated ({audit_error_type or 'auth error'}) — there is no backup left."
            )
        try:
            await self._db.set_anthropic_key_status(
                key.key_id,
                "inactive",
                audit_op_id=audit_op_id,
                audit_source=audit_source,
                audit_event_type="status_change",
                audit_decision="deactivate",
                audit_path=audit_path,
                audit_model=audit_model,
                audit_http_status=audit_http_status,
                audit_request_id=audit_request_id,
                audit_error_type=audit_error_type,
                audit_error_message=audit_error_message,
                audit_retry_after=audit_retry_after,
                audit_context=audit_context,
            )
        except Exception as exc:
            # Memory is already updated, so the pool behaves correctly;
            # only durability is lost. Raising here would turn a database
            # outage into a client 500 on exactly the path meant to fail over.
            logger.warning("key deactivation not persisted: %s", exc)
            _alert_failure(source="key deactivation persist", exc=exc)
        logger.warning("Key %s deactivated (auth error)", key.key_id[:12])

    async def promote_to_primary(
        self, key: _AnthropicKey, *, audit_op_id: str = "", audit_source: str = "",
        audit_path: str = "", audit_model: str | None = None,
    ) -> None:
        """Promote a standby that is about to serve into the primary role. Idempotent."""
        if key.role != "standby":
            return
        key.role = "primary"  # memory-first (before await) — closes the idempotency window
        try:
            await self._db.set_anthropic_key_role(
                key.key_id, "primary",
                audit_op_id=audit_op_id, audit_source=audit_source,
                audit_event_type="role_change", audit_decision="promote",
                audit_path=audit_path, audit_model=audit_model,
                audit_error_message="Standby promoted to primary on failover",
            )
        except Exception as exc:
            # Memory is already updated, so the pool behaves correctly;
            # only durability is lost. Raising here would turn a database
            # outage into a client 500 on exactly the path meant to fail over.
            logger.warning("promotion to primary not persisted: %s", exc)
            _alert_failure(source="key promotion to primary persist", exc=exc)
        logger.warning("Standby key %s promoted to primary", key.key_id[:12])

    async def mark_low_balance(
        self,
        key: _AnthropicKey,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        audit_http_status: int | None = None,
        audit_request_id: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_retry_after: int | None = None,
        audit_context: dict | None = None,
    ) -> None:
        """Mark key as low_balance — skip in rotation but don't permanently disable."""
        self._banned.add(key.key_id)
        key.status = "low_balance"
        try:
            await self._db.set_anthropic_key_status(
                key.key_id,
                "low_balance",
                audit_op_id=audit_op_id,
                audit_source=audit_source,
                audit_event_type="mark_low_balance",
                audit_decision="low_balance",
                audit_path=audit_path,
                audit_model=audit_model,
                audit_http_status=audit_http_status,
                audit_request_id=audit_request_id,
                audit_error_type=audit_error_type,
                audit_error_message=audit_error_message,
                audit_retry_after=audit_retry_after,
                audit_context=audit_context,
            )
        except Exception as exc:
            # Memory is already updated, so the pool behaves correctly;
            # only durability is lost. Raising here would turn a database
            # outage into a client 500 on exactly the path meant to fail over.
            logger.warning("low_balance not persisted: %s", exc)
            _alert_failure(source="low_balance persist", exc=exc)
        logger.warning("Key %s marked low_balance (billing/credit error)", key.key_id[:12])

    _REFRESH_BLOCKED = "__refresh_blocked__"

    async def ensure_valid_token(
        self,
        key: _AnthropicKey,
        client: httpx.AsyncClient,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        activate: bool = True,
        refresh_buffer_ms: int = _REFRESH_BUFFER_MS,
    ) -> str | None:
        """Return a valid token string, refreshing OAuth if needed.

        ``refresh_buffer_ms`` is how far ahead of expiry a refresh is triggered; the
        default matches the serving path. The standby keep-warm loop passes a much wider
        buffer so a dormant standby is refreshed hours early and is never expired at failover.

        Returns ``_REFRESH_BLOCKED`` when refresh is temporarily unavailable
        (rate-limited / transient) — caller should try the next key, NOT deactivate.
        Returns ``None`` only when the key is genuinely unusable (caller deactivates).
        The refresh is shielded from cancellation so an inbound disconnect cannot abort
        a rotation half-done (Anthropic rotates the refresh token as a side effect)."""
        if key.key_type == "api_key":
            return key.api_key
        if not key.is_expired(refresh_buffer_ms):
            return key.access_token

        task = asyncio.ensure_future(
            self._refresh_locked(
                key, client,
                audit_op_id=audit_op_id, audit_source=audit_source,
                audit_path=audit_path, audit_model=audit_model,
                activate=activate, refresh_buffer_ms=refresh_buffer_ms,
            )
        )
        self._refresh_tasks.add(task)
        task.add_done_callback(self._on_refresh_task_done)
        return await asyncio.shield(task)

    async def _refresh_locked(
        self,
        key: _AnthropicKey,
        client: httpx.AsyncClient,
        *,
        audit_op_id: str,
        audit_source: str,
        audit_path: str,
        audit_model: str | None,
        activate: bool,
        refresh_buffer_ms: int = _REFRESH_BUFFER_MS,
    ) -> str | None:
        async with self._refresh_lock:
            if not key.is_expired(refresh_buffer_ms):
                return key.access_token

            now_mono = time.monotonic()
            now_ms = int(time.time() * 1000)
            token_valid = (
                bool(key.access_token)
                and key.expires_at is not None
                and now_ms < key.expires_at - _TOKEN_VALID_FLOOR_MS
            )

            if token_valid and now_mono < self._refresh_backoff.get(key.key_id, 0.0):
                return key.access_token

            # Refresh-dead latch: this refresh token already returned a fatal 4xx and
            # will never succeed again. Don't touch /token — serve the still-valid access
            # token, or give up (None → caller deactivates) once it has expired.
            if key.key_id in self._refresh_dead:
                return key.access_token if token_valid else None

            # Refreshing consumes the refresh token upstream: Anthropic rotates
            # it and retires the old one. With the database down the rotated
            # token could not be persisted, so a *temporary* outage would become
            # a *permanent* key loss — the next restart or pool reload reads the
            # dead token back and only manual re-auth recovers it. Refusing
            # costs the key nothing but time: it keeps serving its current
            # access token, and becomes refreshable again the moment the DB
            # returns. The asymmetry decides.
            if not self._db.is_available():
                if token_valid:
                    self._defer(key, now_mono)
                    logger.warning(
                        "DB unavailable — refresh of key %s deferred, serving the "
                        "current token", key.key_id[:12],
                    )
                    self._fire_alert(
                        category="transient", key=key, code="db_unavailable",
                        valid_ms_left=(key.expires_at - now_ms) if key.expires_at else 0,
                    )
                    return key.access_token
                # _REFRESH_BLOCKED, never None: None makes every caller
                # deactivate the key — itself a DB write that would fail — and
                # bans a key that recovers by itself. This means "try the next
                # key, leave this one alone".
                logger.warning(
                    "DB unavailable and token for key %s has expired — blocking "
                    "refresh rather than burning it", key.key_id[:12],
                )
                return self._REFRESH_BLOCKED

            await _record_anthropic_event(
                self._db, key_id=key.key_id, event_type="refresh_attempt",
                op_id=audit_op_id, source=audit_source, decision="refresh",
                path=audit_path, model=audit_model,
                context={
                    "expires_at": key.expires_at,
                    "has_refresh_token": bool(key.refresh_token),
                    "scopes": normalize_scope(key.scopes),
                },
            )

            if not key.refresh_token:
                if token_valid:
                    self._defer(key, now_mono)
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_deferred",
                        op_id=audit_op_id, source=audit_source, decision="reuse_valid_token",
                        path=audit_path, model=audit_model,
                        error_type="missing_refresh_token",
                        error_message="No refresh_token; serving still-valid access token",
                    )
                    return key.access_token
                await _record_anthropic_event(
                    self._db, key_id=key.key_id, event_type="refresh_failed",
                    op_id=audit_op_id, source=audit_source, decision="deactivate",
                    path=audit_path, model=audit_model,
                    error_type="missing_refresh_token",
                    error_message="OAuth key has no refresh_token",
                )
                return None

            try:
                new_token, new_expires, rotated_refresh = await _refresh_oauth_token(
                    client, key.refresh_token, key.client_id,
                    scope=normalize_scope(key.scopes),
                )
            except httpx.HTTPStatusError as exc:  # 429
                retry_after = _parse_retry_after(exc, _REFRESH_RETRY_BACKOFF_SECONDS)
                valid_ms_left = (key.expires_at - now_ms) if key.expires_at else 0
                if token_valid:
                    self._defer(key, now_mono, seconds=max(retry_after, _REFRESH_RETRY_BACKOFF_SECONDS))
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_deferred",
                        op_id=audit_op_id, source=audit_source, decision="reuse_valid_token",
                        path=audit_path, model=audit_model, http_status=429,
                        error_type="rate_limited", retry_after=retry_after,
                        error_message="Refresh rate-limited; serving still-valid access token",
                    )
                    self._fire_alert(category="transient", key=key, code="rate_limited",
                                     http_status=429, valid_ms_left=valid_ms_left)
                    return key.access_token
                await _record_anthropic_event(
                    self._db, key_id=key.key_id, event_type="refresh_rate_limited",
                    op_id=audit_op_id, source=audit_source, decision="cooldown",
                    path=audit_path, model=audit_model, http_status=429,
                    error_type="rate_limited", retry_after=retry_after,
                    error_message="Refresh rate-limited",
                )
                self.cooldown(key, retry_after)
                self._fire_alert(category="transient", key=key, code="rate_limited", http_status=429)
                return self._REFRESH_BLOCKED
            except Exception as exc:  # OAuthRefreshError (4xx) | httpx transport/timeout | ...
                logger.exception("OAuth refresh failed for key %s", key.key_id[:12])
                valid_ms_left = (key.expires_at - now_ms) if key.expires_at else 0
                if _is_auth_fatal(exc):
                    # Fatal 4xx (invalid_grant): the refresh token is single-use-dead. First
                    # see whether another writer already rotated + persisted a good token.
                    if await self._reread_token_if_changed(key):
                        self._clear_refresh_dead(key.key_id)
                        await _record_anthropic_event(
                            self._db, key_id=key.key_id, event_type="refresh_deferred",
                            op_id=audit_op_id, source=audit_source, decision="reread_newer_token",
                            path=audit_path, model=audit_model,
                            error_type="stale_object_rescued",
                            error_message="Refresh token rotated by another writer; adopted newer DB token",
                        )
                        return self._REFRESH_BLOCKED  # DB had a newer token; retry next cycle
                    # Genuinely dead — latch so no refresher retries, and alert once.
                    self._mark_refresh_dead(key)
                    err_code = getattr(exc, "error_code", None)
                    if token_valid:
                        await _record_anthropic_event(
                            self._db, key_id=key.key_id, event_type="refresh_deferred",
                            op_id=audit_op_id, source=audit_source, decision="reuse_valid_token",
                            path=audit_path, model=audit_model,
                            error_type="refresh_runtime_error", error_message=str(exc),
                            context={"error_code": err_code, "refresh_dead": True},
                        )
                        self._fire_alert(category="brick", key=key, exc=exc, valid_ms_left=valid_ms_left)
                        return key.access_token
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_failed",
                        op_id=audit_op_id, source=audit_source, decision="deactivate",
                        path=audit_path, model=audit_model,
                        error_type="refresh_runtime_error", error_message=str(exc),
                        context={"error_code": err_code, "refresh_dead": True},
                    )
                    self._fire_alert(category="brick", key=key, exc=exc, deactivated=True)
                    return None
                # Non-fatal transient (network / timeout / 5xx).
                if token_valid:
                    self._defer(key, now_mono)
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_deferred",
                        op_id=audit_op_id, source=audit_source, decision="reuse_valid_token",
                        path=audit_path, model=audit_model,
                        error_type="refresh_runtime_error", error_message=str(exc),
                    )
                    self._fire_alert(category="transient", key=key, exc=exc, valid_ms_left=valid_ms_left)
                    return key.access_token
                n = self._transient_refresh_fails.get(key.key_id, 0) + 1
                self._transient_refresh_fails[key.key_id] = n
                if n >= _MAX_TRANSIENT_REFRESH_FAILS:
                    self._transient_refresh_fails.pop(key.key_id, None)
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="refresh_failed",
                        op_id=audit_op_id, source=audit_source, decision="deactivate",
                        path=audit_path, model=audit_model,
                        error_type="transient_exhausted", error_message=str(exc),
                    )
                    self._fire_alert(category="transient", key=key, exc=exc, deactivated=True)
                    return None
                await _record_anthropic_event(
                    self._db, key_id=key.key_id, event_type="refresh_failed",
                    op_id=audit_op_id, source=audit_source, decision="cooldown",
                    path=audit_path, model=audit_model,
                    error_type="refresh_transient", error_message=str(exc),
                )
                self.cooldown(key, _REFRESH_RETRY_BACKOFF_SECONDS)
                self._fire_alert(category="transient", key=key, exc=exc)
                return self._REFRESH_BLOCKED

            # Got new tokens. PERSIST FIRST (durability), then best-effort activation.
            previous_expires_at = key.expires_at
            key.access_token = new_token
            key.expires_at = new_expires
            if rotated_refresh:
                key.refresh_token = rotated_refresh
            try:
                await self._db.update_anthropic_oauth_tokens(
                    key.key_id, new_token, new_expires, rotated_refresh,
                    audit_op_id=audit_op_id, audit_source=audit_source,
                    audit_event_type="refresh_succeeded", audit_decision="update_tokens",
                    audit_path=audit_path, audit_model=audit_model,
                    audit_context={
                        "previous_expires_at": previous_expires_at,
                        "new_expires_at": new_expires,
                        "rotated_refresh_token": bool(rotated_refresh),
                    },
                )
            except Exception:
                logger.critical("rotated token not persisted for key %s; retrying once", key.key_id[:12])
                try:
                    await self._db.update_anthropic_oauth_tokens(
                        key.key_id, new_token, new_expires, rotated_refresh,
                        audit_source=audit_source, audit_event_type="refresh_succeeded",
                        audit_decision="update_tokens",
                    )
                except Exception as exc:
                    logger.critical("persist retry failed for key %s; DB token stale until next refresh", key.key_id[:12])
                    # The most dangerous state in the system, and until now the
                    # only one nobody was told about. Anthropic has already
                    # retired the old refresh token, so the row left in the DB is
                    # dead: serving continues on the in-memory token, but the
                    # next restart or pool reload reads the dead one back and the
                    # key is gone until someone re-authenticates by hand.
                    #
                    # Only the *final* failure alerts. The first one retries, and
                    # a retry that succeeds has lost nothing worth waking anyone.
                    _alert_failure(
                        source=f"rotated token not saved ({key.key_id[:12]})",
                        exc=exc,
                        detail=(
                            "A dead refresh token is left in the DB. The proxy keeps "
                            "serving on the in-memory token, but a restart or a pool "
                            "reload will lose the key — manual re-authorization required."
                        ),
                    )
            self._refresh_backoff.pop(key.key_id, None)
            self._transient_refresh_fails.pop(key.key_id, None)

            if activate:
                try:
                    await activate_oauth_access_token(
                        client, access_token=new_token, base_url=UPSTREAM_BASE,
                    )
                except Exception as exc:  # warmup only; token already persisted and valid
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="activation_failed",
                        op_id=audit_op_id, source=audit_source, decision="note",
                        path=audit_path, model=audit_model,
                        error_type="activation_runtime_error", error_message=str(exc),
                    )
                    logger.warning("OAuth activation warmup failed for key %s: %s", key.key_id[:12], exc)
            logger.info("Refreshed OAuth token for key %s, expires_at=%d", key.key_id[:12], new_expires)
            return new_token

    @property
    def available(self) -> int:
        return sum(1 for k in self._keys if k.key_id not in self._banned)


@dataclass
class _DailyOAuthSmokeSchedule:
    current_day: date
    morning_slot: datetime
    midday_slot: datetime
    morning_done: bool = False
    midday_done: bool = False


# ---------------------------------------------------------------------------
# Request forwarding helpers
# ---------------------------------------------------------------------------

def _forward_headers(request: web.Request) -> dict[str, str]:
    h: dict[str, str] = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in _DROP_REQUEST_HEADERS:
            continue
        if kl in ("authorization", "x-api-key"):
            continue
        h[k] = v
    return h


def _extract_client_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _usage_dashboard_authorize(request: web.Request) -> bool:
    """Authorize a ``/_usage`` dashboard request.

    The dashboard is opened in a browser, which cannot set an ``Authorization``
    header, so a ``?key=sp-xxx`` query param is accepted in addition to the
    normal header token (header takes precedence). This mirrors the auth the
    dashboard had on the smart proxy before it moved here.
    """
    pool = request.app["anthropic_pool"]
    token = _extract_client_token(request) or request.query.get("key", "").strip()
    return pool.check_auth(token)


def _truthy_query(request: web.Request, *names: str) -> bool:
    """True if any named query param is 1/true/yes/on (case-insensitive)."""
    q = request.rel_url.query
    for name in names:
        v = (q.get(name) or "").strip()
        if v and v.lower() in ("1", "true", "yes", "on"):
            return True
    return False


def _extract_reload_token(request: web.Request) -> str:
    """Proxy auth for /_reload: ?key= / ?token=, Authorization Bearer, or x-api-key."""
    q = request.rel_url.query
    k = (q.get("key") or q.get("token") or "").strip()
    if k:
        return k
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _mask(s: str, keep: int = 12) -> str:
    if not s or len(s) <= keep:
        return "***"
    return s[:keep] + "..."


def _body_preview(body: bytes, limit: int = 1000) -> str:
    return body.decode("utf-8", errors="replace")[:limit]


def _parse_error_details(body: bytes) -> tuple[str, str, dict | None]:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return "", _body_preview(body), None

    if not isinstance(parsed, dict):
        return "", _body_preview(body), None

    error = parsed.get("error", {})
    if not isinstance(error, dict):
        error = {}
    return (
        str(error.get("type", "")).strip(),
        str(error.get("message", "")).strip() or _body_preview(body),
        parsed,
    )


def _extract_response_request_id(
    headers: dict[str, str],
    parsed: dict | None = None,
) -> str:
    return str(
        (parsed or {}).get("request_id")
        or headers.get("request-id")
        or headers.get("x-request-id")
        or headers.get("anthropic-request-id")
        or ""
    ).strip()


# ---------------------------------------------------------------------------
# Unexpected-failure alerting
# ---------------------------------------------------------------------------

_SRC_ROOT = os.path.dirname(os.path.abspath(__file__))

# Alert plumbing for the handful of call sites that have no app handle -- chiefly
# _record_anthropic_event, which the key pool calls from a dozen places and which
# would otherwise need `app` threaded through all of them. Set once in
# _on_startup, cleared in _on_cleanup; an explicit `app` argument always wins.
_ALERT_FALLBACK: dict[str, object] = {}


def _last_in_repo_frame(exc: BaseException) -> str:
    """``file.py:lineno`` of the deepest frame inside this package, else ""..

    The deepest *own* frame is what identifies the bug: the outermost frames are
    aiohttp's, and the innermost are usually the DB driver's.
    """
    tb = exc.__traceback__
    label = ""
    while tb is not None:
        filename = tb.tb_frame.f_code.co_filename
        if os.path.dirname(os.path.abspath(filename)) == _SRC_ROOT:
            label = f"{os.path.basename(filename)}:{tb.tb_lineno}"
        tb = tb.tb_next
    return label


def _alert_failure(
    app: object | None = None,
    *,
    source: str,
    exc: BaseException | None = None,
    detail: str = "",
) -> None:
    """Fire one throttled Telegram alert about an unexpected in-process failure.

    Never raises. Every caller is an ``except`` block whose whole job is to keep
    the proxy serving, so a problem in here -- a traceback walk, a formatting
    slip -- must not replace the failure it is trying to report.
    """
    try:
        carrier = app if app is not None else _ALERT_FALLBACK
        notifier = carrier.get("_notifier")  # type: ignore[union-attr]
        if notifier is None:
            return
        frame = _last_in_repo_frame(exc) if exc is not None else ""
        exc_name = type(exc).__name__ if exc is not None else ""
        # Keyed on the failing *line*, not the handler: the catch-all route
        # funnels all proxied traffic through one handler, so handler-name
        # granularity would file two unrelated bugs under one signature.
        signature = f"{exc_name}@{frame or source}"
        throttle = carrier.get("_alert_throttle")  # type: ignore[union-attr]
        repeats = throttle.should_send(signature) if throttle is not None else 0
        if repeats is None:
            return

        parts = [f"🔴 {_SERVICE_NAME} · failure: {source}"]
        if exc is not None:
            parts.append(f"{exc_name}: {str(exc)[:300] or '—'}")
        if frame:
            parts.append(frame)
        if detail:
            parts.append(detail)
        if repeats:
            parts.append(f"…and {repeats} more time(s) in the last 30 min")
        task = asyncio.create_task(notifier.notify("\n".join(parts)))
        tasks = carrier.get("_alert_tasks")  # type: ignore[union-attr]
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except Exception:
        logger.exception("failure alert could not be sent for %s", source)


def _memory_is_fresher(mem_expires_at: object, row_expires_at: object) -> bool:
    """True when the in-memory token is strictly newer than the stored one.

    Anthropic rotates the refresh token on every refresh, so a strictly newer
    expiry identifies the strictly newer — and therefore only living — token.
    Both sides must be known: an unset expiry proves nothing either way.
    """
    return (
        isinstance(mem_expires_at, int)
        and isinstance(row_expires_at, int)
        and mem_expires_at > row_expires_at
    )


def _is_upstream_overload(status_code: int, body: bytes) -> bool:
    """True for Anthropic-wide capacity pressure, as opposed to any other 5xx.

    529 is the documented overload status, but the same condition also arrives
    as an ``overloaded_error`` body, so match either. Everything else in the 5xx
    range stays on the ordinary retry path.
    """
    if status_code == 529:
        return True
    if status_code < 500:
        return False
    error_type, _message, _parsed = _parse_error_details(body)
    return error_type == "overloaded_error"


@dataclass
class _AttemptFailure:
    """Why one attempt in the proxy retry loop gave up.

    ``ours`` separates a fault we can act on -- a blocked refresh, a dead token,
    a transport error reaching Anthropic -- from upstream capacity problems,
    which are not our outage and deliberately do not raise an alert.
    """
    reason: str
    ours: bool


def _summarise_attempt_failures(failures: list[_AttemptFailure]) -> str:
    """"upstream 529 x3, transport: ConnectError" -- ordered, de-duplicated."""
    counts: dict[str, int] = {}
    for failure in failures:
        counts[failure.reason] = counts.get(failure.reason, 0) + 1
    return ", ".join(
        reason if n == 1 else f"{reason} ×{n}" for reason, n in counts.items()
    )


def _caller_label_for(pool: object, proxy_key: str) -> str:
    """Label the caller, tolerating a pool double that predates proxy_key_name."""
    lookup = getattr(pool, "proxy_key_name", None)
    return _caller_label(lookup(proxy_key) if lookup else "", proxy_key)


def _caller_label(name: str, proxy_key: str) -> str:
    """Name the consumer without printing a usable key: "Acme (webapp) ...ffee11".

    Only real ``sp-`` keys get a tail; internal markers such as the passthrough
    sentinel would otherwise render as "... hrough" and read like a masked key.
    """
    if not proxy_key.startswith("sp-"):
        return name or proxy_key or "—"
    tail = proxy_key[-6:] if len(proxy_key) >= 6 else ""
    return f"{name or '—'} …{tail}" if tail else (name or "—")


@web.middleware
async def _failure_alert_middleware(request: web.Request, handler):  # noqa: ANN001
    """Report any exception escaping a handler, then let aiohttp answer as before.

    Catching ``Exception`` (not ``BaseException``) lets ``asyncio.CancelledError``
    through untouched for free. Two things are deliberately not failures:
    deliberate ``HTTPException`` responses, and a client hanging up mid-stream --
    ``StreamResponse.write`` raises ``ClientConnectionResetError`` (a
    ``ConnectionResetError``) and Claude Code's stream watchdog makes that a
    daily event, not an incident.

    Note the alert is all this can add once the response is prepared: after
    ``prepare()`` there is no 500 left to send and aiohttp simply closes the
    connection on the client.
    """
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except ConnectionResetError:
        raise
    except Exception as exc:
        _alert_failure(
            request.app, source=f"{request.method} {request.path}", exc=exc,
        )
        raise


def _watch_background_task(app: object, name: str, task: asyncio.Task) -> None:
    """Alert if a background loop ever stops.

    These loops are ``while True``; a task that has exited raises nothing ever
    again, so the alert calls inside its own ``except`` blocks are unreachable
    precisely when the loop is gone. Cancellation is a clean shutdown -- anything
    else, including a quiet return, is a fault.
    """

    def _on_done(finished: asyncio.Task) -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is None:
            _alert_failure(
                app, source=f"background task {name} exited",
                detail="the loop returned without an error — it is never supposed to finish",
            )
        else:
            _alert_failure(app, source=f"background task {name} crashed", exc=exc)

    task.add_done_callback(_on_done)


async def _record_anthropic_event(
    db: Database | None,
    *,
    key_id: str,
    event_type: str,
    op_id: str = "",
    source: str = "",
    decision: str = "",
    path: str = "",
    model: str | None = None,
    http_status: int | None = None,
    request_id: str = "",
    error_type: str = "",
    error_message: str = "",
    retry_after: int | None = None,
    context: dict | None = None,
    app: object | None = None,
) -> None:
    if db is None:
        return
    try:
        await db.record_anthropic_key_event(
            key_id=key_id,
            event_type=event_type,
            op_id=op_id,
            source=source,
            decision=decision,
            path=path,
            model=model,
            http_status=http_status,
            request_id=request_id,
            error_type=error_type,
            error_message=error_message,
            retry_after=retry_after,
            context=context,
        )
    except Exception as exc:
        # Observability only -- never fail a live request over it. This ran
        # unguarded inside ensure_valid_token(), *before* the upstream /token
        # call, so a broken anthropic_key_events write 500'd the caller and
        # left the key unrefreshed instead of merely losing an audit row.
        # Token persistence keeps its own durability path and is unaffected.
        logger.warning(
            "audit event %s for key %s not recorded: %s",
            event_type, key_id[:12], exc,
        )
        # Swallowed, but not silent: a dead audit table hides exactly the
        # refresh diagnostics an outage is read from.
        _alert_failure(app, source=f"audit record {event_type}", exc=exc)


# ---------------------------------------------------------------------------
# Proxy handler
# ---------------------------------------------------------------------------

# The Claude Code version behind every fingerprint below is not pinned here —
# it lives in app["claude_code_version"] and is rendered per request. See
# smart_proxy.claude_code_identity for why (Anthropic gates models on it).
_CLAUDE_LIKE_BETA_BASE = (
    "interleaved-thinking-2025-05-14,"
    "redact-thinking-2026-02-12,"
    "context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,"
    "claude-code-20250219"
)
# Captured from a real Claude Code 2.1.260. These track the SDK and Node that
# the CLI bundles — an axis independent of the Claude Code version above, not
# derivable from it, so they are not rendered from it and must not be folded
# into its learning. Unlike cc_version they are cosmetic: upstream accepts a
# gated model with these stale, nonsensical, or absent entirely (measured
# 2026-09-04, on the same request where an old cc_version still returned 400).
# So they may drift without breaking anything; refresh them only to keep the
# fingerprint coherent, by capturing a current CLI request.
_CLAUDE_LIKE_HEADER_OVERRIDES = {
    "X-Stainless-Package-Version": "0.112.1",
    "X-Stainless-Runtime-Version": "v26.3.0",
    "Accept-Encoding": "gzip, deflate, br, zstd",
}
_CLAUDE_LIKE_STRIP_HEADERS = (
    "x-stainless-helper-method",
    "Accept-Language",
    "sec-fetch-mode",
)

_OAUTH_BETAS = (
    "claude-code-20250219,oauth-2025-04-20,"
    "interleaved-thinking-2025-05-14,"
    "context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05"
)
_STREAM_HEAD_MAX = 64 * 1024
_STREAM_COMMIT_MARKERS = (
    b'"type":"content_block_delta"',
    b'"type":"input_json_delta"',
    b'"type":"message_delta"',
    b'"type":"content_block_stop"',
    b'"type":"message_stop"',
)
_STREAM_ERROR_MARKERS = (
    b"\nevent: error\n",
    b'"type":"error"',
)
_SMOKE_MODEL = "claude-haiku-4-5-20251001"
_PARIS_TZ = ZoneInfo("Europe/Paris")
_SMOKE_WINDOW_RE = re.compile(
    r"^(?P<start_hour>\d{2}):(?P<start_minute>\d{2})-(?P<end_hour>\d{2}):(?P<end_minute>\d{2})$"
)


def _merge_beta_flags(existing: str | None, required: str) -> str:
    """Return union of existing and required beta flags preserving order."""
    merged: list[str] = []
    seen: set[str] = set()
    for chunk in (existing or "", required):
        for flag in chunk.split(","):
            item = flag.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return ",".join(merged)


def _strip_1m_context_beta(existing: str | None) -> str | None:
    """Remove 1M context beta flags while preserving all other tokens."""
    if existing is None:
        return None
    kept = [
        flag.strip()
        for flag in existing.split(",")
        if flag.strip() and not flag.strip().startswith("context-1m-")
    ]
    return ",".join(kept) if kept else None


def _find_header_key(headers: dict[str, str], target: str) -> str | None:
    target_lower = target.lower()
    for key in headers:
        if key.lower() == target_lower:
            return key
    return None


def _get_header_value(headers: dict[str, str], target: str) -> str | None:
    key = _find_header_key(headers, target)
    return headers.get(key) if key is not None else None


def _set_header_value(headers: dict[str, str], target: str, value: str) -> None:
    key = _find_header_key(headers, target)
    headers[key or target] = value


def _setdefault_header(headers: dict[str, str], target: str, value: str) -> None:
    if _find_header_key(headers, target) is None:
        headers[target] = value


def _pop_header(headers: dict[str, str], target: str) -> None:
    key = _find_header_key(headers, target)
    if key is not None:
        headers.pop(key, None)


def _apply_claude_like_headers(
    headers: dict[str, str], claude_code_version: str
) -> dict[str, str]:
    merged = dict(headers)
    for header in _CLAUDE_LIKE_STRIP_HEADERS:
        _pop_header(merged, header)

    existing_beta = (_get_header_value(merged, "anthropic-beta") or "").strip()
    if not any("claude-code" in flag.strip() for flag in existing_beta.split(",") if flag.strip()):
        _set_header_value(merged, "anthropic-beta", _CLAUDE_LIKE_BETA_BASE)

    user_agent = (_get_header_value(merged, "User-Agent") or "").strip()
    if not user_agent.startswith("claude-cli/"):
        _set_header_value(
            merged, "User-Agent", render_cli_user_agent(claude_code_version)
        )

    if not ((_get_header_value(merged, "X-Claude-Code-Session-Id") or "").strip()):
        _set_header_value(merged, "X-Claude-Code-Session-Id", str(uuid4()))

    if not ((_get_header_value(merged, "x-app") or "").strip()):
        _set_header_value(merged, "x-app", "cli")

    for header, value in _CLAUDE_LIKE_HEADER_OVERRIDES.items():
        _set_header_value(merged, header, value)
    return merged


def _extract_model(body: bytes) -> str | None:
    try:
        data = json.loads(body)
        return data.get("model") if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _request_debug_summary(body: bytes) -> dict[str, object]:
    """Return compact request metadata for diagnostics without full payload logging."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}

    messages = data.get("messages")
    system = data.get("system")
    container = data.get("container")
    summary: dict[str, object] = {
        "model": data.get("model"),
        "stream": data.get("stream"),
        "messages_count": len(messages) if isinstance(messages, list) else None,
        "system_count": len(system) if isinstance(system, list) else (1 if isinstance(system, str) else 0),
        "has_container": isinstance(container, dict),
        "top_keys": sorted(list(data.keys()))[:12],
    }
    if isinstance(container, dict):
        cid = str(container.get("id", "")).strip()
        if cid:
            summary["container_id"] = _mask(cid, keep=10)
    for key in ("parent_message_id", "conversation_id", "session_id"):
        value = str(data.get(key, "")).strip()
        if value:
            summary[key] = _mask(value, keep=10)
    return summary


def record_kwargs_for(data: object, headers) -> dict:
    """Classification kwargs for UsageTracker.record; never raises."""
    try:
        if not isinstance(data, dict):
            raise TypeError(f"data must be dict, got {type(data).__name__}")
        rc = classify_request(
            data,
            user_agent=headers.get("user-agent", "") if headers else "",
            x_app=headers.get("x-app", "") if headers else "",
        )
        return {
            "request_kind": rc.kind,
            "session_id": rc.session_id,
            "project": rc.project,
            "title": rc.title,
        }
    except Exception:
        return {"request_kind": "unknown", "session_id": "", "project": "", "title": ""}


def _safe_json_obj(body: bytes) -> dict:
    """Best-effort parse of a request body into a dict; never raises."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _strip_system_phrase(body: bytes, phrase: str) -> bytes:
    """Remove a configured phrase from system text blocks only."""
    if not phrase:
        return body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    if not isinstance(data, dict):
        return body

    system = data.get("system")
    if not isinstance(system, list):
        return body

    changed = False
    new_system: list[object] = []
    for item in system:
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ):
            text = item["text"]
            new_text = text.replace(phrase, "")
            if new_text != text:
                updated = dict(item)
                updated["text"] = new_text
                item = updated
                changed = True
        new_system.append(item)

    if not changed:
        return body

    data["system"] = new_system
    return json.dumps(data, separators=(",", ":")).encode()


def _inject_billing_header(body: bytes, claude_code_version: str) -> bytes:
    """Prepend Claude Code billing header to the system prompt.

    Anthropic reads the model version gate out of this block, so the version it
    carries decides whether gated models answer at all for clients that don't
    send a block of their own.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    if not isinstance(data, dict):
        return body

    billing = {"type": "text", "text": render_billing_header(claude_code_version)}
    system = data.get("system")

    def _has_billing(items: list) -> bool:
        return any(
            "x-anthropic-billing-header" in (s.get("text", "") if isinstance(s, dict) else str(s))
            for s in items
        )

    if isinstance(system, list):
        if not _has_billing(system):
            data["system"] = [billing] + system
    elif isinstance(system, str):
        if "x-anthropic-billing-header" not in system:
            data["system"] = [billing, {"type": "text", "text": system}]
    else:
        data["system"] = [billing]

    # Do NOT force streaming. The OAuth/Claude-Code upstream serves plain JSON
    # for non-streaming requests just fine; forcing stream:true here made the
    # proxy answer non-streaming clients (e.g. the Anthropic SDK's
    # messages.create) with SSE, which they mis-parse and crash on .content.
    # Respect whatever the client sent (absent => Anthropic defaults to JSON).
    return json.dumps(data, separators=(",", ":")).encode()


def _is_real_claude_code_cli(request: web.Request) -> bool:
    """True when the *incoming* request came from the Claude Code CLI.

    Checked against the original User-Agent, before the proxy normalizes it to
    a claude-cli/* string for the upstream. Other clients (SDKs, custom tools)
    send a different UA and are left untouched.
    """
    ua = (request.headers.get("User-Agent") or "").strip()
    return ua.startswith("claude-cli/")


def _is_claude_code_subagent(request: web.Request) -> bool:
    """True when the request is a Claude Code sub-agent (Task) call.

    Claude Code tags every sub-agent request with an ``x-claude-code-agent-id``
    header (absent on the main interactive session and on housekeeping calls, and
    independent of the sub-agent's model). Sub-agents run in tight back-to-back
    loops that keep a 5-minute cache warm on their own, so a 1-hour TTL buys them
    no extra read hits — only the doubled cache-write premium. Excluded from the
    TTL upgrade so only the long-lived main session gets the 1h cache.
    """
    return bool((request.headers.get("x-claude-code-agent-id") or "").strip())


_CLAUDE_CODE_SYSTEM_MARKER = "you are claude code"
_CLAUDE_CODE_OAUTH_BETA = "oauth-2025-04-20"


def _claude_code_signal(request: web.Request, req_body: dict | None) -> str:
    """Name of the first signal identifying the request as Claude Code, else ''.

    Read against the request *as the client sent it*. Both the headers and
    ``req_body`` are still pristine at the call site: the proxy's own rewriting
    (``_forward_headers`` normalises the User-Agent, ``_apply_oauth_headers`` and
    ``_apply_claude_like_headers`` set ``x-app: cli`` and the oauth beta) happens
    on a per-attempt copy further down. Evaluating this after that point would
    make every signal self-inflicted and the gate useless.

    This is a denylist over client-controlled hints, not a security boundary — a
    determined holder of a scoped proxy key can strip all of them. It exists to
    keep *our own* Claude Code sessions off a paid credential; the scope list and
    the per-key spend limit are what actually bound the damage.
    """
    ua = (request.headers.get("User-Agent") or "").strip()
    if ua.startswith("claude-cli/"):
        return "user_agent"
    if (request.headers.get("x-claude-code-agent-id") or "").strip():
        return "agent_id_header"
    if (request.headers.get("x-app") or "").strip().lower() == "cli":
        return "x_app"
    if _CLAUDE_CODE_OAUTH_BETA in (request.headers.get("anthropic-beta") or "").lower():
        return "oauth_beta"
    if isinstance(req_body, dict):
        if _system_text(req_body.get("system")).lstrip()[:64].lower().startswith(
            _CLAUDE_CODE_SYSTEM_MARKER
        ):
            return "system_prompt"
        if _session_id(req_body.get("metadata")):
            return "session_metadata"
    return ""


def _fallback_admission(
    request: web.Request, req_body: dict | None, proxy_key: str, limiter
) -> tuple[str | None, str]:
    """Decide once per request whether it may escalate to the paid fallback tier.

    Returns ``(fallback_for, deny_reason)``: ``fallback_for`` is what to hand
    :meth:`AnthropicKeyPool.pick`, ``None`` meaning "subscription tier only".

    Three independent gates, all fail-closed:

    * a real ``sp-`` proxy key — the ``claude-passthrough`` bucket can never
      reach a paid key. Since ``check_auth`` was tightened only a configured
      key gets this far, so that bucket now means "authenticated, but by a key
      that is not ``sp-``-prefixed": vestigial, and still fail-closed;
    * no Claude Code fingerprint, whoever's proxy key it is;
    * a *currently configured* spend limit on that proxy key. Checking it here
      rather than only when the scope is edited means clearing a key's limit
      revokes its access to the paid credential on the next request, instead of
      leaving an unbounded consumer behind a stale validation.
    """
    if not proxy_key.startswith("sp-"):
        return None, "not_a_proxy_key"
    signal = _claude_code_signal(request, req_body)
    if signal:
        return None, f"claude_code:{signal}"
    if limiter is None or not limiter.limits_for(proxy_key):
        return None, "no_spend_limit"
    return proxy_key, ""


def _upgrade_cache_ttl(body: bytes, ttl: str = "1h") -> bytes:
    """Upgrade 5-minute ephemeral cache breakpoints to a longer TTL.

    Claude Code emits cache_control: {"type": "ephemeral"} (5-minute default,
    or an explicit "5m") on its breakpoints. Bumping those to 1h keeps large
    prefixes (system prompt, tools, conversation history) cached across the
    pauses between turns, trading a 2x cache-write for avoided full re-writes.
    Blocks already at the target TTL — or with any other explicit TTL — are left
    as-is, and no new breakpoints are added (the 4-breakpoint limit is untouched).
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body

    changed = False

    def _walk(node: object) -> None:
        nonlocal changed
        if isinstance(node, dict):
            cc = node.get("cache_control")
            if (
                isinstance(cc, dict)
                and cc.get("type") == "ephemeral"
                and cc.get("ttl") in (None, "5m")
            ):
                cc["ttl"] = ttl
                changed = True
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(data)
    if not changed:
        return body
    return json.dumps(data, separators=(",", ":")).encode()


def _stream_has_commit_marker(buf: bytes) -> bool:
    return any(marker in buf for marker in _STREAM_COMMIT_MARKERS)


def _stream_has_error_marker(buf: bytes) -> bool:
    return any(marker in buf for marker in _STREAM_ERROR_MARKERS)


async def _buffer_stream_until_commit(
    response,  # noqa: ANN001
    *,
    timeout: float = 10.0,
) -> tuple[bytes, object, str]:
    """Buffer the first Anthropic SSE bytes before committing a response.

    Anthropic occasionally returns HTTP 200 but closes the SSE stream after only
    ``message_start`` or ``content_block_start``. By holding the head briefly we
    can classify those cases as retryable instead of leaking a broken stream to
    the client.

    The hold is time-bounded by ``timeout`` seconds (``<= 0`` disables the bound).
    During a long thinking / slow-first-token phase Anthropic keeps the connection
    alive with ``event: ping`` frames, which are neither commit nor error markers.
    Without a bound we would keep swallowing them and forward nothing downstream,
    so a Cloudflare edge in front of the proxy hits its ~100s response timeout and
    returns a 524. Once the budget elapses we commit the buffered head and fall
    through to live pass-through, where those pings reach the client and keep the
    connection warm. The deadline is only checked between reads (never mid-read),
    so no in-flight httpx read is cancelled.
    """
    initial_buf = b""
    aiter = response.aiter_bytes().__aiter__()
    deadline = time.monotonic() + timeout if timeout > 0 else None

    while True:
        if _stream_has_error_marker(initial_buf):
            return initial_buf, aiter, "error"
        if _stream_has_commit_marker(initial_buf):
            return initial_buf, aiter, "commit"
        if len(initial_buf) >= _STREAM_HEAD_MAX:
            return initial_buf, aiter, "commit"
        if deadline is not None and time.monotonic() >= deadline:
            logger.info(
                "Pre-commit buffer budget (%.1fs) elapsed with no commit marker; "
                "flushing %d buffered bytes to keep the downstream connection warm",
                timeout, len(initial_buf),
            )
            return initial_buf, aiter, "commit"
        try:
            initial_buf += await aiter.__anext__()
        except StopAsyncIteration:
            return initial_buf, aiter, "eof"


def _extract_sse_error_details(buf: bytes) -> tuple[str, str]:
    """Pull (error_type, error_message) out of a buffered SSE error event.

    Anthropic delivers stream-level errors as ``event: error`` followed by a
    ``data: {json}`` line. Returns ("", preview) when no structured error
    payload can be located, so callers always get something to log.
    """
    for raw_line in buf.split(b"\n"):
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:"):].strip()
        if b'"error"' not in payload and b'"type":"error"' not in payload:
            continue
        error_type, error_message, _ = _parse_error_details(payload)
        if error_type or error_message:
            return error_type, error_message
    return "", _body_preview(buf)


async def _handle_stream_failure_before_commit(
    *,
    kind: str,  # "error" | "truncated"
    buf: bytes,
    key: _AnthropicKey,
    pool: "AnthropicKeyPool",
    db: "Database | None",
    op_id: str,
    path: str,
    model: str | None,
) -> tuple[str, str]:
    """Log + persist a broken-SSE-before-commit event and cool the key down.

    Returns ``(error_type, error_message)`` so the caller can decide what to do
    next (rotate keys, or surface a clean status to the client).

    OAuth keys are NOT cooled down: there is typically a single subscription
    key, so a transient overload/truncation must not brick the whole proxy for
    30s. API keys (many, pulled) are still cooled so the pool rotates away.

    ``overloaded_error`` is special: it is Anthropic-wide capacity pressure, not
    a key fault. We never cool the key (the client's retry must be able to reuse
    it) and the caller surfaces a clean 529 instead of burning local attempts —
    retrying the same overloaded backend in a tight, back-off-less loop only
    makes things worse, while Claude Code's retry engine already does
    exponential backoff + jitter and honours ``retry-after``.
    """
    if kind == "error":
        error_type, error_message = _extract_sse_error_details(buf)
        event_type = "sse_error_before_commit"
    else:
        error_type = ""
        error_message = f"stream truncated before commit ({len(buf)} bytes)"
        event_type = "sse_truncated_before_commit"

    is_overloaded = error_type == "overloaded_error"
    skip_cooldown = key.key_type == "oauth" or is_overloaded
    if is_overloaded:
        decision = "surface_529"
    elif skip_cooldown:
        decision = "retry_no_cooldown"
    else:
        decision = "cooldown"
    logger.warning(
        "Anthropic SSE %s before commit key=%s model=%s type=%s msg=%s bytes=%d decision=%s",
        kind,
        key.key_id[:12],
        model or "-",
        error_type or "-",
        (error_message or "")[:200],
        len(buf),
        decision,
    )
    await _record_anthropic_event(
        db,
        key_id=key.key_id,
        event_type=event_type,
        op_id=op_id,
        source="proxy_request",
        decision=decision,
        path=path,
        model=model,
        http_status=200,
        error_type=error_type or kind,
        error_message=error_message,
        context={"bytes": len(buf), "body": _body_preview(buf)},
    )
    if not skip_cooldown:
        pool.cooldown(key)
    return error_type, error_message


@dataclass(frozen=True)
class _UpstreamDecision:
    action: str
    status: int | None = None
    body: bytes = b""
    content_type: str = "application/json"
    passthrough_headers: dict[str, str] = field(default_factory=dict)


def _apply_oauth_headers(
    headers: dict[str, str],
    token: str,
    *,
    claude_code_version: str,
    disable_1m_context: bool = False,
    claude_like: bool = False,
) -> dict[str, str]:
    merged = dict(headers)
    if disable_1m_context:
        sanitized = _strip_1m_context_beta(_get_header_value(merged, "anthropic-beta"))
        if sanitized:
            _set_header_value(merged, "anthropic-beta", sanitized)
        else:
            beta_key = _find_header_key(merged, "anthropic-beta")
            if beta_key is not None:
                merged.pop(beta_key, None)
    if claude_like:
        merged = _apply_claude_like_headers(merged, claude_code_version)
    merged["Authorization"] = f"Bearer {token}"
    _set_header_value(
        merged,
        "anthropic-beta",
        _merge_beta_flags(_get_header_value(merged, "anthropic-beta"), _OAUTH_BETAS),
    )
    _setdefault_header(merged, "anthropic-version", "2023-06-01")
    _setdefault_header(merged, "anthropic-dangerous-direct-browser-access", "true")
    _setdefault_header(merged, "User-Agent", render_cli_user_agent(claude_code_version))
    _setdefault_header(merged, "x-app", "cli")
    return merged


def _build_oauth_smoke_request(
    client: httpx.AsyncClient,
    token: str,
    *,
    claude_code_version: str,
    disable_1m_context: bool = False,
    claude_like: bool = False,
):
    body = json.dumps(
        {
            "model": _SMOKE_MODEL,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        },
        separators=(",", ":"),
    ).encode()
    body = _inject_billing_header(body, claude_code_version)
    headers = _apply_oauth_headers(
        {},
        token,
        claude_code_version=claude_code_version,
        disable_1m_context=disable_1m_context,
        claude_like=claude_like,
    )
    return client.build_request(
        "POST",
        f"{UPSTREAM_BASE}/v1/messages?beta=true",
        headers=headers,
        content=body,
    )


def _parse_smoke_window(value: str) -> tuple[clock_time, clock_time]:
    match = _SMOKE_WINDOW_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            "Invalid smoke window format. Expected HH:MM-HH:MM, got "
            f"{value!r}"
        )

    start = clock_time(
        hour=int(match.group("start_hour")),
        minute=int(match.group("start_minute")),
    )
    end = clock_time(
        hour=int(match.group("end_hour")),
        minute=int(match.group("end_minute")),
    )
    if start >= end:
        raise ValueError(
            "Smoke window start must be earlier than end, got "
            f"{value!r}"
        )
    return start, end


def _window_bounds(
    day: date,
    *,
    window: tuple[clock_time, clock_time],
) -> tuple[datetime, datetime]:
    window_start, window_end = window
    return (
        datetime(
            day.year,
            day.month,
            day.day,
            window_start.hour,
            window_start.minute,
            tzinfo=_PARIS_TZ,
        ),
        datetime(
            day.year,
            day.month,
            day.day,
            window_end.hour,
            window_end.minute,
            tzinfo=_PARIS_TZ,
        ),
    )


def _draw_window_slot(
    day: date,
    *,
    window: tuple[clock_time, clock_time],
    rng: random.Random,
) -> datetime:
    window_start, window_end = _window_bounds(day, window=window)
    span_seconds = int((window_end - window_start).total_seconds())
    if span_seconds <= 0:
        raise ValueError("Smoke window must span at least one second")
    offset_seconds = rng.randrange(span_seconds)
    return window_start + timedelta(seconds=offset_seconds)


def _build_daily_oauth_smoke_schedule(
    *,
    now: datetime,
    rng: random.Random,
    morning_window: tuple[clock_time, clock_time],
    midday_window: tuple[clock_time, clock_time],
) -> _DailyOAuthSmokeSchedule:
    local_now = now.astimezone(_PARIS_TZ)
    day = local_now.date()
    return _DailyOAuthSmokeSchedule(
        current_day=day,
        morning_slot=_draw_window_slot(day, window=morning_window, rng=rng),
        midday_slot=_draw_window_slot(day, window=midday_window, rng=rng),
    )


def _ensure_daily_oauth_smoke_schedule(
    schedule: _DailyOAuthSmokeSchedule | None,
    now: datetime,
    rng: random.Random,
    morning_window: tuple[clock_time, clock_time],
    midday_window: tuple[clock_time, clock_time],
) -> _DailyOAuthSmokeSchedule:
    local_day = now.astimezone(_PARIS_TZ).date()
    if schedule is None or schedule.current_day != local_day:
        return _build_daily_oauth_smoke_schedule(
            now=now,
            rng=rng,
            morning_window=morning_window,
            midday_window=midday_window,
        )
    return schedule


def _mark_expired_smoke_windows(
    schedule: _DailyOAuthSmokeSchedule,
    *,
    now: datetime,
    morning_window: tuple[clock_time, clock_time],
    midday_window: tuple[clock_time, clock_time],
) -> None:
    local_now = now.astimezone(_PARIS_TZ)
    _, morning_end = _window_bounds(schedule.current_day, window=morning_window)
    _, midday_end = _window_bounds(schedule.current_day, window=midday_window)
    if local_now >= morning_end:
        schedule.morning_done = True
    if local_now >= midday_end:
        schedule.midday_done = True


def _next_pending_smoke_slot(
    schedule: _DailyOAuthSmokeSchedule,
) -> tuple[str, datetime] | None:
    pending: list[tuple[str, datetime]] = []
    if not schedule.morning_done:
        pending.append(("morning", schedule.morning_slot))
    if not schedule.midday_done:
        pending.append(("midday", schedule.midday_slot))
    if not pending:
        return None
    return min(pending, key=lambda item: item[1])


async def _classify_unsuccessful_response(
    *,
    response,
    key: _AnthropicKey,
    pool: AnthropicKeyPool,
    db: Database | None,
    source: str,
    op_id: str,
    path: str,
    model: str | None,
    body: bytes,
    attempt: int,
    max_attempts: int,
) -> _UpstreamDecision:
    status_code = response.status_code
    # aiohttp raises ValueError when the content_type kwarg carries a charset,
    # and Anthropic sits behind Cloudflare, whose 5xx interstitials are
    # "text/html; charset=UTF-8" — surfacing one verbatim would 500 the handler.
    content_type = (
        response.headers.get("content-type", "application/json").partition(";")[0].strip()
        or "application/json"
    )
    resp_body = await response.aread()
    await response.aclose()
    error_type, error_message, parsed_body = _parse_error_details(resp_body)
    request_id = _extract_response_request_id(dict(response.headers), parsed_body)

    if status_code in (401, 402, 403):
        error_msg = error_message.lower()

        is_billing = (
            status_code == 402
            or "credit" in error_msg
            or "balance" in error_msg
            or "billing" in error_msg
            or "too low" in error_msg
        )

        if status_code == 401:
            if "oauth authentication is currently not supported" in error_msg:
                logger.warning(
                    "OAuth unsupported for this endpoint; keeping key %s active: %s",
                    key.key_id[:12], resp_body[:200],
                )
                return _UpstreamDecision(
                    action="respond",
                    status=401,
                    body=resp_body,
                    content_type=content_type,
                )
            logger.warning(
                "Auth error 401 key %s -> inactive: %s",
                key.key_id[:12], resp_body[:200],
            )
            await _record_anthropic_event(
                db,
                key_id=key.key_id,
                event_type="upstream_auth_error",
                op_id=op_id,
                source=source,
                decision="deactivate",
                path=path,
                model=model,
                http_status=status_code,
                request_id=request_id,
                error_type=error_type or "authentication_error",
                error_message=error_message,
                context={
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "request": _request_debug_summary(body),
                },
            )
            await pool.deactivate(
                key,
                audit_op_id=op_id,
                audit_source=source,
                audit_path=path,
                audit_model=model,
                audit_http_status=status_code,
                audit_request_id=request_id,
                audit_error_type=error_type or "authentication_error",
                audit_error_message=error_message,
                audit_context={"request": _request_debug_summary(body)},
            )
            return _UpstreamDecision(action="retry")

        if is_billing:
            logger.warning(
                "Billing error %d key %s -> low_balance: %s",
                status_code, key.key_id[:12], resp_body[:200],
            )
            await pool.mark_low_balance(
                key,
                audit_op_id=op_id,
                audit_source=source,
                audit_path=path,
                audit_model=model,
                audit_http_status=status_code,
                audit_request_id=request_id,
                audit_error_type=error_type or "billing_error",
                audit_error_message=error_message,
                audit_context={"request": _request_debug_summary(body)},
            )
            return _UpstreamDecision(action="retry")

        if error_type == "authentication_error":
            logger.warning(
                "Auth error 403 key %s -> inactive: %s",
                key.key_id[:12], resp_body[:200],
            )
            await _record_anthropic_event(
                db,
                key_id=key.key_id,
                event_type="upstream_auth_error",
                op_id=op_id,
                source=source,
                decision="deactivate",
                path=path,
                model=model,
                http_status=status_code,
                request_id=request_id,
                error_type=error_type,
                error_message=error_message,
                context={
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "request": _request_debug_summary(body),
                },
            )
            await pool.deactivate(
                key,
                audit_op_id=op_id,
                audit_source=source,
                audit_path=path,
                audit_model=model,
                audit_http_status=status_code,
                audit_request_id=request_id,
                audit_error_type=error_type,
                audit_error_message=error_message,
                audit_context={"request": _request_debug_summary(body)},
            )
        else:
            logger.warning(
                "Permission error 403 key %s (not deactivated): %s",
                key.key_id[:12], resp_body[:200],
            )
        return _UpstreamDecision(action="retry")

    if status_code == 429:
        retry_after_hdr = response.headers.get("retry-after", "")
        try:
            retry_after_sec = int(float(retry_after_hdr)) if retry_after_hdr else None
        except (ValueError, TypeError):
            retry_after_sec = None

        reset_epoch = response.headers.get("anthropic-ratelimit-unified-reset")
        reset_iso = None
        if reset_epoch:
            try:
                reset_iso = datetime.fromtimestamp(int(reset_epoch), tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                pass

        limit_type = response.headers.get("anthropic-ratelimit-unified-representative-claim", "")
        try:
            util_5h = float(response.headers.get("anthropic-ratelimit-unified-5h-utilization", ""))
        except (ValueError, TypeError):
            util_5h = None
        try:
            util_7d = float(response.headers.get("anthropic-ratelimit-unified-7d-utilization", ""))
        except (ValueError, TypeError):
            util_7d = None

        if db is not None:
            try:
                await db.record_rate_limit(
                    provider="anthropic",
                    credential_id=key.key_id,
                    retry_after=retry_after_sec,
                    reset_at=reset_iso,
                    limit_type=limit_type,
                    utilization_5h=util_5h,
                    utilization_7d=util_7d,
                )
            except Exception as exc:
                # A 429 must always end in a cooldown and a failover. Losing the
                # analytics row is nothing; raising here would answer 500 on the
                # one path whose whole purpose is to switch keys and carry on.
                logger.warning("rate-limit row not persisted: %s", exc)
                _alert_failure(source="rate-limit row persist", exc=exc)

        logger.warning(
            "Rate limited (429) key %s  retry-after=%s  type=%s  5h=%.0f%%  7d=%.0f%%  req_id=%s  err_type=%s",
            key.key_id[:12], retry_after_hdr, limit_type,
            (util_5h or 0) * 100, (util_7d or 0) * 100,
            request_id or "-", error_type or "-",
        )
        if "/v1/messages" in path:
            logger.warning(
                "429 context model=%s attempt=%d/%d detail=%s request=%s",
                model,
                attempt,
                max_attempts,
                error_message[:180] if error_message else "-",
                _request_debug_summary(body),
            )

        await _record_anthropic_event(
            db,
            key_id=key.key_id,
            event_type="rate_limited",
            op_id=op_id,
            source=source,
            decision="cooldown",
            path=path,
            model=model,
            http_status=status_code,
            request_id=request_id,
            error_type=error_type or "rate_limit_error",
            error_message=error_message,
            retry_after=retry_after_sec,
            context={
                "limit_type": limit_type,
                "utilization_5h": util_5h,
                "utilization_7d": util_7d,
                "request": _request_debug_summary(body),
            },
        )
        pool.cooldown(key, retry_after_sec, model=model)
        passthrough_headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower().startswith(("retry-after", "anthropic-ratelimit", "x-ratelimit"))
        }
        return _UpstreamDecision(
            action="retry",
            status=429,
            body=resp_body,
            content_type=content_type,
            passthrough_headers=passthrough_headers,
        )

    if status_code >= 500:
        logger.warning(
            "Upstream %d with key %s: %s",
            status_code, key.key_id[:12], resp_body[:200],
        )
        # Carry the upstream answer back so the caller can surface it verbatim
        # once no untried key is left. Retrying an overloaded backend in a tight,
        # back-off-less loop only deepens the overload -- the client's own retry
        # engine honours retry-after and is strictly better placed to wait.
        return _UpstreamDecision(
            action="retry",
            status=status_code,
            body=resp_body,
            content_type=content_type,
            passthrough_headers={
                name: value
                for name, value in response.headers.items()
                if name.lower().startswith("retry-after")
            },
        )

    return _UpstreamDecision(action="retry")


def _extract_rate_limit_headers(
    headers: dict[str, str] | object,
) -> dict[str, object]:
    """Pull Anthropic unified rate-limit fields from response headers.

    Returns a flat dict with ``utilization_5h`` (float|None), ``utilization_7d``
    (float|None), ``limit_type`` (str), ``reset_iso`` (str|None).  Never raises.
    """
    out: dict[str, object] = {"utilization_5h": None, "utilization_7d": None,
                              "limit_type": "", "reset_iso": None,
                              "reset_7d": None}
    try:
        get = headers.get  # works for both dict and httpx.Headers
        out["limit_type"] = str(get("anthropic-ratelimit-unified-representative-claim", "") or "")
        # The 7d epoch specifically, not the representative one: a wipe is a
        # 7d utilization drop while *this* window's own reset stays put.
        reset_7d = get("anthropic-ratelimit-unified-7d-reset")
        if reset_7d:
            out["reset_7d"] = str(reset_7d)
        reset_epoch = get("anthropic-ratelimit-unified-reset")
        if reset_epoch:
            try:
                out["reset_iso"] = datetime.fromtimestamp(
                    int(str(reset_epoch)), tz=timezone.utc
                ).isoformat()
            except (ValueError, OSError):
                pass
        for key, attr in [("anthropic-ratelimit-unified-5h-utilization", "utilization_5h"),
                          ("anthropic-ratelimit-unified-7d-utilization", "utilization_7d")]:
            try:
                out[attr] = float(str(get(key, "")))
            except (ValueError, TypeError):
                pass
    except Exception:
        pass
    return out


async def _maybe_record_utilization(
    db: Database | None,
    *,
    key_id: str,
    headers: dict[str, str] | object,
    pool: "AnthropicKeyPool | None" = None,
    context: dict | None = None,
) -> None:
    """Persist unified rate-limit utilisation from *any* response, not just 429s.

    Called on every ``/v1/messages`` response so we can correlate utilisation with
    token counts and cost over time.  Never raises.
    """
    rl = _extract_rate_limit_headers(headers)
    if rl["utilization_5h"] is None and rl["utilization_7d"] is None:
        return

    # The per-request wipe channel. Runs before (and independently of) the
    # rate_limit_log write: it is memory-based, so it still fires with the
    # database down, and it is the only channel carrying a request-id.
    if pool is not None:
        try:
            utilization_7d = rl["utilization_7d"]
            wipe = pool.observe_unified_headers(
                key_id,
                # Headers report a fraction (0.65); the poll channel and the
                # oauth_limit_wipe column are percent. Convert, or the two
                # sources would write incomparable numbers to one column.
                None if utilization_7d is None else float(utilization_7d) * 100.0,
                rl.get("reset_7d"),  # type: ignore[arg-type]
            )
            if wipe is not None:
                wipe["context_json"] = json.dumps({
                    **(context or {}),
                    **_snapshot_headers(headers),
                })
                logger.warning(
                    "OAuth LIMIT WIPE (headers) key=%s %.1f%%->0%% "
                    "with 7d-reset=%s unchanged",
                    key_id[:12], wipe["from_utilization"],
                    wipe["resets_at_claimed"],
                )
                pool.alert_limit_wipe(wipe)
                if db is not None:
                    await db.record_oauth_limit_wipe(wipe)
        except Exception:
            logger.warning("header wipe channel failed for key %s",
                           key_id[:12], exc_info=True)

    if db is None:
        return
    try:
        await db.record_rate_limit(
            provider="anthropic",
            credential_id=key_id,
            retry_after=None,
            reset_at=rl["reset_iso"],
            limit_type=str(rl["limit_type"]) or "five_hour",
            utilization_5h=rl["utilization_5h"],
            utilization_7d=rl["utilization_7d"],
        )
    except Exception as exc:
        # Was a bare `pass` — a rate-limit observation lost without a trace is
        # how /_oauth_usage quietly goes stale.
        logger.warning("rate-limit observation not recorded: %s", exc)
        _alert_failure(source="rate-limit observation record", exc=exc)


def _is_billable_path(method: str, path: str) -> bool:
    """True for requests that can consume budget.

    ``count_tokens`` is free and clients call it constantly, so gating it would
    break them without protecting the budget.
    """
    return (
        method == "POST"
        and "/v1/messages" in path
        and not path.endswith("/count_tokens")
    )


async def _proxy_handler(request: web.Request) -> web.StreamResponse:
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    client: httpx.AsyncClient = request.app["http_client"]
    tracker: UsageTracker | None = request.app.get("usage_tracker")
    op_id = str(uuid4())

    token = _extract_client_token(request)
    if not pool.check_auth(token):
        return web.Response(
            status=401,
            body=b'{"error":"unauthorized"}',
            content_type="application/json",
        )

    usage_proxy_key = token if token.startswith("sp-") else "claude-passthrough"

    body = await request.read()
    path = request.path

    limiter: KeyLimiter | None = request.app.get("key_limiter")
    if limiter is not None and _is_billable_path(request.method, path):
        block = limiter.check(usage_proxy_key)
        if block is not None:
            message = (
                f"SmartProxy: you have reached your {block.label} limit. "
                f"Retry in {_humanize_seconds(block.retry_after)}"
            )
            logger.info(
                "429 spend limit: key=%s spent=%.4f limit=%.2f retry_after=%ds",
                _mask(usage_proxy_key), block.spent_usd, block.limit_usd,
                block.retry_after,
            )
            return web.Response(
                status=429,
                headers={
                    "retry-after": str(block.retry_after),
                    "x-should-retry": "false",
                },
                body=json.dumps({
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": message,
                    },
                }).encode(),
                content_type="application/json",
            )

    if request.method == "POST" and "/v1/messages" in path:
        body = _strip_system_phrase(body, request.app.get("strip_system_phrase", ""))
        if (
            request.app.get("upgrade_cache_ttl")
            and _is_real_claude_code_cli(request)
            and not _is_claude_code_subagent(request)
        ):
            body = _upgrade_cache_ttl(body)
    qs = request.query_string
    if qs:
        base_url = f"{UPSTREAM_BASE}{path}?{qs}"
    else:
        base_url = f"{UPSTREAM_BASE}{path}"

    model = _extract_model(body)
    req_body = _safe_json_obj(body)
    logger.info(">>> %s %s  bytes=%d  model=%s", request.method, path, len(body), model)

    fallback_for, fallback_denied = _fallback_admission(
        request, req_body, usage_proxy_key, limiter
    )

    # A real Claude Code CLI states its version twice — in the User-Agent and in
    # the billing block it puts first in the system prompt. Harvest it here, but
    # adopt it only once upstream has accepted this very request (below), so the
    # proxy never starts claiming a version Anthropic would reject. Read-only:
    # req_body is not mutated, so classification and admission are unaffected.
    cc_version: ClaudeCodeVersion = request.app["claude_code_version"]
    cc_version_candidate = cc_version.candidate(
        user_agent=request.headers.get("User-Agent"), req_body=req_body
    )

    max_attempts = max(len(pool._keys), 3)
    attempt_failures: list[_AttemptFailure] = []
    tried_key_ids: set[str] = set()
    for attempt in range(max_attempts):
        key = pool.pick(model=model, fallback_for=fallback_for)
        if key is None:
            if fallback_denied and pool.has_scoped_fallback(usage_proxy_key):
                # Otherwise "why didn't the backup kick in?" is unanswerable.
                logger.info(
                    "Paid fallback withheld for %s: %s",
                    _mask(usage_proxy_key), fallback_denied,
                )
            retry_after = pool.next_available_in(model=model, fallback_for=fallback_for)
            if retry_after > 0:
                retry_human = _humanize_seconds(retry_after)
                if model:
                    message = (
                        f"You've reached your {model} limit. "
                        f"Retry in {retry_human} or switch models with /model."
                    )
                else:
                    message = f"Rate limit reached. Retry in {retry_human}."
                return web.Response(
                    status=429,
                    # retry-after header stays raw seconds per the HTTP spec; the
                    # human-readable duration goes only in the client-facing message.
                    headers={
                        "retry-after": str(retry_after),
                        "x-should-retry": "false",
                    },
                    body=json.dumps({
                        "type": "error",
                        "error": {
                            "type": "rate_limit_error",
                            "message": message,
                        },
                    }).encode(),
                    content_type="application/json",
                )
            msg = "No available Anthropic keys"
            logger.error(msg)
            return web.Response(
                status=503,
                body=json.dumps({"type": "error", "error": {"type": "api_error", "message": msg}}).encode(),
                content_type="application/json",
            )

        tried_key_ids.add(key.key_id)
        effective_token = await pool.ensure_valid_token(
            key,
            client,
            audit_op_id=op_id,
            audit_source="proxy_request",
            audit_path=path,
            audit_model=model,
        )
        if effective_token == pool._REFRESH_BLOCKED:
            logger.info("Key %s refresh blocked, trying next key", key.key_id[:12])
            attempt_failures.append(_AttemptFailure("refresh blocked", ours=True))
            continue
        if effective_token is None:
            await pool.deactivate(
                key,
                audit_op_id=op_id,
                audit_source="proxy_request",
                audit_path=path,
                audit_model=model,
                audit_error_type="oauth_refresh_failed",
                audit_error_message="Failed to obtain a valid OAuth token before forwarding request",
                audit_context={"request": _request_debug_summary(body)},
            )
            attempt_failures.append(_AttemptFailure("no valid OAuth token", ours=True))
            continue

        if key.role == "standby":
            await pool.promote_to_primary(
                key, audit_op_id=op_id, audit_source="proxy_request",
                audit_path=path, audit_model=model,
            )
        elif key.role == "fallback":
            # Deliberately not promoted — a fallback key stays a fallback key.
            await pool.note_fallback_serve(
                key, usage_proxy_key, audit_op_id=op_id,
                audit_path=path, audit_model=model,
            )

        # Per-attempt, never in place: the oauth branch below rewrites body and URL
        # with Claude Code billing identity and ?beta=true. Rebinding the shared
        # variables would leak them into the *next* attempt — which, once a request
        # can escalate from a cooled oauth key to a paid api_key, means presenting
        # Claude Code billing metadata on an sk-ant- credential.
        attempt_body = body
        attempt_url = base_url
        fwd = _forward_headers(request)
        if key.key_type == "oauth":
            fwd = _apply_oauth_headers(
                fwd,
                effective_token,
                claude_code_version=cc_version.token,
                disable_1m_context=bool(request.app.get("disable_1m_context")),
                claude_like=bool(request.app.get("claude_like")),
            )
            if "/v1/messages" in path:
                attempt_body = _inject_billing_header(attempt_body, cc_version.token)
                if "beta=true" not in (qs or ""):
                    attempt_url = f"{UPSTREAM_BASE}{path}?beta=true"
                    if qs:
                        attempt_url += f"&{qs}"
        else:
            fwd["x-api-key"] = effective_token

        try:
            req = client.build_request(
                request.method,
                attempt_url,
                headers=fwd,
                content=attempt_body if attempt_body else None,
            )
            r = await client.send(req, stream=True)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(
                "Transport error with key %s: %s", key.key_id[:12], exc
            )
            attempt_failures.append(
                _AttemptFailure(f"transport: {type(exc).__name__}", ours=True)
            )
            continue

        if r.status_code in (401, 402, 403, 429) or r.status_code >= 500:
            outcome = await _classify_unsuccessful_response(
                response=r,
                key=key,
                pool=pool,
                db=request.app.get("db"),
                source="proxy_request",
                op_id=op_id,
                path=path,
                model=model,
                body=body,
                attempt=attempt + 1,
                max_attempts=max_attempts,
            )
            if outcome.action == "respond":
                return web.Response(
                    status=outcome.status,
                    body=outcome.body,
                    content_type=outcome.content_type,
                )
            if r.status_code == 429:
                next_key = pool.pick(model=model, fallback_for=fallback_for)
                if next_key is None:
                    resp = web.Response(
                        status=429,
                        body=outcome.body,
                        content_type=outcome.content_type,
                    )
                    for name, value in outcome.passthrough_headers.items():
                        resp.headers[name] = value
                    resp.headers["x-should-retry"] = "false"
                    return resp
            if _is_upstream_overload(r.status_code, outcome.body):
                # Anthropic-wide capacity, not a bad key. Try a *different*
                # untried key if the pool has one -- pick() is sticky and an
                # overload sets no cooldown, so it must be told to skip the key
                # that just failed. Once they are exhausted, surface the upstream
                # answer verbatim: a 529 the SDK understands and retries with its
                # own backoff beats a 502 it cannot interpret.
                #
                # Deliberately narrow. A plain 500/502/503 is often a blip that
                # the next attempt on the same key clears, so those keep the
                # original retry behaviour rather than being handed straight to
                # a caller that may have no retry logic at all.
                next_key = pool.pick(
                    model=model, fallback_for=fallback_for, exclude=tried_key_ids,
                )
                if next_key is None:
                    resp = web.Response(
                        status=outcome.status or r.status_code,
                        body=outcome.body,
                        content_type=outcome.content_type,
                    )
                    for name, value in outcome.passthrough_headers.items():
                        resp.headers[name] = value
                    logger.info(
                        "Surfacing upstream %d to %s (no untried key left)",
                        r.status_code,
                        _caller_label_for(pool, usage_proxy_key),
                    )
                    return resp
                attempt_failures.append(
                    _AttemptFailure(f"upstream {r.status_code}", ours=False)
                )
                continue
            attempt_failures.append(
                _AttemptFailure(
                    f"upstream {r.status_code}",
                    # An auth/permission status means this key is unusable and
                    # somebody has to act on it — that is ours, not Anthropic's
                    # capacity. Without this a 403 storm exhausts the attempts
                    # and the final 502 alerts nobody.
                    ours=r.status_code in (401, 402, 403),
                )
            )
            continue

        # Upstream accepted the request, so the version it carried is one
        # Anthropic honours right now — the only endorsement worth adopting.
        # Checked as an explicit 2xx: a 400 (e.g. claude_code_version_too_old)
        # is not filtered above and reaches this point on its way to the client.
        if cc_version_candidate is not None and 200 <= r.status_code < 300:
            cc_version.commit(cc_version_candidate)
            cc_version_candidate = None

        # Success path: stream response back to client
        is_stream = "text/event-stream" in (
            r.headers.get("content-type", "")
        )
        stream_aiter = None
        initial_stream_buf = b""
        if is_stream:
            initial_stream_buf, stream_aiter, stream_state = await _buffer_stream_until_commit(
                r, timeout=request.app.get("precommit_timeout", 10.0)
            )
            if stream_state == "error":
                err_type, err_msg = await _handle_stream_failure_before_commit(
                    kind="error",
                    buf=initial_stream_buf,
                    key=key,
                    pool=pool,
                    db=request.app.get("db"),
                    op_id=op_id,
                    path=path,
                    model=model,
                )
                retry_after = r.headers.get("retry-after")
                retry_after_ms = r.headers.get("retry-after-ms")
                await r.aclose()
                if err_type == "overloaded_error":
                    # Hand the overload straight to Claude Code as a clean HTTP
                    # 529: its retry engine (exponential backoff + jitter,
                    # honours retry-after) is strictly better than retrying the
                    # same overloaded backend here. Surfacing a real 529 status
                    # — rather than forwarding the SSE error event mid-stream —
                    # also avoids the SDK's broken mid-stream retry path. Safe to
                    # surface because nothing has been sent to the client yet.
                    resp = web.Response(
                        status=529,
                        reason="Overloaded",
                        body=json.dumps(
                            {
                                "type": "error",
                                "error": {
                                    "type": err_type,
                                    "message": err_msg or "Overloaded",
                                },
                            }
                        ).encode(),
                        content_type="application/json",
                    )
                    if retry_after is not None:
                        resp.headers["retry-after"] = retry_after
                    if retry_after_ms is not None:
                        resp.headers["retry-after-ms"] = retry_after_ms
                    return resp
                attempt_failures.append(
                    _AttemptFailure(f"stream error: {err_type or 'unknown'}", ours=False)
                )
                continue
            if stream_state == "eof" and not _stream_has_commit_marker(initial_stream_buf):
                await _handle_stream_failure_before_commit(
                    kind="truncated",
                    buf=initial_stream_buf,
                    key=key,
                    pool=pool,
                    db=request.app.get("db"),
                    op_id=op_id,
                    path=path,
                    model=model,
                )
                await r.aclose()
                # Truncation before the commit marker can be ours as easily as
                # theirs — precommit_timeout is our setting — so this counts as
                # our fault: a persistent one must not exhaust the attempts and
                # then return a 502 that alerts nobody.
                attempt_failures.append(
                    _AttemptFailure("stream truncated before commit", ours=True)
                )
                continue

        stream_resp = web.StreamResponse(status=r.status_code)
        for name, value in r.headers.items():
            if name.lower() in _STRIP_RESPONSE_HEADERS:
                continue
            stream_resp.headers[name] = value

        await stream_resp.prepare(request)
        head_buf = initial_stream_buf
        tail_buf = initial_stream_buf[-_TAIL_BUF_MAX:] if is_stream else b""
        resp_body_buf = b""
        try:
            if is_stream:
                if initial_stream_buf:
                    await stream_resp.write(initial_stream_buf)
                assert stream_aiter is not None
                async for chunk in stream_aiter:
                    await stream_resp.write(chunk)
                    tail_buf = (tail_buf + chunk)[-_TAIL_BUF_MAX:]
            else:
                async for chunk in r.aiter_bytes():
                    await stream_resp.write(chunk)
                    resp_body_buf += chunk
        except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
            logger.warning("Mid-stream read error: %s", exc)
        finally:
            await r.aclose()
        try:
            await stream_resp.write_eof()
        except (ConnectionResetError, ConnectionError, Exception) as exc:
            logger.debug("write_eof ignored (client disconnected): %s", exc)

        if tracker and model:
            if is_stream:
                hu = extract_usage_from_sse("anthropic", head_buf) if head_buf else None
                tu = extract_usage_from_sse("anthropic", tail_buf) if tail_buf else None
                inp = (hu[0] if hu and hu[0] else 0) or (tu[0] if tu and tu[0] else 0)
                out = (tu[1] if tu and tu[1] else 0) or (hu[1] if hu and hu[1] else 0)
                cache_read = (hu[2] if hu else 0) or (tu[2] if tu else 0)
                cache_create = (hu[3] if hu else 0) or (tu[3] if tu else 0)
                cache_create_5m = (hu[4] if hu else 0) or (tu[4] if tu else 0)
                cache_create_1h = (hu[5] if hu else 0) or (tu[5] if tu else 0)
                web_search_requests = (tu[6] if tu else 0) or (hu[6] if hu else 0)
                usage = (
                    inp, out, cache_read, cache_create, cache_create_5m, cache_create_1h, web_search_requests
                ) if (inp or out or cache_read or cache_create or cache_create_5m or cache_create_1h or web_search_requests) else None
            elif resp_body_buf:
                usage = extract_usage("anthropic", resp_body_buf)
            else:
                usage = None
            if usage:
                tracker.record(
                    usage_proxy_key, key.key_id, "anthropic", model,
                    usage[0], usage[1],
                    usage[2] if len(usage) > 2 else 0,
                    usage[3] if len(usage) > 3 else 0,
                    usage[4] if len(usage) > 4 else 0,
                    usage[5] if len(usage) > 5 else 0,
                    web_search_requests=usage[6] if len(usage) > 6 else 0,
                    group_name=(key.name or "").strip() or None,
                    via_openai_compat=request.headers.get("x-smart-proxy-openai-compat") == "1",
                    **record_kwargs_for(req_body, request.headers),
                )
                if limiter is not None:
                    try:
                        limiter.add(usage_proxy_key, model, usage)
                    except Exception as exc:
                        # The response is already streamed; never fail it here.
                        logger.exception("spend accounting failed")
                        _alert_failure(
                            request.app, source="per-key spend accounting", exc=exc,
                        )

        if "/v1/messages" in path:
            asyncio.create_task(
                _maybe_record_utilization(
                    request.app.get("db"),
                    key_id=key.key_id,
                    headers=r.headers,
                    pool=request.app.get("anthropic_pool"),
                    context={
                        "path": path,
                        "model": model,
                        "proxy_key": (usage_proxy_key or "")[:16],
                    },
                )
            )

        logger.info(
            "<<< %s %s status=%d  key=%s",
            request.method, path, r.status_code, key.key_id[:12],
        )
        return stream_resp

    msg = f"All {max_attempts} attempts failed"
    summary = _summarise_attempt_failures(attempt_failures)
    caller = _caller_label_for(pool, usage_proxy_key)
    logger.error("%s — %s: %s", msg, caller, summary or "no reason recorded")
    # It is a return, not a raise, so the middleware never sees it. Alert only
    # when at least one attempt died for a reason of ours: Anthropic running out
    # of capacity is not our outage, and waking someone for it trains them to
    # ignore the channel. Pure upstream exhaustion is surfaced to the caller
    # above and never reaches here.
    if any(failure.ours for failure in attempt_failures):
        _alert_failure(
            request.app, source="proxy could not serve the request",
            detail=f"{caller}: {max_attempts} attempts — {summary}\n{request.method} {path}",
        )
    return web.Response(
        status=502,
        body=json.dumps({"error": msg}).encode(),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

_MIN_DASHBOARD_SECRET_LEN = 16


def validate_dashboard_secret(secret: str) -> str | None:
    """Return why this secret is unusable, or None if it is fine.

    Empty is fine and means "nobody administers the dashboard". Anything else
    has to be long enough to survive guessing, and must not be shaped like a
    proxy or Anthropic key -- that shape means it was pasted from the wrong
    place, and it would be a credential in two systems at once.
    """
    secret = secret.strip()
    if not secret:
        return None
    if secret.startswith(("sp-", "sk-ant-")):
        return (
            "ANTHROPIC_PROXY_DASHBOARD_SECRET looks like a proxy or Anthropic key. "
            "It is a separate operator password -- generate a new one."
        )
    if len(secret) < _MIN_DASHBOARD_SECRET_LEN:
        return (
            f"ANTHROPIC_PROXY_DASHBOARD_SECRET must be at least "
            f"{_MIN_DASHBOARD_SECRET_LEN} characters; it guards every key in the pool."
        )
    return None


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


_OAUTH_USAGE_BETA = "oauth-2025-04-20"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate_resets_at_minute(raw: str) -> str | None:
    """Normalize an upstream resets_at to minute precision in UTC.

    Upstream jitters the fractional seconds between polls of the same
    window, so minute-truncated resets_at is the window's identity.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


_WINDOW_TOP_LEVEL_SKIP = frozenset({"limits", "extra_usage"})


def _extract_window_observations(usage: dict) -> list[dict]:
    """Flatten an /api/oauth/usage payload into window observations.

    Top-level window objects keep their JSON key as ``window_kind``
    (``five_hour``, ``seven_day``, ...); ``limits[]`` entries become
    ``limit:<kind>`` with the model display name appended for scoped
    limits (``limit:weekly_scoped:Fable``).
    """
    if not isinstance(usage, dict):
        return []
    out: list[dict] = []

    def _add(kind: str, raw_resets_at: object, utilization: object) -> None:
        if not isinstance(raw_resets_at, str):
            return
        resets_at = _truncate_resets_at_minute(raw_resets_at)
        if resets_at is None:
            return
        util = (
            float(utilization)
            if isinstance(utilization, (int, float)) and not isinstance(utilization, bool)
            else None
        )
        out.append({
            "window_kind": kind,
            "resets_at": resets_at,
            "resets_at_raw": raw_resets_at,
            "utilization": util,
        })

    for key, value in usage.items():
        if key in _WINDOW_TOP_LEVEL_SKIP or not isinstance(value, dict):
            continue
        if "resets_at" in value:
            _add(key, value.get("resets_at"), value.get("utilization"))

    limits = usage.get("limits")
    if isinstance(limits, list):
        for limit in limits:
            if not isinstance(limit, dict):
                continue
            kind = limit.get("kind")
            if not isinstance(kind, str) or not kind:
                continue
            name = f"limit:{kind}"
            scope = limit.get("scope")
            if isinstance(scope, dict):
                model = scope.get("model")
                if isinstance(model, dict):
                    display = model.get("display_name") or model.get("id")
                    if isinstance(display, str) and display:
                        name = f"{name}:{display}"
            _add(name, limit.get("resets_at"), limit.get("percent"))
    return out


def _canonicalize_usage(value: object) -> object:
    """Minute-truncate every ``resets_at``, recursively.

    Upstream jitters the fractional seconds of ``resets_at`` between polls of
    the same window; without this every poll would hash differently and the
    snapshot table would degenerate into one row per poll.
    """
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            if key == "resets_at" and isinstance(item, str):
                out[key] = _truncate_resets_at_minute(item) or item
            else:
                out[key] = _canonicalize_usage(item)
        return out
    if isinstance(value, list):
        return [_canonicalize_usage(item) for item in value]
    return value


def _canonical_usage_hash(usage: object) -> str:
    """Content hash of a usage payload, used only to deduplicate snapshots.

    Canonicalisation applies to the hash alone — the payload is stored
    verbatim, including the fields ``_extract_window_observations`` discards
    (``extra_usage``, ``limit_dollars``, ``severity``, ``is_active`` and the
    codename buckets whose ``resets_at`` is null). The trigger for a limit
    wipe is unknown, so dropping fields in advance is exactly the mistake that
    left the 2026-09-01 event unexplainable.
    """
    blob = json.dumps(
        _canonicalize_usage(usage),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _minutes_between_iso(a_iso: str, b_iso: str) -> float | None:
    """Signed distance from *a* to *b* in minutes; None if unparseable."""
    try:
        return (
            datetime.fromisoformat(b_iso) - datetime.fromisoformat(a_iso)
        ).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


@dataclass
class _WindowState:
    """Last observation of one logical window, held in memory on the pool.

    Detection reads this rather than the database so that a wipe during a
    database outage is still noticed and alerted — the breaker fails every
    query fast, and that is exactly when we would most want to know.
    """

    utilization: float | None
    resets_at: str
    resets_at_raw: str
    seen_at: str


def _detect_limit_wipes(
    prev_state: dict[str, _WindowState],
    observations: list[dict],
    *,
    key_id: str,
    seen_at: str,
) -> list[dict]:
    """Find undeclared wipes of a weekly counter in one observation batch.

    A wipe is a weekly-class counter falling from above zero to exactly zero
    while its ``resets_at`` stays put. Deliberately *not* "two or more kinds
    read zero": at every 5-hour boundary ``five_hour`` and ``limit:session``
    both read zero, and at the Thursday boundary all three weekly kinds do —
    that rule fires on ordinary rollovers and, once duplicate kinds are
    collapsed, still misses the real event, which carried a single logical
    counter.

    ``UTILIZATION_DROP_THRESHOLD_PP`` is not applied: a wipe from 3% to 0 is
    still a wipe, and a seven-day counter reaching exactly zero inside one
    two-minute poll is not natural decay.
    """
    collapsed: dict[str, dict] = {}
    for observation in observations:
        kind = canonical_window_kind(observation["window_kind"])
        if kind in collapsed and collapsed[kind]["window_kind"] == kind:
            continue  # keep the observation whose own kind is the canonical one
        collapsed[kind] = observation

    five_prev = prev_state.get("five_hour")
    five_now = collapsed.get("five_hour")
    five_hour_rolled = False
    five_hour_early_minutes: float | None = None
    if five_prev is not None and five_now is not None:
        drift = _minutes_between_iso(five_prev.resets_at, five_now["resets_at"])
        if drift is not None and abs(drift) > RESETS_AT_JITTER_TOLERANCE_MINUTES:
            five_hour_rolled = True
            # Measured against resets_at_raw, not resets_at: a window row keeps
            # its first-observed resets_at and absorbs upstream drift into the
            # raw value alone, which would put the smallest observed figure
            # (18.5 min) inside the jitter band.
            five_hour_early_minutes = _minutes_between_iso(
                seen_at, five_prev.resets_at_raw)

    wipes: list[dict] = []
    for kind in sorted(collapsed):
        if not is_weekly_window_kind(kind):
            continue
        observation = collapsed[kind]
        prev = prev_state.get(kind)
        utilization = observation.get("utilization")
        if prev is None or prev.utilization is None or utilization is None:
            continue
        if not (prev.utilization > 0 and utilization == 0):
            continue
        drift = _minutes_between_iso(prev.resets_at, observation["resets_at"])
        if drift is None or abs(drift) > RESETS_AT_JITTER_TOLERANCE_MINUTES:
            continue  # resets_at moved: an ordinary window reset, not a wipe
        minutes_left = _minutes_between_iso(seen_at, observation["resets_at"])
        wipes.append({
            "key_id": key_id,
            "window_kind": kind,
            "observed_at": seen_at,
            "prev_seen_at": prev.seen_at,
            "from_utilization": prev.utilization,
            "resets_at_claimed": observation["resets_at"],
            "hours_before_claimed": (
                None if minutes_left is None else minutes_left / 60.0),
            "five_hour_rolled": five_hour_rolled,
            "five_hour_early_minutes": five_hour_early_minutes,
            "source": "poll",
        })
    return wipes


def _format_refresh_due_in_human(expires_at: int | None) -> str | None:
    if expires_at is None:
        return None

    refresh_due_ms = int(expires_at) - _REFRESH_BUFFER_MS
    delta_seconds = int((refresh_due_ms - int(time.time() * 1000)) / 1000)
    if abs(delta_seconds) < 60:
        return "now"

    suffix = "ago" if delta_seconds < 0 else ""
    prefix = "in " if delta_seconds > 0 else ""
    remaining = abs(delta_seconds)
    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")

    return f"{prefix}{' '.join(parts[:2])}{(' ' + suffix) if suffix else ''}"


_SNAPSHOT_HEADER_WHITELIST: tuple[str, ...] = (
    "request-id",
    "date",
    "cf-ray",
    "anthropic-organization-id",
)


def _snapshot_headers(headers: object) -> dict[str, str]:
    """Keep the response headers that can identify *why* a payload changed.

    ``request-id`` is the only artifact Anthropic support can act on, and a
    changed ``anthropic-organization-id`` across a wipe would point at a plan
    or org migration.
    """
    try:
        items = {str(k).lower(): str(v) for k, v in headers.items()}  # type: ignore[union-attr]
    except AttributeError:
        return {}
    return {
        k: v for k, v in items.items()
        if k in _SNAPSHOT_HEADER_WHITELIST or k.startswith("anthropic-ratelimit-")
    }


async def _record_window_observations(
    db: Database,
    key_id: str,
    usage: dict,
    *,
    pool: "AnthropicKeyPool | None" = None,
    raw_body: str | None = None,
    headers: object = None,
    seen_at: str | None = None,
) -> None:
    """Persist window lifecycles from a usage payload; never raise.

    Wipe detection runs off the pool's in-memory state before any database
    call, so an undeclared wipe is still noticed and alerted while the
    database is unavailable. Persistence is best-effort and split so that a
    failing snapshot write cannot suppress the window/drop writes or the
    alert.
    """
    observations = _extract_window_observations(usage)
    if not observations:
        return
    seen_at = seen_at or _utc_now_iso()

    wipes: list[dict] = []
    if pool is not None:
        try:
            wipes = pool.observe_windows(key_id, observations, seen_at=seen_at)
        except Exception:
            logger.warning("wipe detection failed for key %s", key_id[:12],
                           exc_info=True)

    snapshot_id: int | None = None
    prev_snapshot_id: int | None = None
    try:
        if raw_body is not None:
            prev_snapshot_id = pool.last_snapshot_id(key_id) if pool else None
            snapshot_id = await db.record_oauth_usage_snapshot(
                key_id,
                payload_hash=_canonical_usage_hash(usage),
                payload_json=raw_body,
                headers_json=json.dumps(_snapshot_headers(headers)),
                seen_at=seen_at,
            )
            if pool is not None and snapshot_id is not None:
                pool.set_last_snapshot_id(key_id, snapshot_id)
    except Exception:
        logger.warning("usage snapshot not stored for key %s", key_id[:12],
                       exc_info=True)

    for wipe in wipes:
        logger.warning(
            "OAuth LIMIT WIPE key=%s kind=%s %.1f%%->0%% with resets_at=%s "
            "unchanged (%.1fh early); five_hour_rolled=%s early=%sm",
            key_id[:12], wipe["window_kind"], wipe["from_utilization"],
            wipe["resets_at_claimed"], wipe.get("hours_before_claimed") or 0.0,
            wipe["five_hour_rolled"], wipe.get("five_hour_early_minutes"),
        )
        if pool is not None:
            pool.alert_limit_wipe(wipe)
        try:
            await db.record_oauth_limit_wipe({
                **wipe,
                "snapshot_id": snapshot_id,
                "prev_snapshot_id": prev_snapshot_id,
            })
        except Exception:
            logger.warning("limit wipe not stored for key %s", key_id[:12],
                           exc_info=True)

    try:
        reset_events = await db.record_oauth_window_observations(
            key_id, observations, seen_at=seen_at)
        for event in reset_events:
            if event.get("type") == "utilization_drop":
                logger.info(
                    "OAuth window utilization drop key=%s kind=%s %.1f%%->%.1f%% "
                    "while claimed resets_at=%s (undeclared reset?)",
                    key_id[:12],
                    event["window_kind"],
                    event["from_utilization"],
                    event["to_utilization"],
                    event["resets_at"],
                )
                continue
            logger.info(
                "OAuth window reset key=%s kind=%s prev_resets_at=%s new_resets_at=%s span=%sd",
                key_id[:12],
                event["window_kind"],
                event["prev_resets_at"],
                event["new_resets_at"],
                event["span_days"],
            )
    except Exception:
        logger.warning(
            "Failed to record oauth window observations for key %s",
            key_id[:12],
            exc_info=True,
        )


async def _build_oauth_usage_payload(
    pool: AnthropicKeyPool,
    client: httpx.AsyncClient,
    db: Database,
    *,
    claude_code_version: str,
    include_inactive_oauth: bool = False,
) -> tuple[list[dict], dict | None]:
    rows = await db.list_anthropic_keys()
    oauth_rows = [r for r in rows if r.get("key_type") == "oauth"]
    if not include_inactive_oauth:
        oauth_rows = [
            r for r in oauth_rows
            if r.get("status") in ("active", "low_balance")
        ]
    out: list[dict] = []
    last_failure: dict | None = None
    usage_url = f"{UPSTREAM_BASE}/api/oauth/usage"
    usage_headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": render_code_user_agent(claude_code_version),
        "anthropic-beta": _OAUTH_USAGE_BETA,
    }

    for row in oauth_rows:
        if str(row.get("role") or "primary") in ("standby", "fallback"):
            continue   # standby: no usage GET, no refresh/activation footprint (usage == primary's)
        loaded_key = pool.get_loaded_key(str(row.get("id", ""))) if hasattr(pool, "get_loaded_key") else None
        key = loaded_key if isinstance(loaded_key, _AnthropicKey) else _anthropic_key_from_row(row)
        op_id = str(uuid4())
        entry: dict = {
            "id": key.key_id,
            "name": key.name,
            "status": row.get("status"),
            "expires_at": key.expires_at,
            "refresh_due_in_human": _format_refresh_due_in_human(key.expires_at),
        }
        token = await pool.ensure_valid_token(
            key,
            client,
            audit_op_id=op_id,
            audit_source="oauth_usage",
            audit_path="/api/oauth/usage",
        )
        if token == pool._REFRESH_BLOCKED:
            entry["error"] = "oauth_refresh_rate_limited"
            last_failure = {
                "seen_at": _utc_now_iso(),
                **entry,
            }
            out.append(entry)
            continue
        if token is None:
            entry["error"] = "no_valid_oauth_token"
            last_failure = {
                "seen_at": _utc_now_iso(),
                **entry,
            }
            out.append(entry)
            continue

        headers = {**usage_headers, "Authorization": f"Bearer {token}"}
        try:
            r = await client.get(usage_url, headers=headers, timeout=30.0)
        except httpx.HTTPError as exc:
            entry["error"] = "usage_request_failed"
            entry["error_detail"] = str(exc)
            last_failure = {
                "seen_at": _utc_now_iso(),
                **entry,
            }
            out.append(entry)
            continue

        entry["http_status"] = r.status_code
        if r.status_code == 200:
            try:
                entry["usage"] = r.json()
            except (json.JSONDecodeError, ValueError):
                entry["error"] = "usage_invalid_json"
                entry["usage_body_preview"] = r.text[:500]
                last_failure = {
                    "seen_at": _utc_now_iso(),
                    **entry,
                }
            else:
                # The raw upstream response, not the handler's payload: that one
                # is served from a shared 120 s cache and carries per-caller
                # smartproxy_* limits, so snapshotting there would re-observe
                # the cache with a fresh timestamp and hash caller-specific data.
                await _record_window_observations(
                    db,
                    key.key_id,
                    entry["usage"],
                    pool=pool,
                    raw_body=r.text,
                    headers=r.headers,
                    seen_at=_utc_now_iso(),
                )
        else:
            entry["error"] = "usage_upstream_error"
            try:
                entry["upstream"] = r.json()
            except (json.JSONDecodeError, ValueError):
                entry["upstream_body_preview"] = r.text[:500]
            last_failure = {
                "seen_at": _utc_now_iso(),
                **entry,
                "response_headers": {k.lower(): v for k, v in r.headers.items()},
            }

        out.append(entry)

    return out, last_failure


def _inject_smartproxy_limits(payload: dict, limiter, proxy_key: str) -> dict:
    """Append this proxy key's SmartProxy limits to each entry's ``usage.limits``.

    Returns a copy — the payload handed in may be the shared /_oauth_usage cache
    entry, which is served to every caller for up to a minute, so mutating it
    would leak one key's spend to another.
    """
    entries = [
        {
            "kind": f"smartproxy_{kind}",
            "source": "smartproxy",
            "limit_usd": info["limit_usd"],
            "spent_usd": info["spent_usd"],
            "percent": info["percent"],
            "resets_at": info["resets_at"],
        }
        for kind, info in limiter.snapshot(proxy_key).items()
        if info.get("limit_usd")
    ]
    if not entries:
        return payload

    out = dict(payload)
    new_keys = []
    for entry in payload.get("keys") or []:
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            new_keys.append(entry)
            continue
        new_usage = dict(usage)
        new_usage["limits"] = list(usage.get("limits") or []) + entries
        new_entry = dict(entry)
        new_entry["usage"] = new_usage
        new_keys.append(new_entry)
    out["keys"] = new_keys
    return out


def _smartproxy_limit_key(request: web.Request) -> str:
    """The ``?key=`` proxy key whose limits should be reported, or ''."""
    candidate = request.query.get("key", "").strip()
    if not candidate:
        return ""
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    return candidate if pool.is_proxy_key(candidate) else ""


async def _oauth_usage_handler(request: web.Request) -> web.Response:
    """GET platform usage for OAuth rows in ``anthropic_keys`` (Claude web OAuth).

    By default only ``active`` and ``low_balance`` OAuth keys are queried. Pass
    ``?include_inactive=1`` or ``?all_oauth=1`` to include ``inactive`` keys as well.

    When ``oauth_usage_require_auth`` is true (env ``ANTHROPIC_OAUTH_USAGE_REQUIRE_AUTH``),
    authenticates like the proxy: a configured ``sp-*`` key, nothing else --
    from a header or ``?key=``. On by default. Refreshes tokens when needed via
    the same path as forwarding.

    When ``oauth_usage_cache_seconds`` > 0, the aggregated JSON is cached for that many
    seconds (``ANTHROPIC_OAUTH_USAGE_CACHE_SECONDS``), separately per ``include_inactive``.
    """
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    client: httpx.AsyncClient = request.app["http_client"]
    db: Database = request.app["db"]

    if request.app.get("oauth_usage_require_auth") and not pool.check_auth(
        _extract_client_token(request) or request.query.get("key", "").strip()
    ):
        return web.json_response({"error": "unauthorized"}, status=401)

    include_inactive_oauth = _truthy_query(
        request, "include_inactive", "all_oauth"
    )

    limiter = request.app.get("key_limiter")
    limit_key = _smartproxy_limit_key(request) if limiter is not None else ""

    def _finalize(built: dict) -> dict:
        return _inject_smartproxy_limits(built, limiter, limit_key) if limit_key else built

    ttl = int(request.app.get("oauth_usage_cache_seconds", 0))
    if ttl <= 0:
        keys, last_failure = await _build_oauth_usage_payload(
            pool,
            client,
            db,
            claude_code_version=request.app["claude_code_version"].token,
            include_inactive_oauth=include_inactive_oauth,
        )
        payload = {
            "generated_at": _utc_now_iso(),
            "cached_until": None,
            "cache_ttl_seconds": ttl,
            "served_from_cache": False,
            "include_inactive_oauth": include_inactive_oauth,
            "keys": keys,
        }
        if last_failure is not None:
            payload["last_failure"] = last_failure
        return web.json_response(_finalize(payload))

    lock: asyncio.Lock = request.app["_oauth_usage_cache_lock"]
    async with lock:
        entry: dict | None = request.app.get("_oauth_usage_cache_entry")
        now = time.monotonic()
        if (
            entry is not None
            and now < entry["until"]
            and entry.get("include_inactive_oauth") == include_inactive_oauth
        ):
            payload = dict(entry["payload"])
            payload["served_from_cache"] = True
            body = json.dumps(
                _finalize(payload), separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            return web.Response(body=body, content_type="application/json")

        keys, last_failure = await _build_oauth_usage_payload(
            pool,
            client,
            db,
            claude_code_version=request.app["claude_code_version"].token,
            include_inactive_oauth=include_inactive_oauth,
        )
        generated_at = _utc_now_iso()
        cached_until = (
            (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat().replace("+00:00", "Z")
        )
        payload = {
            "generated_at": generated_at,
            "cached_until": cached_until,
            "cache_ttl_seconds": ttl,
            "served_from_cache": False,
            "include_inactive_oauth": include_inactive_oauth,
            "keys": keys,
        }
        has_errors = any("error" in item for item in keys)
        if last_failure is not None:
            payload["last_failure"] = last_failure
        if not has_errors:
            request.app["_oauth_usage_cache_entry"] = {
                "until": now + float(ttl),
                "include_inactive_oauth": include_inactive_oauth,
                "payload": dict(payload),
            }
        else:
            request.app["_oauth_usage_cache_entry"] = None
            payload["cached_until"] = None

        body = json.dumps(
            _finalize(payload), separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    return web.Response(body=body, content_type="application/json")


def _span_days_between(prev_resets_at: str, resets_at: str) -> float | None:
    try:
        prev_dt = datetime.fromisoformat(prev_resets_at)
        new_dt = datetime.fromisoformat(resets_at)
        return round((new_dt - prev_dt).total_seconds() / 86400.0, 2)
    except (ValueError, TypeError):
        return None


def _hours_between(start_iso: str, end_iso: str) -> float | None:
    try:
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        return round((end_dt - start_dt).total_seconds() / 3600.0, 1)
    except (ValueError, TypeError):
        return None


def _window_usage_block(models: dict[str, dict] | None, prices: dict) -> dict | None:
    """Per-model token counters + cost for one window, shaped like /_usage."""
    if not models:
        return None
    out_models: dict[str, dict] = {}
    totals: dict[str, object] = {c: 0 for c in WINDOW_USAGE_COUNTERS}
    total_cost = 0.0
    cost_partial = False
    for model in sorted(models):
        row = models[model]
        entry: dict[str, object] = {c: row[c] for c in WINDOW_USAGE_COUNTERS}
        cost, partial = estimate_cost_with_cache(
            model,
            row["input_tokens"],
            row["output_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_creation_tokens=row["cache_creation_tokens"],
            cache_creation_5m_tokens=row["cache_creation_5m_tokens"],
            cache_creation_1h_tokens=row["cache_creation_1h_tokens"],
            web_search_requests=row["web_search_requests"],
            prices=prices,
        )
        entry["cost_usd"] = round(cost, 4) if cost is not None else None
        if cost is not None:
            total_cost += cost
        if cost is None or partial:
            cost_partial = True
        out_models[model] = entry
        for c in WINDOW_USAGE_COUNTERS:
            totals[c] += row[c]
    totals["cost_usd"] = round(total_cost, 4)
    totals["cost_partial"] = cost_partial
    return {"models": out_models, "totals": totals}


async def build_oauth_usage_history(
    db: Database, *, kind_filter: str | None, per_kind_limit: int
) -> list[dict]:
    """Build the per-OAuth-key rate-limit window history (see oauth_window_log)."""
    prices = build_price_lookup(await db.get_all_model_prices())

    rows = await db.list_anthropic_keys()
    keys_out: list[dict] = []
    for row in rows:
        if row.get("key_type") != "oauth":
            continue
        key_id = str(row.get("id", ""))
        log_rows = await db.list_oauth_window_log(key_id)
        usage_rows = await db.list_oauth_window_usage(key_id)
        usage_by_window: dict[int, dict[str, dict]] = {}
        for u in usage_rows:
            usage_by_window.setdefault(u["window_id"], {})[u["model"]] = u
        windows: dict[str, list[dict]] = {}
        for log_row in log_rows:  # ordered window_kind ASC, resets_at ASC
            kind = log_row["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            bucket = windows.setdefault(kind, [])
            span = None
            if bucket:
                span = _span_days_between(
                    bucket[-1]["resets_at"], log_row["resets_at"]
                )
            bucket.append({
                "resets_at": log_row["resets_at"],
                "first_seen_at": log_row["first_seen_at"],
                "first_active_at": log_row["first_active_at"],
                "last_seen_at": log_row["last_seen_at"],
                "observations": log_row["observations"],
                "last_utilization": log_row["last_utilization"],
                "max_utilization": log_row["max_utilization"],
                "max_utilization_at": log_row["max_utilization_at"],
                "span_days_since_prev": span,
                "usage": _window_usage_block(
                    usage_by_window.get(log_row["id"]), prices),
            })
        for bucket in windows.values():
            bucket.reverse()  # newest first
            del bucket[per_kind_limit:]
        drop_rows = await db.list_oauth_window_drops(key_id)
        drops: dict[str, list[dict]] = {}
        for drop_row in drop_rows:  # ordered window_kind ASC, dropped_at ASC
            kind = drop_row["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            drops.setdefault(kind, []).append({
                "dropped_at": drop_row["dropped_at"],
                "prev_seen_at": drop_row["prev_seen_at"],
                "from_utilization": drop_row["from_utilization"],
                "to_utilization": drop_row["to_utilization"],
                "resets_at_claimed": drop_row["resets_at"],
                "hours_before_claimed_reset": _hours_between(
                    drop_row["dropped_at"], drop_row["resets_at"]
                ),
            })
        for bucket in drops.values():
            bucket.reverse()  # newest first
            del bucket[per_kind_limit:]
        pending_rows = await db.list_oauth_window_usage_pending(key_id)
        pending: dict[str, dict] = {}
        for p in pending_rows:
            kind = p["window_kind"]
            if kind_filter and kind != kind_filter:
                continue
            block = pending.setdefault(
                kind, {"models": {}, "updated_at": p["updated_at"]})
            block["models"][p["model"]] = {
                c: p[c] for c in WINDOW_USAGE_COUNTERS}
            if p["updated_at"] > block["updated_at"]:
                block["updated_at"] = p["updated_at"]
        # Deliberately not filtered by ?kind=: a wipe spans counters, and the
        # 5h re-open that accompanies it is a window_reset that never appears
        # in `drops` at all, so a filtered view would hide the whole event.
        wipes = await db.list_oauth_limit_wipes(key_id)
        wipes.reverse()  # newest first
        del wipes[per_kind_limit:]
        keys_out.append({
            "id": key_id,
            "name": row.get("name"),
            "status": row.get("status"),
            "windows": windows,
            "drops": drops,
            "wipes": wipes,
            "pending": pending,
        })
    return keys_out


async def _oauth_usage_history_handler(request: web.Request) -> web.Response:
    """GET observed rate-limit window history for OAuth keys.

    One record per observed window instance (see ``oauth_window_log``).
    ``span_days_since_prev`` — distance to the previous window of the same
    kind, i.e. the actual window length. ``?kind=seven_day`` filters by
    window kind; ``?limit=N`` caps records per kind (default 50, newest
    first). Auth follows the same ``oauth_usage_require_auth`` flag as
    ``/_oauth_usage``.
    """
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    db: Database = request.app["db"]

    if request.app.get("oauth_usage_require_auth") and not pool.check_auth(
        _extract_client_token(request)
    ):
        return web.json_response({"error": "unauthorized"}, status=401)

    kind_filter = request.query.get("kind") or None
    try:
        per_kind_limit = max(1, int(request.query.get("limit", "50")))
    except ValueError:
        per_kind_limit = 50

    keys_out = await build_oauth_usage_history(
        db, kind_filter=kind_filter, per_kind_limit=per_kind_limit
    )
    return web.json_response({
        "generated_at": _utc_now_iso(),
        "keys": keys_out,
    })


async def _root_handler(request: web.Request) -> web.Response:
    if request.method in ("HEAD", "GET"):
        return web.Response(text="ok\n", content_type="text/plain")
    return await _proxy_handler(request)


_OAUTH_LOGIN_SESSION_TTL_SEC = 600
_OAUTH_LS_PREFIX = "smart_proxy_oauth_"
_OAUTH_BC_NAME = "smart_proxy_oauth"


def _oauth_broadcast_to_manual_tab_script(state: str, key_id: str) -> str:
    """Tell the manual-login tab that /callback finished (BroadcastChannel + localStorage)."""
    payload_json = json.dumps({"ok": True, "state": state, "key_id": key_id})
    state_js = json.dumps(state)
    prefix_js = json.dumps(_OAUTH_LS_PREFIX)
    bc_js = json.dumps(_OAUTH_BC_NAME)
    return (
        "<script>\n"
        "(function() {\n"
        f"  var payload = {payload_json};\n"
        "  try {\n"
        f"    localStorage.setItem({prefix_js} + {state_js}, JSON.stringify(payload));\n"
        "  } catch (e) {}\n"
        "  try {\n"
        f"    var bc = new BroadcastChannel({bc_js});\n"
        "    bc.postMessage(payload);\n"
        "    bc.close();\n"
        "  } catch (e) {}\n"
        "})();\n"
        "</script>\n"
    )


def _oauth_manual_tab_listener_script(state: str) -> str:
    """Listen for sibling tab completing OAuth (same origin)."""
    state_js = json.dumps(state)
    prefix_js = json.dumps(_OAUTH_LS_PREFIX)
    bc_js = json.dumps(_OAUTH_BC_NAME)
    return (
        "<script>\n"
        "(function() {\n"
        f"  var STATE = {state_js};\n"
        "  function showDone(payload) {\n"
        "    var box = document.getElementById('oauth-manual-status');\n"
        "    if (box) {\n"
        "      box.innerHTML = '<p><strong>OAuth saved.</strong> Key id: <code>' +\n"
        "        (payload.key_id || '') + '</code>. You can close this tab.</p>';\n"
        "    }\n"
        "    var fm = document.getElementById('oauth-manual-form');\n"
        "    if (fm) fm.style.display = 'none';\n"
        "  }\n"
        "  window.addEventListener('storage', function(ev) {\n"
        "    if (ev.key === " + prefix_js + " + STATE && ev.newValue) {\n"
        "      try { showDone(JSON.parse(ev.newValue)); } catch (e) {}\n"
        "    }\n"
        "  });\n"
        "  try {\n"
        f"    var bc = new BroadcastChannel({bc_js});\n"
        "    bc.onmessage = function(ev) {\n"
        "      if (ev.data && ev.data.state === STATE && ev.data.ok) showDone(ev.data);\n"
        "    };\n"
        "  } catch (e) {}\n"
        "})();\n"
        "</script>\n"
    )


def _oauth_redirect_uri(app: web.Application) -> str:
    """redirect_uri for Claude OAuth authorize + token exchange (Anthropic whitelist)."""
    override = (app.get("oauth_login_redirect_uri") or "").strip()
    if override:
        return override.rstrip("/")

    port_override = (app.get("oauth_login_redirect_port") or "").strip()
    if port_override.isdigit():
        port = int(port_override)
    else:
        base = (app.get("oauth_login_base_url") or "").strip()
        parsed = urlparse(base)
        if parsed.port is not None:
            port = parsed.port
        else:
            # Same port as anthropic-proxy when BASE_URL omits :port (typical remote URL).
            port = PROXY_PORT

    return f"http://localhost:{port}/callback"


def _normalize_pasted_auth_code(raw: str) -> str:
    """Return authorization code from raw paste (bare token or callback URL query)."""
    s = raw.strip()
    if not s:
        return ""
    if "code=" not in s:
        return s
    parsed = urlparse(s)
    qs = parsed.query or (s[1:] if s.startswith("?") else "")
    pairs = parse_qs(qs)
    code = (pairs.get("code") or [None])[0]
    return code.strip() if code else s


def _prune_oauth_login_sessions(app: web.Application) -> None:
    sessions: dict[str, dict] = app["_oauth_login_sessions"]
    now = time.time()
    stale = [
        k for k, v in sessions.items()
        if now - float(v.get("ts", 0)) > _OAUTH_LOGIN_SESSION_TTL_SEC
    ]
    for k in stale:
        del sessions[k]


class OAuthExchangeError(Exception):
    """Code-for-token exchange failure; ``status`` is the HTTP status to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _oauth_exchange_and_store(
    app: web.Application, *, code: str, state: str
) -> dict:
    """Pop the PKCE session, exchange the code, insert the key, reload the pool.

    Shared by the legacy HTML flow and the dashboard JSON API.
    Returns ``{"key_id": ..., "name": ...}``; raises OAuthExchangeError.
    """
    if not code or not state:
        raise OAuthExchangeError(400, "missing code or state")

    sessions: dict[str, dict] = app["_oauth_login_sessions"]
    sess = sessions.pop(state, None)
    if not sess:
        raise OAuthExchangeError(
            400,
            "unknown or expired OAuth state — start the login again "
            f"(sessions expire after {_OAUTH_LOGIN_SESSION_TTL_SEC // 60} minutes).",
        )

    client: httpx.AsyncClient = app["http_client"]
    try:
        data = await exchange_authorization_code(
            client,
            token_url=TOKEN_URL,
            code=code,
            verifier=sess["verifier"],
            state=state,
            redirect_uri=sess["redirect_uri"],
            client_id=CLAUDE_OAUTH_CLIENT_ID,
        )
    except RuntimeError as exc:
        logger.warning("OAuth code exchange failed: %s", exc)
        raise OAuthExchangeError(502, str(exc)) from exc

    access = data.get("access_token", "")
    if not access:
        raise OAuthExchangeError(502, json.dumps(data, indent=2)[:2000])

    expires_at = data.get("expires_at")
    if expires_at is None and data.get("expires_in"):
        expires_at = int((time.time() + float(data["expires_in"])) * 1000)

    scope_val = data.get("scope", "")
    scopes = scope_val.split() if isinstance(scope_val, str) else scope_val

    org = data.get("organization", {})

    db: Database = app["db"]
    key_id = str(uuid4())
    await db.insert_anthropic_key(
        id=key_id,
        key_type="oauth",
        access_token=access,
        refresh_token=data.get("refresh_token", ""),
        expires_at=int(expires_at) if expires_at is not None else None,
        scopes=json.dumps(scopes),
        subscription_type=org.get("organization_type", ""),
        rate_limit_tier=org.get("rate_limit_tier", ""),
        name=sess["name"],
    )

    pool: AnthropicKeyPool = app["anthropic_pool"]
    await pool.reload()

    logger.info("OAuth login stored new key %s..", key_id[:12])
    return {"key_id": key_id, "name": sess["name"]}


async def _oauth_run_code_exchange(
    request: web.Request,
    code: str,
    state: str,
) -> web.Response:
    """Legacy HTML flow: shared exchange core rendered as text/HTML responses."""
    try:
        result = await _oauth_exchange_and_store(request.app, code=code, state=state)
    except OAuthExchangeError as exc:
        return web.Response(
            status=exc.status,
            text=exc.message + "\n",
            content_type="text/plain",
            charset="utf-8",
        )

    notify = _oauth_broadcast_to_manual_tab_script(state, result["key_id"])
    html = (
        f"<!DOCTYPE html><html><body><h2>OAuth saved</h2>"
        f"<p>Key id: <code>{html_lib.escape(result['key_id'])}</code></p>"
        f"<p>The proxy has reloaded keys from the database. You can close this tab.</p>"
        f"{notify}"
        f"</body></html>"
    )
    return web.Response(text=html, content_type="text/html", charset="utf-8")


async def _oauth_login_callback(request: web.Request) -> web.Response:
    """Anthropic redirects here with ?code=&state=; exchange and store OAuth key."""
    err = request.query.get("error")
    if err:
        desc = request.query.get("error_description", "")
        body = (
            f"<!DOCTYPE html><html><body><h2>OAuth error</h2>"
            f"<p>{html_lib.escape(err)}: {html_lib.escape(desc)}</p></body></html>"
        )
        return web.Response(
            status=400,
            text=body,
            content_type="text/html",
            charset="utf-8",
        )

    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return web.Response(
            status=400,
            text="missing code or state",
            content_type="text/plain",
            charset="utf-8",
        )

    return await _oauth_run_code_exchange(request, code, state)


async def _reload_handler(request: web.Request) -> web.Response:
    pool: AnthropicKeyPool = request.app["anthropic_pool"]
    token = _extract_reload_token(request)
    peer = request.remote or ""
    # Same-machine CLI / curl without ?key= still works when proxy keys exist.
    localhost_ok = peer in ("127.0.0.1", "::1")
    if pool._proxy_keys and not localhost_ok and not pool.check_auth(token):
        return web.Response(
            status=401,
            body=json.dumps({"error": "unauthorized"}).encode(),
            content_type="application/json",
        )
    await pool.reload()
    await _resync_limiter(request.app)
    cc_version: ClaudeCodeVersion = request.app["claude_code_version"]
    # ?reset_claude_code_version=1 drops a learned version back to the configured
    # floor — the operator's escape hatch when a newer version misbehaves and
    # lowering the floor alone would be overridden by what the proxy learned.
    if request.query.get("reset_claude_code_version"):
        cc_version.reset()
    # ?clear_cooldowns=1 (optionally &model=<id>) retries a rate-limited key now
    # instead of waiting out our clamp of upstream's retry-after.
    cleared = None
    if request.query.get("clear_cooldowns"):
        cleared = pool.clear_cooldowns(model=request.query.get("model") or None)
    payload = {
        "status": "reloaded",
        "active": pool.available,
        "claude_code_version": cc_version.token,
    }
    if cleared is not None:
        payload["cooldowns_cleared"] = cleared
    return web.Response(
        text=json.dumps(payload) + "\n",
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# App factory & standalone entry
# ---------------------------------------------------------------------------

_USAGE_FLUSH_INTERVAL = 60  # seconds
_LOW_BALANCE_RECHECK_INTERVAL = 86400  # 24 hours
_HOURLY_RETENTION_DAYS = 35  # covers any window up to monthly; bounds the table
# Wipes have shown up roughly every six weeks, so 60 days holds about one
# unreferenced event's worth of context; snapshots a wipe points at never expire.
_USAGE_SNAPSHOT_RETENTION_DAYS = 60
# Reconciliation runs hourly and its first pass recovers months of history;
# only wipes newer than this are worth waking anyone for.
_WIPE_ALERT_MAX_AGE_HOURS = 6


def _window_usage_deltas(rows: list[tuple]) -> list[dict]:
    """Aggregate flushed usage rows into per-(key, model) window deltas."""
    agg: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        if row[4] != "anthropic":
            continue
        acc = agg.setdefault((row[3], row[5]), [0] * len(WINDOW_USAGE_COUNTERS))
        for i in range(len(WINDOW_USAGE_COUNTERS)):
            acc[i] += int(row[7 + i])
    return [
        {"key_id": key_id, "model": model,
         **dict(zip(WINDOW_USAGE_COUNTERS, counters))}
        for (key_id, model), counters in agg.items()
    ]


async def _attribute_flushed_usage(
    db: Database, rows: list[tuple], app: object | None
) -> None:
    deltas = _window_usage_deltas(rows)
    if not deltas:
        return
    try:
        await db.attribute_oauth_window_usage(deltas)
    except Exception as exc:
        logger.exception("OAuth window usage attribution failed")
        _alert_failure(app, source="usage attribution to the OAuth window", exc=exc)


async def _flush_usage(
    tracker: UsageTracker, db: Database, app: object | None = None
) -> int:
    """Flush buffered usage; attribute whatever landed to OAuth rate-limit windows.

    A partial flush still raises, so the loop alerts and `_resync_limiter` /
    `_on_shutdown` behave as before -- but the usage_daily rows that *did*
    commit are attributed first. They are in the database; without this they
    would never be counted against any window.
    """
    try:
        rows = await tracker.flush(db)
    except UsageFlushError as exc:
        if exc.rows:
            await _attribute_flushed_usage(db, exc.rows, app)
        raise
    if rows:
        await _attribute_flushed_usage(db, rows, app)
    return len(rows)


async def _resync_limiter(app: web.Application) -> None:
    """Re-read limits and prices, then re-seed spend from the hourly buckets.

    Flushes buffered usage first. Seeding *replaces* the live spend counter with
    the hourly-bucket totals, so whatever the tracker has not flushed yet would
    otherwise be silently forgiven — every reload would hand each key back up to
    a flush interval's worth of budget.

    Note this flush-first ordering isn't airtight: ``UsageTracker.flush`` swaps
    its buffers under its lock but writes them to the DB afterwards, outside the
    lock, so a request landing between the swap and the DB write is neither in
    the buffer nor yet visible to the reseed query. That's within the documented
    accuracy envelope (a restart/reload can lose at most a flush interval).
    """
    limiter: KeyLimiter | None = app.get("key_limiter")
    if limiter is None:
        return
    tracker: UsageTracker | None = app.get("usage_tracker")
    db: Database | None = app.get("db")
    if tracker is not None and db is not None:
        await _flush_usage(tracker, db, app)
    await limiter.load()


async def _usage_flush_loop(app: web.Application) -> None:
    tracker: UsageTracker = app["usage_tracker"]
    db: Database = app["db"]
    last_prune = 0.0
    while True:
        await asyncio.sleep(_USAGE_FLUSH_INTERVAL)
        try:
            n = await _flush_usage(tracker, db, app)
            if n:
                logger.info("Flushed %d usage rows to DB", n)
        except Exception as exc:
            # This is the one that ran every 60s for hours on 2026-08-21 and
            # woke nobody: the flush dies before usage_key_hourly, so the spend
            # limiter's re-seed source goes stale while serving looks healthy.
            logger.warning("Usage flush error: %s", exc)
            _alert_failure(app, source="usage flush to the DB", exc=exc)
        now = time.monotonic()
        if now - last_prune >= 3600:
            last_prune = now
            try:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=_HOURLY_RETENTION_DAYS)
                ).strftime("%Y-%m-%dT%H")
                await db.prune_usage_key_hourly(cutoff)
            except Exception as exc:
                logger.warning("Hourly usage prune error: %s", exc)
                _alert_failure(app, source="usage_key_hourly pruning", exc=exc)
            try:
                # Snapshots referenced by a wipe are kept regardless of age —
                # they are the forensic record the table exists for.
                snapshot_cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(days=_USAGE_SNAPSHOT_RETENTION_DAYS)
                ).isoformat()
                dropped = await db.prune_oauth_usage_snapshots(before=snapshot_cutoff)
                if dropped:
                    logger.info("Pruned %d oauth usage snapshots", dropped)
            except Exception as exc:
                logger.warning("Usage snapshot prune error: %s", exc)
                _alert_failure(app, source="oauth_usage_snapshot pruning", exc=exc)
            try:
                # The detector runs off in-memory state, which is empty for the
                # first couple of minutes after a restart; the drop-log writer
                # compares against the database and has no such gap. Reconciling
                # from it hourly closes that hole without a separate schedule.
                recovered = await db.reconcile_limit_wipes_from_drops()
                if recovered:
                    logger.info(
                        "Reconciled %d limit wipe(s) the detector had missed",
                        len(recovered))
                # A reconciled wipe is precisely the one nobody saw live, so it
                # is the one most worth alerting on. Only recent ones: the first
                # run recovers months of history, and a burst of alerts about
                # events long past is noise, not news.
                fresh_after = (
                    datetime.now(timezone.utc)
                    - timedelta(hours=_WIPE_ALERT_MAX_AGE_HOURS)
                ).isoformat()
                pool = app.get("anthropic_pool")
                if pool is not None:
                    for wipe in recovered:
                        if wipe["observed_at"] >= fresh_after:
                            pool.alert_limit_wipe(wipe)
            except Exception as exc:
                logger.warning("Limit wipe reconciliation error: %s", exc)
                _alert_failure(app, source="oauth_limit_wipe reconciliation", exc=exc)


async def _recheck_low_balance_loop(app: web.Application) -> None:
    """Periodically probe low_balance keys to see if they recovered."""
    db: Database = app["db"]
    pool: AnthropicKeyPool = app["anthropic_pool"]
    client: httpx.AsyncClient = app["http_client"]

    while True:
        await asyncio.sleep(_LOW_BALANCE_RECHECK_INTERVAL)
        try:
            rows = await db.get_low_balance_anthropic_keys()
            if not rows:
                continue

            logger.info("Rechecking %d low_balance Anthropic keys", len(rows))
            for row in rows:
                op_id = str(uuid4())
                key_id = row["id"]
                key_type = row["key_type"]
                api_key = row["api_key"]
                access_token = row["access_token"]
                token = api_key if key_type == "api_key" else access_token
                if not token:
                    continue

                headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
                if key_type == "api_key":
                    headers["x-api-key"] = token
                else:
                    headers["Authorization"] = f"Bearer {token}"

                try:
                    r = await client.post(
                        f"{UPSTREAM_BASE}/v1/messages",
                        headers=headers,
                        json={
                            "model": "claude-haiku-4-5-20251001",
                            "max_tokens": 1,
                            "messages": [{"role": "user", "content": "ping"}],
                        },
                        timeout=15.0,
                    )
                    if r.status_code == 200:
                        await db.set_anthropic_key_status(
                            key_id,
                            "active",
                            audit_op_id=op_id,
                            audit_source="low_balance_recheck",
                            audit_event_type="status_change",
                            audit_decision="reactivate",
                            audit_path="/v1/messages",
                            audit_model=_SMOKE_MODEL,
                            audit_http_status=200,
                            audit_request_id=r.headers.get("request-id", ""),
                            audit_context={"reason": "low_balance_recheck_probe"},
                        )
                        logger.info("Key %s recovered from low_balance → active", key_id[:12])
                    elif r.status_code in (401, 403):
                        await db.set_anthropic_key_status(
                            key_id,
                            "inactive",
                            audit_op_id=op_id,
                            audit_source="low_balance_recheck",
                            audit_event_type="status_change",
                            audit_decision="deactivate",
                            audit_path="/v1/messages",
                            audit_model=_SMOKE_MODEL,
                            audit_http_status=r.status_code,
                            audit_request_id=r.headers.get("request-id", ""),
                            audit_error_type="authentication_error",
                            audit_error_message=f"Low balance recheck returned HTTP {r.status_code}",
                            audit_context={"reason": "low_balance_recheck_probe"},
                        )
                        logger.warning("Key %s low_balance → inactive (%d)", key_id[:12], r.status_code)
                    else:
                        logger.info("Key %s still low_balance (HTTP %d)", key_id[:12], r.status_code)
                except Exception as exc:
                    logger.warning("Recheck failed for key %s: %s", key_id[:12], exc)

            await pool.reload()
        except Exception as exc:
            logger.warning("Low balance recheck error: %s", exc)
            _alert_failure(app, source="low_balance key recheck", exc=exc)


async def _run_oauth_smoke_pass(app: web.Application, window_name: str) -> None:
    db: Database = app["db"]
    pool: AnthropicKeyPool = app["anthropic_pool"]
    client: httpx.AsyncClient = app["http_client"]

    rows = await db.get_active_anthropic_oauth_keys()
    if not rows:
        logger.info("Anthropic OAuth smoke %s skipped: no active OAuth keys", window_name)
        return

    disable_1m_context = bool(app.get("disable_1m_context"))
    should_reload = False
    logger.info("Running Anthropic OAuth smoke %s for %d keys", window_name, len(rows))

    for row in rows:
        # Standby keys are kept warm by _standby_keepwarm_loop (refresh-only, no inference).
        # The smoke pass is primary-only — never send a standby an inference request.
        # 'fallback' is api_key-only by API validation; the guard is defence in depth
        # against a hand-edited row turning a daily smoke into paid inference.
        if str(row.get("role") or "primary") in ("standby", "fallback"):
            continue
        op_id = str(uuid4())
        key = next((k for k in pool._keys if k.key_id == row["id"]), None)
        if key is None:
            key = _anthropic_key_from_row(row)
        was_expired = key.is_expired()
        effective_token = await pool.ensure_valid_token(
            key, client, audit_op_id=op_id, audit_source="scheduled_smoke",
            audit_path="/v1/messages", audit_model=_SMOKE_MODEL,
        )
        if effective_token == pool._REFRESH_BLOCKED:
            logger.info("Smoke %s refresh blocked for key %s", window_name, key.key_id[:12])
            continue
        if effective_token is None:
            await pool.deactivate(key, audit_op_id=op_id,
                audit_source="scheduled_smoke",
                audit_path="/v1/messages", audit_model=_SMOKE_MODEL,
                audit_error_type="oauth_refresh_failed",
                audit_error_message="Scheduled smoke could not obtain a valid OAuth token",
                audit_context={"window_name": window_name})
            should_reload = True
            continue
        if was_expired and all(k.key_id != key.key_id for k in pool._keys):
            should_reload = True

        req = _build_oauth_smoke_request(
            client,
            effective_token,
            # Read live, not snapshotted at startup: the smoke check must claim
            # whatever version the proxy has learned by now.
            claude_code_version=app["claude_code_version"].token,
            disable_1m_context=disable_1m_context,
            claude_like=bool(app.get("claude_like")),
        )
        try:
            response = await client.send(req, stream=True)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning("Smoke %s transport error key %s: %s", window_name, key.key_id[:12], exc)
            continue

        if response.status_code == 200:
            await response.aclose()
            logger.info("Anthropic OAuth smoke %s ok key=%s", window_name, key.key_id[:12])
            continue

        outcome = await _classify_unsuccessful_response(
            response=response,
            key=key,
            pool=pool,
            db=db,
            source="scheduled_smoke",
            op_id=op_id,
            path="/v1/messages",
            model=_SMOKE_MODEL,
            body=req.content if req.content is not None else b"",
            attempt=1,
            max_attempts=1,
        )
        if outcome.status in (401, 402, 403):
            should_reload = True

    if should_reload:
        await pool.reload()


def _standby_keepwarm_sleep_seconds(
    keys: list[_AnthropicKey],
    now_ms: int,
    buffer_ms: int,
    *,
    floor_s: int,
    cap_s: int,
) -> float:
    """Seconds to sleep until the soonest standby is due for a proactive refresh.

    Considers only live OAuth standbys. Returns ``cap_s`` when none are eligible, and
    clamps to ``[floor_s, cap_s]`` — the floor stops a busy-spin when a refresh is due
    but deferred (``expires_at`` hasn't moved, so the due time stays in the past)."""
    due = [
        k.expires_at - buffer_ms
        for k in keys
        if k.role == "standby"
        and k.status != "inactive"
        and k.key_type == "oauth"
        and k.expires_at is not None
    ]
    if not due:
        return cap_s
    remaining_s = (min(due) - now_ms) / 1000.0
    return max(floor_s, min(cap_s, remaining_s))


async def _standby_keepwarm_step(app: web.Application) -> float:
    """One keep-warm iteration: proactively refresh any live OAuth standby whose token
    is within the keep-warm buffer of expiry, then return how long to sleep next.

    Runs independent of primary state and never consults cooldowns — a standby must not
    wake to serve merely because the primary is cooled, but it must stay warm regardless.
    Keys are re-selected from the live pool every call (never cached across the sleep):
    ``pool.reload()`` swaps instances, and refreshing a detached copy would burn a
    single-use refresh token invisibly to the pooled object."""
    pool: AnthropicKeyPool = app["anthropic_pool"]
    client: httpx.AsyncClient = app["http_client"]
    buffer_ms: int = app["standby_keepwarm_buffer_ms"]

    standbys = [k for k in pool._keys if k.role == "standby" and k.status != "inactive"]
    for key in standbys:
        if key.key_type != "oauth" or not key.is_expired(buffer_ms):
            continue
        op_id = str(uuid4())
        token = await pool.ensure_valid_token(
            key, client, audit_op_id=op_id, audit_source="standby_keepwarm",
            audit_path="/v1/messages", audit_model=_SMOKE_MODEL,
            activate=False, refresh_buffer_ms=buffer_ms,
        )
        # A still-valid token that merely failed to refresh returns the old token or
        # ``_REFRESH_BLOCKED`` — both safe to ignore, the standby stays usable. Only a
        # genuinely dead key returns None; deactivate it so it is not a silent failover trap.
        if token is None:
            await pool.deactivate(
                key, audit_op_id=op_id, audit_source="standby_keepwarm",
                audit_path="/v1/messages", audit_model=_SMOKE_MODEL,
                audit_error_type="oauth_refresh_failed",
                audit_error_message="Standby keep-warm could not obtain a valid OAuth token",
            )
        elif key.is_expired():
            logger.warning(
                "Standby keep-warm falling behind: key %s within serving buffer of expiry",
                key.key_id[:12],
            )

    return _standby_keepwarm_sleep_seconds(
        pool._keys, int(time.time() * 1000), buffer_ms,
        floor_s=_STANDBY_KEEPWARM_FLOOR_S, cap_s=_STANDBY_KEEPWARM_CAP_S,
    )


async def _standby_keepwarm_loop(app: web.Application) -> None:
    while True:
        try:
            sleep_s = await _standby_keepwarm_step(app)
        except Exception as exc:
            logger.warning("Standby keep-warm error: %s", exc)
            _alert_failure(app, source="standby key keep-warm", exc=exc)
            sleep_s = _STANDBY_KEEPWARM_FLOOR_S
        await asyncio.sleep(sleep_s)


async def _scheduled_oauth_smoke_loop(app: web.Application) -> None:
    rng = random.Random()
    schedule: _DailyOAuthSmokeSchedule | None = None
    morning_window = app["oauth_smoke_morning_window"]
    midday_window = app["oauth_smoke_midday_window"]

    while True:
        now = datetime.now(_PARIS_TZ)
        previous_day = schedule.current_day if schedule is not None else None
        schedule = _ensure_daily_oauth_smoke_schedule(
            schedule,
            now,
            rng,
            morning_window=morning_window,
            midday_window=midday_window,
        )
        if schedule.current_day != previous_day:
            logger.info(
                "Anthropic OAuth smoke slots for %s Europe/Paris: morning=%s midday=%s",
                schedule.current_day.isoformat(),
                schedule.morning_slot.strftime("%H:%M"),
                schedule.midday_slot.strftime("%H:%M"),
            )

        _mark_expired_smoke_windows(
            schedule,
            now=now,
            morning_window=morning_window,
            midday_window=midday_window,
        )
        next_slot = _next_pending_smoke_slot(schedule)
        if next_slot is None:
            tomorrow = datetime(
                schedule.current_day.year,
                schedule.current_day.month,
                schedule.current_day.day,
                tzinfo=_PARIS_TZ,
            ) + timedelta(days=1)
            await asyncio.sleep(max((tomorrow - now).total_seconds(), 1))
            continue

        window_name, target = next_slot
        delay = (target - now).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
            continue

        await _run_oauth_smoke_pass(app, window_name)
        if window_name == "morning":
            schedule.morning_done = True
        else:
            schedule.midday_done = True


def _notify_startup(app: web.Application) -> None:
    """Announce that the proxy came up, so a silent alert pipe is visible."""
    notifier = app.get("_notifier")
    if notifier is None:
        return
    pool: AnthropicKeyPool | None = app.get("anthropic_pool")
    active = len([k for k in pool._keys if k.status != "inactive"]) if pool else 0
    task = asyncio.create_task(notifier.notify(
        f"🟢 {_SERVICE_NAME} · proxy started, active keys: {active}"
    ))
    app["_alert_tasks"].add(task)
    task.add_done_callback(app["_alert_tasks"].discard)


def _start_background_tasks(app: web.Application) -> None:
    app["_flush_task"] = asyncio.create_task(_usage_flush_loop(app))
    app["_recheck_task"] = asyncio.create_task(_recheck_low_balance_loop(app))
    if app.get("oauth_smoke_enabled", True):
        app["_oauth_smoke_task"] = asyncio.create_task(_scheduled_oauth_smoke_loop(app))
    else:
        logger.info("Anthropic OAuth smoke windows disabled")
    if app.get("standby_keepwarm_enabled", True):
        app["_standby_keepwarm_task"] = asyncio.create_task(_standby_keepwarm_loop(app))
    else:
        logger.info("Standby keep-warm disabled")
    # Every loop here runs forever by design; if one stops, its own except
    # blocks can no longer report anything, so watch the task itself.
    for task_name, label in (
        ("_flush_task", "usage-flush"),
        ("_recheck_task", "low-balance-recheck"),
        ("_oauth_smoke_task", "oauth-smoke"),
        ("_standby_keepwarm_task", "standby-keepwarm"),
    ):
        task = app.get(task_name)
        if task is not None:
            _watch_background_task(app, label, task)


async def _require_migrations_applied(db: Database) -> None:
    """Postgres only: refuse to boot with pending migrations.

    Postgres migrations run only from `smart-proxy db migrate`; connecting
    checks nothing. Serving on an unmigrated schema means every 60s flush
    fails -- and a failing flush used to take usage_key_hourly, the spend
    limiter's re-seed source, down with it. A refused boot is loud and cheap;
    a silent hole is neither. sqlite applies its schema on connect, so this
    is a no-op there.
    """
    if getattr(db, "_backend", "sqlite") != "postgres":
        return
    from smart_proxy.db_migrations import POSTGRES_MIGRATIONS

    await db.ensure_migration_table()
    applied = await db.get_applied_migrations()
    pending = [name for name, _statements in POSTGRES_MIGRATIONS if name not in applied]
    if pending:
        raise RuntimeError(
            "pending PostgreSQL migrations: "
            + ", ".join(pending)
            + " -- run `smart-proxy db migrate` before starting the proxy"
        )


async def _on_startup(app: web.Application) -> None:
    db = build_database_from_config(
        database_url=app.get("database_url", ""),
        db_path=app["db_path"],
    )
    await db.connect()
    await _require_migrations_applied(db)
    app["db"] = db

    pool = AnthropicKeyPool(db)
    await pool.reload()
    app["anthropic_pool"] = pool

    tracker = UsageTracker()
    app["usage_tracker"] = tracker

    limiter = KeyLimiter(db, tz=app.get("limit_window_tz") or DEFAULT_WINDOW_TZ)
    await limiter.load()
    app["key_limiter"] = limiter

    app["http_client"] = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)
    notifier = _build_notifier(app)
    pool._notifier = notifier
    app["_notifier"] = notifier
    app["_alert_throttle"] = AlertThrottle()
    app["_alert_tasks"] = set()
    _ALERT_FALLBACK["_notifier"] = notifier
    _ALERT_FALLBACK["_alert_throttle"] = app["_alert_throttle"]
    _ALERT_FALLBACK["_alert_tasks"] = app["_alert_tasks"]
    _wire_db_state_alerts(app, db)
    app["_oauth_usage_cache_lock"] = asyncio.Lock()
    app["_oauth_usage_cache_entry"] = None
    _start_background_tasks(app)
    if notifier is not None:
        # Proves the whole alert pipe end-to-end on every deploy, and doubles as
        # a restart trail: an unexplained one of these is itself a signal.
        _notify_startup(app)


def _wire_db_state_alerts(app: web.Application, db: Database) -> None:
    """Report the database going away and coming back.

    Both are rare state transitions rather than per-request failures, so the
    ordinary throttle is enough: each carries its own signature and can fire at
    most once per window, which is exactly the protection wanted if the database
    flaps rather than dying cleanly.
    """
    if not hasattr(db, "on_db_state_change"):
        return   # sqlite: no connection to lose

    def _on_state(state: str, exc: BaseException | None,
                  outage_seconds: float | None) -> None:
        if state == "open":
            _alert_failure(
                app, source="database unavailable", exc=exc,
                detail=(
                    "The proxy keeps serving requests from in-memory data. "
                    "Traffic accounting and token refresh are suspended."
                ),
            )
        else:
            # Reconcile before announcing, so the message can say whether any
            # token had to be rescued — that is the number worth reading.
            task = asyncio.create_task(_reconcile_after_recovery(app, outage_seconds))
            app["_alert_tasks"].add(task)
            task.add_done_callback(app["_alert_tasks"].discard)

    db.on_db_state_change = _on_state


async def _reconcile_after_recovery(
    app: web.Application, outage_seconds: float | None
) -> None:
    """Write back tokens the database is behind on, then report the recovery."""
    reconciled = 0
    pool: AnthropicKeyPool | None = app.get("anthropic_pool")
    if pool is not None:
        try:
            reconciled = await pool.reconcile_tokens()
        except Exception as exc:
            logger.exception("token reconciliation after DB recovery failed")
            _alert_failure(app, source="token reconciliation after the DB came back", exc=exc)
    detail = f"The outage lasted {int(outage_seconds or 0)}s."
    if reconciled:
        detail += f" Tokens recovered: {reconciled}."
    _alert_failure(app, source="database is available again", detail=detail)


def _build_notifier(app: web.Application) -> TelegramNotifier | None:
    """Build a Telegram notifier from app config, or None when unconfigured."""
    token = str(app.get("telegram_bot_token") or "").strip()
    chat_id = str(app.get("telegram_chat_id") or "").strip()
    if token and chat_id:
        logger.info("Telegram alerts enabled")
        return TelegramNotifier(token, chat_id, app["http_client"])
    # Loud on the negative path on purpose: an unconfigured notifier makes every
    # alert in the process a silent no-op, which is indistinguishable from
    # "nothing has gone wrong" right up until an outage goes unreported.
    logger.warning(
        "Telegram alerts DISABLED — set ANTHROPIC_TELEGRAM_BOT_TOKEN and "
        "ANTHROPIC_TELEGRAM_CHAT_ID to be told when the proxy fails"
    )
    return None


async def _on_cleanup(app: web.Application) -> None:
    for task_name in ("_flush_task", "_recheck_task", "_oauth_smoke_task", "_standby_keepwarm_task"):
        task: asyncio.Task | None = app.get(task_name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    tracker: UsageTracker | None = app.get("usage_tracker")
    db: Database | None = app.get("db")
    if tracker and db:
        await _flush_usage(tracker, db, app)

    # Drop the process-wide alert handles with the app that owns them, so a
    # second app in the same process (tests) never inherits a closed notifier.
    _ALERT_FALLBACK.clear()

    client: httpx.AsyncClient | None = app.get("http_client")
    if client:
        await client.aclose()
    if db:
        await db.close()


def create_app(
    db_path: str,
    *,
    database_url: str = "",
    disable_1m_context: bool = False,
    claude_like: bool = False,
    claude_code_version: str = DEFAULT_CLAUDE_CODE_VERSION,
    claude_code_version_autolearn: bool = True,
    strip_system_phrase: str = "",
    upgrade_cache_ttl: bool = True,
    precommit_timeout_seconds: float = 10.0,
    oauth_smoke_enabled: bool = True,
    oauth_smoke_morning_window: str = "08:00-09:00",
    oauth_smoke_midday_window: str = "14:00-15:00",
    standby_keepwarm_enabled: bool = True,
    standby_keepwarm_buffer_minutes: int = 120,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
    oauth_usage_require_auth: bool = True,
    dashboard_secret: str = "",
    oauth_usage_cache_seconds: int = 60,
    limit_window_tz: str = DEFAULT_WINDOW_TZ,
    oauth_login_base_url: str = "",
    oauth_login_redirect_uri: str = "",
    oauth_login_redirect_port: str = "",
    openai_compat_enabled: bool = True,
    openai_compat_default_max_tokens: int = 8192,
    openai_compat_auto_cache: bool = True,
    openai_compat_cache_ttl: str = "1h",
) -> web.Application:
    app = web.Application(
        client_max_size=100 * 1024 * 1024,
        # The app's only middleware, so there is no ordering hazard; the
        # dashboard and openai-compat handlers mount into this same app and are
        # covered by it too.
        middlewares=[_failure_alert_middleware],
    )
    app["db_path"] = db_path
    app["database_url"] = database_url
    app["_oauth_login_sessions"] = {}
    app["oauth_login_base_url"] = oauth_login_base_url.strip()
    app["oauth_login_redirect_uri"] = oauth_login_redirect_uri.strip()
    app["oauth_login_redirect_port"] = oauth_login_redirect_port.strip()
    app["disable_1m_context"] = disable_1m_context
    app["claude_like"] = claude_like
    # Raises on a malformed configured version, so the proxy refuses to start
    # rather than rendering garbage into every upstream request.
    app["claude_code_version"] = ClaudeCodeVersion(
        claude_code_version, autolearn=claude_code_version_autolearn
    )
    app["strip_system_phrase"] = strip_system_phrase
    app["upgrade_cache_ttl"] = upgrade_cache_ttl
    app["precommit_timeout"] = precommit_timeout_seconds
    app["dashboard_secret"] = dashboard_secret.strip()
    app["oauth_usage_require_auth"] = oauth_usage_require_auth
    app["oauth_usage_cache_seconds"] = oauth_usage_cache_seconds
    app["limit_window_tz"] = (limit_window_tz or DEFAULT_WINDOW_TZ).strip()
    app["oauth_smoke_enabled"] = oauth_smoke_enabled
    if oauth_smoke_enabled:
        app["oauth_smoke_morning_window"] = _parse_smoke_window(oauth_smoke_morning_window)
        app["oauth_smoke_midday_window"] = _parse_smoke_window(oauth_smoke_midday_window)
    app["standby_keepwarm_enabled"] = standby_keepwarm_enabled
    app["standby_keepwarm_buffer_ms"] = max(1, int(standby_keepwarm_buffer_minutes)) * 60 * 1000
    app["telegram_bot_token"] = telegram_bot_token.strip()
    app["telegram_chat_id"] = telegram_chat_id.strip()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/health", _health)
    app.router.add_get("/_reload", _reload_handler)
    app.router.add_post("/_reload", _reload_handler)
    app.router.add_get("/_oauth_usage", _oauth_usage_handler)
    app.router.add_get("/_oauth_usage_history", _oauth_usage_history_handler)
    register_usage_dashboard(
        app,
        authorize=_usage_dashboard_authorize,
        get_db=lambda req: req.app.get("db"),
    )
    register_dashboard_api(app)
    app.router.add_get("/callback", _oauth_login_callback)
    app.router.add_get("/_oauth/callback", _oauth_login_callback)
    if openai_compat_enabled:
        app.setdefault(
            "openai_compat_loopback_base", f"http://127.0.0.1:{PROXY_PORT}"
        )
        setup_openai_compat(
            app,
            default_max_tokens=openai_compat_default_max_tokens,
            auto_cache=openai_compat_auto_cache,
            cache_ttl=openai_compat_cache_ttl,
            models_passthrough=_proxy_handler,
        )
    app.router.add_route("*", "/", _root_handler)
    app.router.add_route("*", "/{path:.+}", _proxy_handler)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from smart_proxy.config import get_settings
    settings = get_settings()

    problem = validate_dashboard_secret(settings.anthropic_proxy_dashboard_secret)
    if problem:
        logger.error("%s", problem)
        raise SystemExit(2)
    if not settings.anthropic_proxy_dashboard_secret.strip():
        # Same reasoning as the Telegram warning below: a missing operator
        # credential is not visible from the outside. Reads still work with an
        # sp-* key; every mutation answers 403 until this is set.
        logger.warning(
            "Dashboard administration DISABLED — set ANTHROPIC_PROXY_DASHBOARD_SECRET "
            "to attach or remove Anthropic accounts, edit roles and spend limits"
        )

    app = create_app(
        settings.db_path,
        database_url=settings.database_url,
        disable_1m_context=settings.anthropic_proxy_disable_1m_context,
        claude_like=settings.claude_like,
        claude_code_version=settings.anthropic_proxy_claude_code_version,
        claude_code_version_autolearn=settings.anthropic_proxy_claude_code_version_autolearn,
        strip_system_phrase=settings.anthropic_proxy_strip_system_phrase,
        upgrade_cache_ttl=settings.anthropic_proxy_upgrade_cache_ttl,
        precommit_timeout_seconds=settings.anthropic_proxy_precommit_timeout_seconds,
        oauth_smoke_enabled=settings.anthropic_oauth_smoke_enabled,
        oauth_smoke_morning_window=settings.anthropic_oauth_smoke_morning_window,
        oauth_smoke_midday_window=settings.anthropic_oauth_smoke_midday_window,
        standby_keepwarm_enabled=settings.anthropic_standby_keepwarm_enabled,
        standby_keepwarm_buffer_minutes=settings.anthropic_standby_keepwarm_buffer_minutes,
        telegram_bot_token=settings.anthropic_telegram_bot_token,
        telegram_chat_id=settings.anthropic_telegram_chat_id,
        oauth_usage_require_auth=settings.anthropic_oauth_usage_require_auth,
        dashboard_secret=settings.anthropic_proxy_dashboard_secret,
        oauth_usage_cache_seconds=settings.anthropic_oauth_usage_cache_seconds,
        limit_window_tz=settings.anthropic_proxy_limit_window_tz,
        oauth_login_base_url=settings.anthropic_oauth_login_base_url,
        oauth_login_redirect_uri=settings.anthropic_oauth_login_redirect_uri,
        oauth_login_redirect_port=settings.anthropic_oauth_login_redirect_port,
        openai_compat_enabled=settings.anthropic_proxy_openai_compat_enabled,
        openai_compat_default_max_tokens=settings.anthropic_proxy_openai_compat_default_max_tokens,
        openai_compat_auto_cache=settings.anthropic_proxy_openai_compat_auto_cache,
        openai_compat_cache_ttl=settings.anthropic_proxy_openai_compat_cache_ttl,
    )
    logger.info(
        "Anthropic proxy starting on 0.0.0.0:%s → %s",
        PROXY_PORT, UPSTREAM_BASE,
    )
    web.run_app(
        app,
        host="0.0.0.0",
        port=PROXY_PORT,
        print=None,
        # Drain rather than drop: connections stop being accepted at once, but
        # handlers already running get this long to finish.
        shutdown_timeout=settings.anthropic_proxy_shutdown_timeout_seconds,
    )


if __name__ == "__main__":
    main()
