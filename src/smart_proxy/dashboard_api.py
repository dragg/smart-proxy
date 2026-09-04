# src/smart_proxy/dashboard_api.py
"""Dashboard JSON API (``/api/*``) and SPA static serving (``/_app/``).

Registered on the Anthropic proxy app before the catch-all proxy route.

Two credentials, two levels. Reads accept a configured ``sp-*`` proxy key --
carried by header, the ``dash_token`` cookie, or ``?key=`` -- so a consumer can
see what it spent. Mutations accept only ``ANTHROPIC_PROXY_DASHBOARD_SECRET``,
and never from the query string. Both are re-validated on every request, so the
cookie needs no signing.
"""

from __future__ import annotations

import logging
import math
import secrets
import time
import uuid

from collections.abc import Awaitable, Callable
from http.cookies import SimpleCookie
from pathlib import Path

from aiohttp import web

from smart_proxy.anthropic_oauth import (
    CLAUDE_OAUTH_CLIENT_ID,
    build_claude_authorize_url,
    generate_pkce_pair,
)
from smart_proxy.db import Database, parse_allowed_proxy_keys
from smart_proxy.key_limits import LIMIT_KINDS
from smart_proxy.usage import build_price_lookup
from smart_proxy.usage_dashboard import (
    _usage_range,
    build_sessions_json,
    build_usage_bucket_series,
    build_usage_cost_json,
    build_usage_kind_json,
)

logger = logging.getLogger(__name__)

_STATIC_APP_DIR = Path(__file__).resolve().parent / "static" / "app"
_COOKIE_NAME = "dash_token"


def _dashboard_token(request: web.Request, *, allow_query: bool = True) -> str:
    """The credential presented with this request.

    ``allow_query`` exists for one reason: the admin secret must never be read
    from a URL. A query string reaches the access log, the ``Referer`` of every
    asset the page loads, and the browser's history. A caller's ``sp-`` key is
    revocable and may keep that convenience; the admin secret is not.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        header_tok = auth[7:].strip()
    else:
        header_tok = request.headers.get("x-api-key", "").strip()
    token = header_tok or request.cookies.get(_COOKIE_NAME, "").strip()
    if not token and allow_query:
        token = request.query.get("key", "").strip()
    return token


def _dashboard_secret(request: web.Request) -> str:
    return str(request.app.get("dashboard_secret", "") or "").strip()


def _token_is_secret(request: web.Request, token: str) -> bool:
    secret = _dashboard_secret(request)
    if not secret or not token:
        return False
    # Compare bytes: secrets.compare_digest() raises TypeError on non-ASCII
    # str, and the token is whatever a caller typed or pasted.
    return secrets.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))


def _is_admin(request: web.Request) -> bool:
    return _token_is_secret(request, _dashboard_token(request, allow_query=False))


def _dashboard_authorized(request: web.Request) -> bool:
    """Reads: a configured ``sp-`` proxy key, or the admin secret."""
    token = _dashboard_token(request)
    pool = request.app["anthropic_pool"]
    return bool(pool.check_auth(token)) or _token_is_secret(request, token)


def _action_authorized(request: web.Request) -> bool:
    """Mutations: the admin secret only.

    A caller's ``sp-`` key authorises spending, not administration. Without it
    anyone handed a key could attach or delete OAuth accounts and clear other
    keys' spend limits.
    """
    return _is_admin(request)


def _unauthorized() -> web.Response:
    return web.json_response({"error": "unauthorized"}, status=401)


def _admin_required(request: web.Request) -> web.Response:
    """Rejection for a mutating endpoint, told apart by what the caller has.

    No usable credential at all is 401 -- go and authenticate. A valid proxy
    key that simply is not the admin secret is 403: authentication succeeded,
    authorisation did not, and retrying the same credential will not help.
    """
    if not _dashboard_authorized(request):
        return _unauthorized()
    return _forbidden(request)


def _forbidden(request: web.Request) -> web.Response:
    """403 for a request that authenticated but may not administer.

    Says whether the secret is configured at all, because an operator who never
    set the variable has no other way to find out. It never reveals its value,
    and the caller already proved it is not what they sent.
    """
    configured = bool(_dashboard_secret(request))
    return web.json_response(
        {
            "error": "admin required" if configured else "admin secret not configured",
            "admin_secret_configured": configured,
            "hint": (
                "sign in with the dashboard admin secret"
                if configured
                else "set ANTHROPIC_PROXY_DASHBOARD_SECRET and restart, then sign in with it"
            ),
        },
        status=403,
    )


async def _api_session(request: web.Request) -> web.Response:
    pool = request.app["anthropic_pool"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = str(body.get("token", "")).strip()
    admin = _token_is_secret(request, token)
    if not token or not (admin or pool.check_auth(token)):
        return web.json_response(
            {
                "error": "unauthorized",
                "admin_secret_configured": bool(_dashboard_secret(request)),
            },
            status=401,
        )
    resp = web.json_response({"ok": True, "admin": admin})
    # Build the Set-Cookie value with http.cookies.SimpleCookie and add it to
    # resp.headers directly (rather than via resp.set_cookie()). aiohttp only
    # copies resp.cookies into resp.headers at send time (Response._start),
    # so resp.set_cookie() alone leaves resp.headers untouched for callers
    # (and tests) inspecting the Response object before it's actually sent.
    cookie: SimpleCookie = SimpleCookie()
    cookie[_COOKIE_NAME] = token
    morsel = cookie[_COOKIE_NAME]
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    # An admin session carries the secret itself, so it expires in hours; a
    # read-only proxy key is revocable and may stay for a month.
    morsel["max-age"] = str(12 * 3600 if admin else 30 * 24 * 3600)
    morsel["path"] = "/"
    resp.headers.add("Set-Cookie", morsel.output(header="").strip())
    return resp


async def _api_usage(request: web.Request) -> web.Response:
    """Per-key/model cost for a range.

    ``YYYY-MM-DD`` bounds read usage_daily exactly as before; ``YYYY-MM-DDTHH``
    bounds read usage_bucket and add a per-hour ``series`` plus ``covered_from``.
    The grammar alone picks the source -- never how much data happens to exist.
    """
    if not _dashboard_authorized(request):
        return _unauthorized()
    rng = _usage_range(request)
    if isinstance(rng, web.Response):
        return rng
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "usage database unavailable"}, status=500)
    prices = build_price_lookup(await db.get_all_model_prices())

    if rng.granularity == "day":
        rows = await db.query_usage_by_key_model(rng.start, rng.end)
        return web.json_response(build_usage_cost_json(rng.start, rng.end, rows, prices))

    rows = await db.query_usage_bucket_by_key_model(rng.start, rng.end)
    series_rows = await db.query_usage_bucket_series(rng.start, rng.end)
    covered_from = await db.min_usage_bucket_hour()
    return web.json_response(
        build_usage_cost_json(
            rng.start,
            rng.end,
            rows,
            prices,
            granularity="hour",
            covered_from=covered_from,
            series=build_usage_bucket_series(series_rows, prices, rng.start, rng.end),
        )
    )


async def _api_usage_kinds(request: web.Request) -> web.Response:
    """Per-request-kind cost for a range; same grammar as /api/usage.

    Going through _usage_range also means a malformed range now 400s instead
    of reaching SQL as a bind parameter and quietly matching nothing.
    """
    if not _dashboard_authorized(request):
        return _unauthorized()
    rng = _usage_range(request)
    if isinstance(rng, web.Response):
        return rng
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    if rng.granularity == "day":
        rows = await db.query_usage_by_kind(rng.start, rng.end)
    else:
        rows = await db.query_usage_bucket_by_kind(rng.start, rng.end)
    prices = build_price_lookup(await db.get_all_model_prices())
    return web.json_response(
        {
            "granularity": rng.granularity,
            "start": rng.start,
            "end": rng.end,
            "kinds": build_usage_kind_json(rows, prices),
        }
    )


async def _api_sessions(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    try:
        limit = max(1, min(500, int(request.query.get("limit", "50"))))
    except ValueError:
        limit = 50
    rows = await db.query_top_sessions(limit)
    prices = build_price_lookup(await db.get_all_model_prices())
    return web.json_response({"sessions": build_sessions_json(rows, prices)})


async def _api_keys(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    limiter = request.app.get("key_limiter")
    rows = await db.list_proxy_keys()
    keys = []
    for r in rows:
        full_key = str(r["key"])
        keys.append({
            "key_prefix": full_key[:12],
            "name": r.get("name") or "",
            "active": bool(r["active"]),
            "created_at": r.get("created_at"),
            "limits": limiter.limits_for(full_key) if limiter else {},
            "usage": limiter.snapshot(full_key) if limiter else {},
        })
    return web.json_response({"keys": keys})


async def _api_anthropic_keys(request: web.Request) -> web.Response:
    """List Anthropic pool keys (no token material; soft-deleted rows hidden)."""
    if not _dashboard_authorized(request):
        return _unauthorized()
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    rows = await db.list_anthropic_keys()
    # Full sp- keys never leave the server: the scope is reported as the same
    # created_at handles the Keys tab uses, plus a display name. Resolving it
    # costs a second query, so only pay it when some key actually has a scope
    # (this endpoint is polled by the dashboard).
    scopes = {r["id"]: parse_allowed_proxy_keys(r.get("allowed_proxy_keys")) for r in rows}
    by_key: dict[str, dict] = {}
    if any(scopes.values()):
        by_key = {
            str(p["key"]): {
                "created_at": p.get("created_at"),
                "name": p.get("name") or "",
                "key_prefix": str(p["key"])[:12],
            }
            for p in await db.list_proxy_keys()
        }
    keys = [
        {
            "id": r["id"],
            "key_type": r["key_type"],
            "status": r["status"],
            "name": r.get("name") or "",
            "role": r.get("role") or "primary",
            "scope": [by_key[k] for k in sorted(scopes[r["id"]]) if k in by_key],
            "subscription_type": r.get("subscription_type") or "",
            "rate_limit_tier": r.get("rate_limit_tier") or "",
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
            "expires_at": r.get("expires_at"),
            "has_refresh_token": bool(r.get("refresh_token")),
        }
        for r in rows
        if r.get("status") != "deleted"
    ]
    # Parked keys look "active" in the table while every request against them is
    # turned away, so the cooldown state ships alongside the rows that show it.
    pool = request.app.get("anthropic_pool")
    cooldowns = pool.cooldown_snapshot() if pool is not None else []
    return web.json_response({"keys": keys, "cooldowns": cooldowns})


async def _api_anthropic_oauth_start(request: web.Request) -> web.Response:
    """Begin a PKCE login for a new Anthropic OAuth key (dashboard flow)."""
    if not _action_authorized(request):
        return _admin_required(request)
    # Lazy import: anthropic_proxy imports this module at startup.
    from smart_proxy.anthropic_proxy import (
        _oauth_redirect_uri,
        _prune_oauth_login_sessions,
    )

    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()[:200] or "oauth-login"

    _prune_oauth_login_sessions(request.app)
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    redirect_uri = _oauth_redirect_uri(request.app)
    request.app["_oauth_login_sessions"][state] = {
        "verifier": verifier,
        "name": name,
        "redirect_uri": redirect_uri,
        "ts": time.time(),
    }
    authorize_url = build_claude_authorize_url(
        challenge=challenge, state=state, redirect_uri=redirect_uri
    )
    return web.json_response({
        "state": state,
        "authorize_url": authorize_url,
        "redirect_uri": redirect_uri,
    })


async def _api_anthropic_oauth_submit(request: web.Request) -> web.Response:
    """Finish the PKCE login with a pasted callback URL or bare code."""
    if not _action_authorized(request):
        return _admin_required(request)
    from smart_proxy.anthropic_proxy import (
        OAuthExchangeError,
        _normalize_pasted_auth_code,
        _oauth_exchange_and_store,
    )

    try:
        body = await request.json()
    except Exception:
        body = {}
    state = str(body.get("state", "")).strip()
    code = _normalize_pasted_auth_code(str(body.get("code", "")))
    try:
        result = await _oauth_exchange_and_store(request.app, code=code, state=state)
    except OAuthExchangeError as exc:
        return web.json_response({"error": exc.message}, status=exc.status)
    return web.json_response({"ok": True, "key_id": result["key_id"]})


async def _anthropic_key_from_body(
    request: web.Request,
) -> tuple[dict | None, dict, web.Response | None]:
    """Common body parsing for key-management endpoints: (row, body, error)."""
    db: Database | None = request.app.get("db")
    if db is None:
        return None, {}, web.json_response(
            {"error": "database unavailable"}, status=500
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    key_id = str(body.get("id", "")).strip()
    if not key_id:
        return None, body, web.json_response({"error": "id required"}, status=400)
    row = await db.get_anthropic_key(key_id)
    if row is None:
        return None, body, web.json_response({"error": "key not found"}, status=404)
    if row.get("status") == "deleted":
        return None, body, web.json_response({"error": "key not found"}, status=404)
    return row, body, None


async def _api_anthropic_key_status(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _admin_required(request)
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    status = "active" if bool(body.get("active")) else "inactive"
    await request.app["db"].set_anthropic_key_status(
        row["id"], status,
        audit_source="dashboard",
        audit_event_type="dashboard_status_change",
        audit_decision=status,
        audit_error_type="manual_action",
        audit_error_message=f"Set {status} via dashboard",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "status": status})


async def _api_anthropic_key_rename(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _admin_required(request)
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    ok = await request.app["db"].set_anthropic_key_name(
        row["id"], name,
        audit_source="dashboard",
        audit_event_type="dashboard_rename",
        audit_decision="rename",
        audit_error_type="manual_action",
        audit_error_message=f"Renamed to {name!r} via dashboard",
    )
    if not ok:
        return web.json_response({"error": "key not found"}, status=404)
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "name": name})


async def _api_anthropic_key_role(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _admin_required(request)
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    role = str(body.get("role", "")).strip()
    if role not in ("primary", "standby", "fallback"):
        return web.json_response(
            {"error": "role must be 'primary', 'standby' or 'fallback'"}, status=400)
    key_type = str(row.get("key_type") or "")
    # 'standby' auto-promotes to primary the first time it serves, which for a paid
    # api_key would silently hand it all traffic — Claude Code included. 'fallback'
    # in turn is api_key-only: an oauth key parked there would still be hit by the
    # smoke pass and the usage poller, which only know how to skip standbys.
    if role == "standby" and key_type != "oauth":
        return web.json_response(
            {"error": "only oauth keys can be standby; use 'fallback' for api keys"},
            status=400)
    if role == "fallback" and key_type != "api_key":
        return web.json_response(
            {"error": "only api_key keys can be fallback"}, status=400)
    if role in ("standby", "fallback"):
        actives = await request.app["db"].get_active_anthropic_keys()
        other_primary = any(
            r.get("id") != row["id"] and (r.get("role") or "primary") == "primary"
            and r.get("status") != "inactive"
            for r in actives
        )
        if not other_primary:
            return web.json_response(
                {"error": "cannot demote the last active primary key"}, status=400)
    await request.app["db"].set_anthropic_key_role(
        row["id"], role,
        audit_source="dashboard", audit_event_type="role_change",
        audit_decision=f"set_{role}",
        audit_error_type="manual_action", audit_error_message=f"Set role {role} via dashboard",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "role": role})


async def _resolve_scope(
    request: web.Request, raw: object
) -> tuple[list[str], web.Response | None]:
    """Turn a list of proxy-key ``created_at`` handles into full ``sp-`` keys.

    Proxy keys are addressed by ``created_at``, the same handle the Keys tab uses
    for its own mutations: the dashboard is never shown a full ``sp-`` key after
    creation, and the displayed 12-char prefix is not unique across keys. The
    full keys are resolved here and never leave the server.
    """
    if not isinstance(raw, list):
        return [], web.json_response({"error": "proxy_keys must be a list"}, status=400)
    db: Database = request.app["db"]
    limiter = request.app.get("key_limiter")
    resolved: list[str] = []
    for item in raw:
        created_at = str(item or "").strip()
        if not created_at:
            return [], web.json_response({"error": "empty proxy key reference"}, status=400)
        full_key = await db.get_proxy_key_by_created_at(created_at)
        if not full_key:
            return [], web.json_response(
                {"error": f"unknown proxy key {created_at}"}, status=404)
        # A consumer with no spend limit could burn the paid credential without
        # bound; the request path re-checks this, so removing a limit later
        # revokes access rather than silently leaving an unbounded consumer.
        if limiter is None or not limiter.limits_for(full_key):
            return [], web.json_response(
                {"error": f"proxy key {full_key[:12]} has no spend limit — "
                          "set one before granting paid fallback"},
                status=400)
        resolved.append(full_key)
    return resolved, None


async def _api_anthropic_apikey_create(request: web.Request) -> web.Response:
    """Add an Anthropic API key with its role and scope in one shot.

    Role and scope are part of the INSERT rather than follow-up edits: a paid
    key created as the default 'primary' would be live in the general pool —
    serving Claude Code included — for as long as it took to go set the role.
    """
    if not _action_authorized(request):
        return _admin_required(request)
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    try:
        body = await request.json()
    except Exception:
        body = {}
    api_key = str(body.get("api_key", "")).strip()
    if not api_key.startswith("sk-ant-"):
        return web.json_response({"error": "api_key must start with sk-ant-"}, status=400)
    role = str(body.get("role", "fallback")).strip() or "fallback"
    if role not in ("fallback", "primary"):
        return web.json_response(
            {"error": "role must be 'fallback' or 'primary'"}, status=400)
    scope, err = await _resolve_scope(request, body.get("proxy_keys") or [])
    if err is not None:
        return err
    if role == "primary" and scope:
        return web.json_response(
            {"error": "a primary key serves everyone; scope applies to 'fallback' only"},
            status=400)
    name = str(body.get("name", "")).strip() or f"apikey-{api_key[:12]}"
    key_id = str(uuid.uuid4())
    await db.insert_anthropic_key(
        id=key_id, key_type="api_key", api_key=api_key, name=name,
        role=role, allowed_proxy_keys=scope,
    )
    await request.app["anthropic_pool"].reload()
    logger.info("Anthropic api_key added via dashboard: %s role=%s scope=%d",
                key_id, role, len(scope))
    return web.json_response({"ok": True, "id": key_id, "role": role, "scope": len(scope)})


async def _api_anthropic_key_scope(request: web.Request) -> web.Response:
    """Replace the set of proxy keys allowed to escalate onto a fallback key."""
    if not _action_authorized(request):
        return _admin_required(request)
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    db: Database = request.app["db"]
    resolved, err = await _resolve_scope(request, body.get("proxy_keys"))
    if err is not None:
        return err
    await db.set_anthropic_key_scope(
        row["id"], resolved,
        audit_source="dashboard", audit_event_type="scope_change",
        audit_decision="set_scope", audit_error_type="manual_action",
        audit_error_message=f"Set fallback scope ({len(resolved)} proxy keys) via dashboard",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "count": len(resolved)})


async def _api_anthropic_key_delete(request: web.Request) -> web.Response:
    """Soft delete: history keeps referencing the key_id; row recoverable via SQL."""
    if not _action_authorized(request):
        return _admin_required(request)
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    await request.app["db"].set_anthropic_key_status(
        row["id"], "deleted",
        audit_source="dashboard",
        audit_event_type="dashboard_delete",
        audit_decision="soft_delete",
        audit_error_type="manual_action",
        audit_error_message="Soft-deleted via dashboard",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True})


async def _api_anthropic_key_refresh(request: web.Request) -> web.Response:
    """Force an OAuth token refresh (+ activation), mirroring the pool's order:
    refresh -> activate -> persist. Nothing is saved when either step fails."""
    if not _action_authorized(request):
        return _admin_required(request)
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    if row.get("key_type") != "oauth" or not row.get("refresh_token"):
        return web.json_response(
            {"error": "not an OAuth key with a refresh token"}, status=400
        )

    # Lazy imports: anthropic_proxy imports this module; anthropic_oauth is
    # imported lazily too so tests can patch smart_proxy.anthropic_oauth.<fn>.
    import smart_proxy.anthropic_oauth as anthropic_oauth
    from smart_proxy.anthropic_proxy import TOKEN_URL, UPSTREAM_BASE

    client = request.app["http_client"]
    try:
        new_token, new_expires, rotated_refresh = await anthropic_oauth.refresh_oauth_token(
            client,
            token_url=TOKEN_URL,
            refresh_token=row["refresh_token"],
            client_id=row.get("client_id") or CLAUDE_OAUTH_CLIENT_ID,
            scope=anthropic_oauth.normalize_scope(row.get("scopes")),
        )
    except Exception as exc:  # RuntimeError or httpx.HTTPStatusError (429)
        return web.json_response({"error": f"refresh failed: {exc}"}, status=502)

    # Persist the rotated token immediately — activation is best-effort warmup and
    # must never cause a rotated (single-use) refresh token to be discarded.
    await request.app["db"].update_anthropic_oauth_tokens(
        row["id"], new_token, new_expires, rotated_refresh,
        audit_source="dashboard",
        audit_event_type="refresh_succeeded",
        audit_decision="update_tokens",
    )
    await request.app["anthropic_pool"].reload()

    # Standby keys must incur zero activation footprint, even on a manual
    # dashboard refresh — only warm up the upstream session for primaries.
    if (row.get("role") or "primary") != "standby":
        try:
            await anthropic_oauth.activate_oauth_access_token(
                client, access_token=new_token, base_url=UPSTREAM_BASE
            )
        except Exception as exc:  # warmup only; token is already saved and valid
            logger.warning(
                "OAuth activation after manual refresh failed for %s: %s", row["id"][:12], exc
            )

    return web.json_response({"ok": True, "expires_at": new_expires})


def _truthy(request: web.Request, name: str) -> bool:
    return request.query.get(name, "").strip().lower() in ("1", "true", "yes")


async def _api_oauth_usage(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    from smart_proxy.anthropic_proxy import _build_oauth_usage_payload

    pool = request.app["anthropic_pool"]
    client = request.app["http_client"]
    db = request.app["db"]
    keys, last_failure = await _build_oauth_usage_payload(
        pool, client, db, include_inactive_oauth=_truthy(request, "include_inactive")
    )
    payload: dict = {"keys": keys}
    if last_failure is not None:
        payload["last_failure"] = last_failure
    return web.json_response(payload)


async def _api_oauth_history(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    from smart_proxy.anthropic_proxy import build_oauth_usage_history

    db = request.app["db"]
    kind_filter = request.query.get("kind") or None
    try:
        per_kind_limit = max(1, int(request.query.get("limit", "50")))
    except ValueError:
        per_kind_limit = 50
    keys_out = await build_oauth_usage_history(
        db, kind_filter=kind_filter, per_kind_limit=per_kind_limit
    )
    return web.json_response({"keys": keys_out})


async def _api_compat_stats(request: web.Request) -> web.Response:
    if not _dashboard_authorized(request):
        return _unauthorized()
    stats = request.app.get("openai_compat_stats")
    if stats is None:
        return web.json_response({"error": "openai-compat disabled"}, status=404)
    return web.json_response(stats.snapshot())


async def _api_reload(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _admin_required(request)
    pool = request.app["anthropic_pool"]
    await pool.reload()
    # Lazy import: anthropic_proxy imports this module at startup.
    from smart_proxy.anthropic_proxy import _resync_limiter

    await _resync_limiter(request.app)
    return web.json_response({"status": "reloaded", "active": pool.available})


async def _api_anthropic_cooldowns_clear(request: web.Request) -> web.Response:
    """Drop every rate-limit cooldown so the next request retries upstream now.

    Deliberately all-or-nothing rather than per-model: the operator's question is
    "has the limit lifted yet", and a key-level cooldown blocks every model
    anyway, so clearing one model alone would usually be a no-op.
    """
    if not _action_authorized(request):
        return _admin_required(request)
    pool = request.app.get("anthropic_pool")
    if pool is None:
        return web.json_response({"error": "pool unavailable"}, status=500)
    cleared = pool.clear_cooldowns()
    return web.json_response({"status": "cleared", "cooldowns_cleared": cleared})


async def _api_key_active(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _admin_required(request)
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    try:
        body = await request.json()
    except Exception:
        body = {}
    # Identify the key by its exact created_at, not the display prefix: two
    # keys can share leading characters (prefix matching would be ambiguous).
    created_at = str(body.get("created_at", "")).strip()
    if not created_at:
        return web.json_response({"error": "created_at required"}, status=400)
    active = bool(body.get("active"))
    full = await db.set_proxy_key_active_by_created_at(created_at, active)
    if full is None:
        return web.json_response({"error": "key not found or ambiguous"}, status=404)
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "active": active})


async def _api_key_limits(request: web.Request) -> web.Response:
    """Set or clear spend limits on a proxy key.

    Partial update: only the kinds present in ``limits`` are touched. A numeric
    value upserts that kind, an explicit ``null`` clears it (unlimited). The
    whole payload is validated before anything is written.
    """
    if not _action_authorized(request):
        return _admin_required(request)
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    limiter = request.app.get("key_limiter")
    if limiter is None:
        return web.json_response({"error": "limiter unavailable"}, status=500)
    try:
        body = await request.json()
    except Exception:
        body = {}

    created_at = str(body.get("created_at", "")).strip()
    if not created_at:
        return web.json_response({"error": "created_at required"}, status=400)
    raw_limits = body.get("limits")
    if not isinstance(raw_limits, dict) or not raw_limits:
        return web.json_response({"error": "limits object required"}, status=400)

    parsed: dict[str, float | None] = {}
    for kind, raw in raw_limits.items():
        if kind not in LIMIT_KINDS:
            return web.json_response(
                {"error": f"unknown limit kind: {kind}"}, status=400)
        if raw is None:
            parsed[kind] = None
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": f"{kind} must be a number or null"}, status=400)
        if not math.isfinite(amount):
            return web.json_response(
                {"error": f"{kind} must be a finite number or null"}, status=400)
        if amount <= 0:
            return web.json_response(
                {"error": f"{kind} must be > 0; send null for unlimited"}, status=400)
        parsed[kind] = amount

    # Identify the key by its exact created_at, not the display prefix: two
    # keys can share leading characters (prefix matching would be ambiguous).
    full_key = await db.get_proxy_key_by_created_at(created_at)
    if full_key is None:
        return web.json_response({"error": "key not found or ambiguous"}, status=404)

    for kind, amount in parsed.items():
        await limiter.set_limit(full_key, kind, amount)
    return web.json_response({"ok": True, "limits": limiter.limits_for(full_key)})


async def _api_key_create(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _admin_required(request)
    db: Database | None = request.app.get("db")
    if db is None:
        return web.json_response({"error": "database unavailable"}, status=500)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    key = f"sp-{secrets.token_hex(16)}"
    await db.add_proxy_key(key, name)
    # Reload so the new key is immediately live (accepted by the strict action
    # gate and by the proxy) without waiting for the next reload cycle.
    await request.app["anthropic_pool"].reload()
    return web.json_response({"key": key, "name": name})


def _make_spa_handler(static_dir: Path) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    async def _spa(request: web.Request) -> web.StreamResponse:
        index = static_dir / "index.html"
        tail = request.match_info.get("tail", "")
        if tail:
            candidate = (static_dir / tail).resolve()
            if candidate.is_file() and static_dir.resolve() in candidate.parents:
                return web.FileResponse(candidate)
        if not index.is_file():
            return web.Response(status=503, text="dashboard not built")
        return web.FileResponse(index)

    return _spa


def register_dashboard_api(
    app: web.Application, *, static_dir: Path | None = None
) -> None:
    """Register /api/* routes and /_app/ SPA static serving."""
    app.router.add_post("/api/session", _api_session)
    app.router.add_get("/api/usage", _api_usage)
    app.router.add_get("/api/usage/kinds", _api_usage_kinds)
    app.router.add_get("/api/sessions", _api_sessions)
    app.router.add_get("/api/keys", _api_keys)
    app.router.add_post("/api/keys", _api_key_create)
    app.router.add_get("/api/anthropic/keys", _api_anthropic_keys)
    app.router.add_post("/api/anthropic/oauth/start", _api_anthropic_oauth_start)
    app.router.add_post("/api/anthropic/oauth/submit", _api_anthropic_oauth_submit)
    app.router.add_post("/api/anthropic/keys/status", _api_anthropic_key_status)
    app.router.add_post("/api/anthropic/keys/role", _api_anthropic_key_role)
    app.router.add_post("/api/anthropic/keys/scope", _api_anthropic_key_scope)
    app.router.add_post("/api/anthropic/keys/apikey", _api_anthropic_apikey_create)
    app.router.add_post("/api/anthropic/keys/rename", _api_anthropic_key_rename)
    app.router.add_post("/api/anthropic/keys/delete", _api_anthropic_key_delete)
    app.router.add_post("/api/anthropic/keys/refresh", _api_anthropic_key_refresh)
    app.router.add_get("/api/oauth/usage", _api_oauth_usage)
    app.router.add_get("/api/oauth/usage/history", _api_oauth_history)
    app.router.add_get("/api/openai-compat/stats", _api_compat_stats)
    app.router.add_post("/api/reload", _api_reload)
    app.router.add_post("/api/anthropic/cooldowns/clear", _api_anthropic_cooldowns_clear)
    app.router.add_post("/api/keys/active", _api_key_active)
    app.router.add_post("/api/keys/limits", _api_key_limits)

    spa_dir = static_dir or _STATIC_APP_DIR
    spa = _make_spa_handler(spa_dir)
    app.router.add_get("/_app/", spa)
    app.router.add_get("/_app/{tail:.*}", spa)
