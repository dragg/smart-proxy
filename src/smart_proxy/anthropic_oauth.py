from __future__ import annotations

import base64
import gzip
import hashlib
import json
import secrets
import time
import zlib
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from smart_proxy.claude_code_identity import (
    DEFAULT_CLAUDE_CODE_VERSION,
    render_cli_user_agent,
    render_code_user_agent,
)

# Claude Code / Claude.ai OAuth (PKCE) — same public client_id as CLI flows
CLAUDE_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_SCOPE = (
    "org:create_api_key user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)

DEFAULT_REFRESH_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, compress, deflate, br",
    "Content-Type": "application/json",
    "User-Agent": "axios/1.13.6",
    "Connection": "keep-alive",
}

DEFAULT_ACTIVATION_BASE_URL = "https://api.anthropic.com"

# Placeholders in _ACTIVATION_STEPS below, swapped for the caller's Claude Code
# version in build_activation_requests. The steps are a module-level literal, so
# the version cannot be baked in here.
_CLI_UA = "\x00cli-ua\x00"
_CODE_UA = "\x00code-ua\x00"

_ACTIVATION_STEPS: list[tuple[str, str, bool, dict[str, str]]] = [
    (
        "GET",
        "/api/claude_code_penguin_mode",
        True,
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, compress, deflate, br",
            "User-Agent": "axios/1.13.6",
            "anthropic-beta": "oauth-2025-04-20",
            "Connection": "keep-alive",
        },
    ),
    (
        "GET",
        "/api/claude_code_grove",
        True,
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, compress, deflate, br",
            "User-Agent": _CLI_UA,
            "anthropic-beta": "oauth-2025-04-20",
            "Connection": "keep-alive",
        },
    ),
    (
        "GET",
        "/api/oauth/account/settings",
        True,
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, compress, deflate, br",
            "User-Agent": _CODE_UA,
            "anthropic-beta": "oauth-2025-04-20",
            "Connection": "keep-alive",
        },
    ),
    (
        "GET",
        "/mcp-registry/v0/servers?version=latest&visibility=commercial",
        False,
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, compress, deflate, br",
            "User-Agent": "axios/1.13.6",
            "Connection": "keep-alive",
        },
    ),
    (
        "GET",
        "/api/claude_cli/bootstrap",
        True,
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, compress, deflate, br",
            "Content-Type": "application/json",
            "User-Agent": _CODE_UA,
            "anthropic-beta": "oauth-2025-04-20",
            "Connection": "keep-alive",
        },
    ),
    (
        "GET",
        "/v1/mcp_servers?limit=1000",
        True,
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, compress, deflate, br",
            "Content-Type": "application/json",
            "User-Agent": "axios/1.13.6",
            "anthropic-beta": "mcp-servers-2025-12-04",
            "anthropic-version": "2023-06-01",
            "Connection": "keep-alive",
        },
    ),
]


class OAuthRefreshError(RuntimeError):
    """Raised when the ``/token`` refresh endpoint returns a non-2xx (non-429)
    response, or a 2xx with a malformed body. Subclasses ``RuntimeError`` so
    existing ``except RuntimeError`` callers keep working. ``status_code`` is the
    HTTP status (200 for a 2xx-with-bad-body); ``error_code`` is the parsed OAuth
    ``error`` field when present. ``token_consumed`` marks the failures where the
    server answered 2xx and had therefore already rotated the refresh token we
    sent: that token is spent, so retrying with it can only earn a 400."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_code: str | None = None,
        token_consumed: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.token_consumed = token_consumed


# Decoders for a Content-Encoding httpx itself could not undo. httpx drops an
# encoding it has no decoder for *silently* and hands back the raw compressed
# bytes, so `.json()` explodes on binary. `httpx[brotli]` keeps that from
# happening for `br`, which is what the refresh headers advertise; this table is
# the second line of defence, because an unreadable /token body costs us the
# single-use rotated refresh token and there is no way to ask for it again.
_MANUAL_DECODERS: dict[str, Callable[[bytes], bytes]] = {
    "gzip": gzip.decompress,
    "deflate": zlib.decompress,
}

try:
    import brotli as _brotli
except ImportError:  # pragma: no cover - brotlicffi is the PyPy/CFFI binding
    try:
        import brotlicffi as _brotli
    except ImportError:
        _brotli = None  # type: ignore[assignment]
if _brotli is not None:
    _MANUAL_DECODERS["br"] = _brotli.decompress

try:
    import zstandard as _zstandard
except ImportError:  # pragma: no cover - optional, not advertised by our headers
    _zstandard = None  # type: ignore[assignment]
if _zstandard is not None:
    _MANUAL_DECODERS["zstd"] = lambda body: _zstandard.ZstdDecompressor().decompress(body)


def _content_encodings(response: httpx.Response) -> list[str]:
    raw = (response.headers or {}).get("content-encoding", "")
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _undecodable_detail(response: httpx.Response) -> str:
    """Describe a body we failed to parse, naming the encoding. Without it a
    decode failure reads like a network blip in the alert and the key is gone
    before anyone looks at the bytes."""
    encodings = ", ".join(_content_encodings(response)) or "none"
    body = getattr(response, "content", b"")[:120]
    try:
        rendered = body.decode()  # a plain-text error page stays readable
    except UnicodeDecodeError:
        rendered = repr(body)  # still compressed — show the bytes as-is
    return f"content-encoding={encodings} body={rendered}"


def _parse_json_body(response: httpx.Response) -> Any:
    """Parse a ``/token`` JSON body, undoing an encoding httpx left in place."""
    try:
        return response.json()
    except Exception:
        pass
    body = response.content
    for name in reversed(_content_encodings(response)):
        decoder = _MANUAL_DECODERS.get(name)
        if decoder is not None:
            body = decoder(body)
    return json.loads(body)


def normalize_scope(raw_scopes: str | list[str] | None) -> str | None:
    """Normalize scopes into OAuth space-delimited scope string."""
    if raw_scopes is None:
        return None
    if isinstance(raw_scopes, list):
        joined = " ".join(str(x).strip() for x in raw_scopes if str(x).strip())
        return joined or None

    s = str(raw_scopes).strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            joined = " ".join(str(x).strip() for x in parsed if str(x).strip())
            return joined or None
        if isinstance(parsed, str):
            return parsed.strip() or None
    except (json.JSONDecodeError, ValueError):
        pass
    return s


def build_refresh_payload(
    refresh_token: str,
    client_id: str,
    scope: str | None = None,
) -> dict[str, str]:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if scope:
        payload["scope"] = scope
    return payload


async def refresh_oauth_token(
    client: httpx.AsyncClient,
    *,
    token_url: str,
    refresh_token: str,
    client_id: str,
    scope: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[str, int, str | None]:
    """Exchange a refresh token for a new access token.

    Returns (access_token, expires_at_epoch_ms, refresh_token_or_none).
    Raises RuntimeError on non-recoverable failures and HTTPStatusError on 429.
    """
    req_headers = dict(DEFAULT_REFRESH_HEADERS)
    if headers:
        req_headers.update(headers)

    r = await client.post(
        token_url,
        json=build_refresh_payload(refresh_token, client_id, scope=scope),
        headers=req_headers,
        timeout=60.0,
    )
    if r.status_code == 429:
        raise httpx.HTTPStatusError(
            "rate limited during token refresh",
            request=r.request,
            response=r,
        )
    if r.status_code >= 400:
        try:
            parsed = _parse_json_body(r)
        except Exception:
            parsed = None
        error_code = parsed.get("error") if isinstance(parsed, dict) else None
        detail = json.dumps(parsed) if parsed is not None else _undecodable_detail(r)
        raise OAuthRefreshError(
            f"OAuth refresh failed HTTP {r.status_code}: {detail[:300]}",
            status_code=r.status_code,
            error_code=error_code,
        )

    try:
        data = _parse_json_body(r)
    except Exception as exc:
        # A 2xx means Anthropic already rotated: the refresh token we sent is
        # spent and its replacement was in this body we cannot read. Retrying
        # would only spend the corpse, so say so and let the caller latch.
        raise OAuthRefreshError(
            f"Unreadable HTTP {r.status_code} refresh response: {_undecodable_detail(r)}",
            status_code=r.status_code,
            error_code="unreadable_response",
            token_consumed=True,
        ) from exc

    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthRefreshError(
            f"No access_token in refresh response: {str(data)[:300]}",
            status_code=r.status_code,
            error_code=data.get("error") if isinstance(data, dict) else None,
            token_consumed=True,
        )
    access_token = data["access_token"]

    expires_at = data.get("expires_at")
    if expires_at is None:
        expires_in = data.get("expires_in", 3600)
        expires_at = int(time.time() * 1000) + int(expires_in) * 1000

    return access_token, int(expires_at), data.get("refresh_token")


def build_activation_requests(
    *,
    base_url: str = DEFAULT_ACTIVATION_BASE_URL,
    access_token: str,
    claude_code_version: str = DEFAULT_CLAUDE_CODE_VERSION,
) -> list[dict]:
    """Build exact OAuth activation requests captured from Claude traffic."""
    host = urlparse(base_url).netloc or "api.anthropic.com"
    substitutions = {
        _CLI_UA: render_cli_user_agent(claude_code_version),
        _CODE_UA: render_code_user_agent(claude_code_version),
    }
    reqs: list[dict] = []
    for method, path, needs_auth, extra_headers in _ACTIVATION_STEPS:
        headers = {k: substitutions.get(v, v) for k, v in extra_headers.items()}
        headers["Host"] = host
        if needs_auth:
            headers["Authorization"] = f"Bearer {access_token}"
        reqs.append(
            {
                "method": method,
                "path": path,
                "url": base_url.rstrip("/") + path,
                "headers": headers,
            }
        )
    return reqs


async def activate_oauth_access_token(
    client: httpx.AsyncClient,
    *,
    access_token: str,
    base_url: str = DEFAULT_ACTIVATION_BASE_URL,
    timeout: float = 20.0,
    claude_code_version: str = DEFAULT_CLAUDE_CODE_VERSION,
) -> list[dict]:
    """Run post-refresh activation requests; raise if any request fails."""
    results: list[dict] = []
    for req in build_activation_requests(
        base_url=base_url,
        access_token=access_token,
        claude_code_version=claude_code_version,
    ):
        resp = await client.request(
            req["method"],
            req["url"],
            headers=req["headers"],
            timeout=timeout,
        )
        item = {
            "method": req["method"],
            "path": req["path"],
            "status_code": resp.status_code,
        }
        results.append(item)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OAuth activation request failed: {req['method']} {req['path']} "
                f"-> HTTP {resp.status_code}"
            )
    return results


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256 PKCE."""
    raw = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_claude_authorize_url(
    *,
    challenge: str,
    state: str,
    redirect_uri: str,
    client_id: str = CLAUDE_OAUTH_CLIENT_ID,
    scope: str = CLAUDE_OAUTH_SCOPE,
) -> str:
    params = {
        "code": "true",
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{CLAUDE_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_authorization_code(
    client: httpx.AsyncClient,
    *,
    token_url: str,
    code: str,
    verifier: str,
    state: str,
    redirect_uri: str,
    client_id: str = CLAUDE_OAUTH_CLIENT_ID,
) -> dict:
    """Exchange OAuth authorization code for tokens (PKCE)."""
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    r = await client.post(token_url, json=body, timeout=60.0)
    if r.status_code != 200:
        raise RuntimeError(f"token exchange HTTP {r.status_code}: {r.text[:500]}")
    return r.json()
