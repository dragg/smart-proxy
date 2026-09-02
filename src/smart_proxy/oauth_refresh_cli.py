"""Manual OAuth refresh against Anthropic token endpoint.

Usage (secrets only via env, never commit):

  export ANTHROPIC_REFRESH_TOKEN='sk-ant-ort01-...'
  export ANTHROPIC_OAUTH_CLIENT_ID='xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'  # from authorize URL ?client_id=
  python -m smart_proxy oauth-refresh

In zsh/bash use ``export`` (not ``set -x VAR value`` — in zsh ``set -x`` enables command tracing
and does not assign variables; in fish the syntax is ``set -x VAR value``).

client_id must match the OAuth app used when you logged in. Copy it from the browser
when Claude opens login: parameter client_id=... in the authorize URL (it can change).

Env:
  ANTHROPIC_REFRESH_TOKEN     required
  ANTHROPIC_OAUTH_CLIENT_ID   required (from authorize URL)
  ANTHROPIC_OAUTH_SCOPE       optional scope string (space-delimited or JSON list)
  ANTHROPIC_OAUTH_TOKEN_URL   optional, default https://platform.claude.com/v1/oauth/token
  ANTHROPIC_OAUTH_REFRESH_DEBUG=1  print request (refresh_token masked) to stderr
"""

from __future__ import annotations

import json
import asyncio
import os
import re
import sys

import httpx

from smart_proxy.config import get_settings
from smart_proxy.db import build_database
from smart_proxy.anthropic_oauth import (
    DEFAULT_REFRESH_HEADERS,
    activate_oauth_access_token,
    build_refresh_payload,
    normalize_scope,
    refresh_oauth_token,
)

DEFAULT_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"

_OAUTH_CLIENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _mask_secret(s: str, keep: int = 12) -> str:
    if not s:
        return "(empty)"
    if len(s) <= keep:
        return "***"
    return s[:keep] + "…"


def _client_id_error_message(client_id: str) -> str | None:
    """Return error text if client_id is obviously wrong; else None."""
    if "<" in client_id or ">" in client_id:
        return (
            "ANTHROPIC_OAUTH_CLIENT_ID looks like a placeholder (contains < or >).\n"
            "Paste the real UUID from the authorize URL (client_id=...), not the instruction text."
        )
    if "copy from" in client_id.lower() or "copy the" in client_id.lower():
        return (
            "ANTHROPIC_OAUTH_CLIENT_ID must be the UUID only, not the sentence from the docs.\n"
            "Example: .../authorize?client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&..."
        )
    if not _OAUTH_CLIENT_ID_RE.match(client_id):
        return (
            "ANTHROPIC_OAUTH_CLIENT_ID must be a UUID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx).\n"
            "If you use zsh/bash, run: export ANTHROPIC_OAUTH_CLIENT_ID='9d1c250a-...'\n"
            "Do not use `set -x ANTHROPIC_OAUTH_CLIENT_ID ...` in zsh — that turns on tracing "
            "and does not set the variable."
        )
    return None


async def _notify_running_proxy_reload() -> None:
    port = os.environ.get("ANTHROPIC_PROXY_PORT", "8090").strip() or "8090"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"http://127.0.0.1:{port}/_reload", timeout=5.0)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    from_db = "--from-db" in argv
    skip_activation = "--skip-activation" in argv
    activation_base_url = "https://api.anthropic.com"
    if "--activation-base-url" in argv:
        idx = argv.index("--activation-base-url")
        if idx + 1 >= len(argv):
            print("--activation-base-url requires a value", file=sys.stderr)
            sys.exit(1)
        activation_base_url = argv[idx + 1].strip()
        if not activation_base_url:
            print("--activation-base-url cannot be empty", file=sys.stderr)
            sys.exit(1)

    id_prefix = ""
    if "--id-prefix" in argv:
        idx = argv.index("--id-prefix")
        if idx + 1 >= len(argv):
            print("--id-prefix requires a value", file=sys.stderr)
            sys.exit(1)
        id_prefix = argv[idx + 1].strip()
        if not id_prefix:
            print("--id-prefix cannot be empty", file=sys.stderr)
            sys.exit(1)

    token_url = os.environ.get("ANTHROPIC_OAUTH_TOKEN_URL", DEFAULT_TOKEN_URL).strip()
    refresh = ""
    client_id = ""
    scope: str | None = None
    selected_key_id = ""

    if from_db:
        async def _load_from_db() -> tuple[str, str, str | None, str]:
            settings = get_settings()
            db = build_database(settings)
            await db.connect()
            try:
                rows = await db.get_active_anthropic_keys()
                oauth_rows = [r for r in rows if r.get("key_type") == "oauth"]
                if id_prefix:
                    prefix = id_prefix.lower()
                    oauth_rows = [
                        r
                        for r in oauth_rows
                        if str(r.get("id", "")).lower().startswith(prefix)
                    ]
                if not oauth_rows:
                    raise RuntimeError("No active OAuth key found in DB for --from-db.")
                if len(oauth_rows) > 1:
                    ids = ", ".join(str(r.get("id", ""))[:12] for r in oauth_rows[:5])
                    raise RuntimeError(
                        f"Multiple active OAuth keys match ({len(oauth_rows)}): {ids}. "
                        "Use --id-prefix to pick one."
                    )
                row = oauth_rows[0]
                r_token = (row.get("refresh_token") or "").strip()
                c_id = (row.get("client_id") or "").strip()
                if not r_token:
                    raise RuntimeError("Selected DB key has empty refresh_token.")
                if not c_id:
                    raise RuntimeError("Selected DB key has empty client_id.")
                sc = normalize_scope(row.get("scopes"))
                return r_token, c_id, sc, str(row.get("id", ""))
            finally:
                await db.close()

        try:
            refresh, client_id, scope, selected_key_id = asyncio.run(_load_from_db())
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    else:
        refresh = os.environ.get("ANTHROPIC_REFRESH_TOKEN", "").strip()
        if not refresh:
            print(
                "Set ANTHROPIC_REFRESH_TOKEN to the refresh token value "
                "(e.g. from Keychain JSON claudeAiOauth.refreshToken).",
                file=sys.stderr,
            )
            sys.exit(1)
        client_id = os.environ.get("ANTHROPIC_OAUTH_CLIENT_ID", "").strip()
        if not client_id:
            print(
                "Set ANTHROPIC_OAUTH_CLIENT_ID to the OAuth client_id from your login flow.\n"
                "When the browser opens authorize, copy the client_id query parameter from the URL, e.g.\n"
                "  .../oauth/authorize?...&client_id=<UUID>&...\n"
                "It must match the app that issued your refresh token.",
                file=sys.stderr,
            )
            sys.exit(1)
        scope = normalize_scope(os.environ.get("ANTHROPIC_OAUTH_SCOPE", "").strip() or None)

    cid_err = _client_id_error_message(client_id)
    if cid_err:
        print(cid_err, file=sys.stderr)
        sys.exit(1)

    body = build_refresh_payload(refresh, client_id, scope=scope)

    if os.environ.get("ANTHROPIC_OAUTH_REFRESH_DEBUG", "").strip() in ("1", "true", "yes"):
        redacted = {**body, "refresh_token": _mask_secret(refresh)}
        print("--- request (debug) ---", file=sys.stderr)
        print(f"POST {token_url}", file=sys.stderr)
        print("Headers:", file=sys.stderr)
        for name, value in DEFAULT_REFRESH_HEADERS.items():
            print(f"  {name}: {value}", file=sys.stderr)
        print("JSON body:", file=sys.stderr)
        print(json.dumps(redacted, indent=2, ensure_ascii=False), file=sys.stderr)
        print("--- end request ---", file=sys.stderr)

    async def _run_refresh() -> tuple[int, dict]:
        async with httpx.AsyncClient() as client:
            try:
                access_token, expires_at, rotated_refresh = await refresh_oauth_token(
                    client,
                    token_url=token_url,
                    refresh_token=refresh,
                    client_id=client_id,
                    scope=scope,
                )
                activation_result: list[dict] = []
                if not skip_activation:
                    activation_result = await activate_oauth_access_token(
                        client,
                        access_token=access_token,
                        base_url=activation_base_url,
                    )
                return 200, {
                    "token_type": "Bearer",
                    "access_token": access_token,
                    "expires_at": expires_at,
                    "refresh_token": rotated_refresh,
                    "activation": activation_result,
                }
            except httpx.HTTPStatusError as exc:
                try:
                    return exc.response.status_code, exc.response.json()
                except json.JSONDecodeError:
                    return exc.response.status_code, {"error": exc.response.text}
            except RuntimeError as exc:
                return 400, {"error": str(exc)}

    status, out = asyncio.run(_run_refresh())
    if from_db and status == 200 and selected_key_id:
        new_access = (out.get("access_token") or "").strip()
        new_refresh = (out.get("refresh_token") or None)
        new_expires_at = out.get("expires_at")
        if new_access and isinstance(new_expires_at, int):
            async def _save_to_db() -> None:
                settings = get_settings()
                db = build_database(settings)
                await db.connect()
                try:
                    await db.update_anthropic_oauth_tokens(
                        selected_key_id,
                        new_access,
                        int(new_expires_at),
                        new_refresh if isinstance(new_refresh, str) and new_refresh else None,
                    )
                finally:
                    await db.close()

            asyncio.run(_save_to_db())
            asyncio.run(_notify_running_proxy_reload())
            out["updated_db_key_id"] = selected_key_id

    print(f"HTTP {status}", file=sys.stderr)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
