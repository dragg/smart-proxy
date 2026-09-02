"""Interactive OAuth PKCE login for Anthropic / Claude Code.

Starts a local HTTP server to receive the OAuth callback automatically,
opens the browser for login, exchanges the code for tokens, and saves
them to the anthropic_keys DB table.

Usage:
    python -m smart_proxy anthropic-login [--name LABEL]
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse

import httpx

from smart_proxy.anthropic_oauth import (
    CLAUDE_OAUTH_CLIENT_ID,
    build_claude_authorize_url,
    exchange_authorization_code,
    generate_pkce_pair,
)

TOKEN_URL = os.environ.get(
    "ANTHROPIC_OAUTH_TOKEN_URL",
    "https://platform.claude.com/v1/oauth/token",
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth redirect, extracts the authorization code."""

    code: str | None = None
    error: str | None = None
    _done: Event

    def do_GET(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        if "code" in qs:
            _CallbackHandler.code = qs["code"][0]
            body = b"<html><body><h2>Login successful!</h2><p>You can close this tab.</p></body></html>"
        else:
            _CallbackHandler.error = qs.get("error", ["unknown"])[0]
            body = f"<html><body><h2>Login failed: {_CallbackHandler.error}</h2></body></html>".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _CallbackHandler._done.set()

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


async def _exchange_code(code: str, verifier: str, state: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient() as client:
        return await exchange_authorization_code(
            client,
            token_url=TOKEN_URL,
            code=code,
            verifier=verifier,
            state=state,
            redirect_uri=redirect_uri,
            client_id=CLAUDE_OAUTH_CLIENT_ID,
        )


async def _save_to_db(data: dict, name: str) -> str:
    from smart_proxy.config import get_settings
    from smart_proxy.db import build_database

    settings = get_settings()
    db = build_database(settings)
    await db.connect()

    expires_at = data.get("expires_at")
    if not expires_at and data.get("expires_in"):
        expires_at = int((time.time() + data["expires_in"]) * 1000)

    scope_val = data.get("scope", "")
    scopes = scope_val.split() if isinstance(scope_val, str) else scope_val

    org = data.get("organization", {})
    account = data.get("account", {})
    sub_type = org.get("organization_type", "")
    tier = org.get("rate_limit_tier", "")

    try:
        key_id = str(uuid.uuid4())
        await db.insert_anthropic_key(
            id=key_id,
            key_type="oauth",
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=expires_at,
            scopes=json.dumps(scopes),
            subscription_type=sub_type,
            rate_limit_tier=tier,
            name=name,
        )
        return key_id
    finally:
        await db.close()


async def _run(name: str) -> None:
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)

    port = _find_free_port()
    redirect_uri = f"http://localhost:{port}/callback"

    done = Event()
    _CallbackHandler._done = done
    _CallbackHandler.code = None
    _CallbackHandler.error = None

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = build_claude_authorize_url(
        challenge=challenge,
        state=state,
        redirect_uri=redirect_uri,
    )

    print("Opening browser for Anthropic login...")
    print(f"\nIf the browser doesn't open, visit:\n{url}\n")
    webbrowser.open(url)

    print("Waiting for authorization callback...")

    got_callback = done.wait(timeout=300)
    server.shutdown()

    if not got_callback:
        print("Timed out waiting for callback (5 minutes).", file=sys.stderr)
        sys.exit(1)

    if _CallbackHandler.error:
        print(f"OAuth error: {_CallbackHandler.error}", file=sys.stderr)
        sys.exit(1)

    code = _CallbackHandler.code
    if not code:
        print("No authorization code received.", file=sys.stderr)
        sys.exit(1)

    print("Got authorization code, exchanging for tokens...")
    try:
        data = await _exchange_code(code, verifier, state, redirect_uri)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    access = data.get("access_token", "")
    refresh = data.get("refresh_token", "")
    expires_in = data.get("expires_in")

    if not access:
        print("No access_token in response:", file=sys.stderr)
        print(json.dumps(data, indent=2), file=sys.stderr)
        sys.exit(1)

    print(f"  access_token:  {access[:20]}...")
    print(f"  refresh_token: {refresh[:20]}..." if refresh else "  refresh_token: (none)")
    if expires_in:
        print(f"  expires_in:    {expires_in}s ({expires_in // 3600}h)")

    org = data.get("organization", {})
    if org.get("name"):
        print(f"  organization:  {org['name']}")

    key_id = await _save_to_db(data, name)
    print(f"\nSaved to DB as: {key_id}")
    print("Key is active and ready to use with anthropic-proxy.")


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    name = "oauth-login"
    if "--name" in argv:
        idx = argv.index("--name")
        if idx + 1 < len(argv):
            name = argv[idx + 1]

    asyncio.run(_run(name))


if __name__ == "__main__":
    main()
