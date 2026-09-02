Anthropic proxy -- reverse-engineering / capture tooling
========================================================

This directory is the diagnostic kit used to work out what the upstream API
actually does (e.g. the day /v1/oauth/token started answering brotli-encoded).
It is not needed to run the proxy -- see the repository README for that.

WARNING: start_capture.sh installs a MITM certificate and a system-wide macOS
Network Extension via sudo. While it runs, TLS from EVERY process on the
machine is intercepted, not just Claude Code. Captures land unredacted on
disk and contain live bearer tokens. Read the script before running it, and
delete what it writes when you are done.

Two proxy modes are available:

  anthropic-proxy        Production proxy with key DB, OAuth auto-refresh, usage tracking
  anthropic-debug-proxy  Transparent pass-through proxy for debugging/capture


Production proxy (anthropic-proxy)
----------------------------------

Manages Anthropic API keys (SQLite locally, DATABASE_URL → PostgreSQL on deploy).
Supports two key types:
  - oauth   : access_token + refresh_token (auto-refreshes before expiry)
  - api_key : plain sk-ant-api... key

1. Import keys (from repo root):

   # OAuth key (from Claude Code's oauth.json / Keychain export):
   .venv/bin/python -m smart_proxy anthropic-key add-oauth oauth.json --name my-claude

   # Plain API key:
   .venv/bin/python -m smart_proxy anthropic-key add-apikey sk-ant-api... --name console-key

2. Create a proxy auth key (sp-*) if you don't have one:

   .venv/bin/python -m smart_proxy proxy-key add claude

3. Start the proxy:

   .venv/bin/python -m smart_proxy anthropic-proxy

   Default: http://0.0.0.0:8090 → https://api.anthropic.com
   Env: ANTHROPIC_PROXY_PORT (default 8090)
        ANTHROPIC_PROXY_UPSTREAM (default https://api.anthropic.com)
        ANTHROPIC_OAUTH_TOKEN_URL (default https://platform.claude.com/v1/oauth/token)
        ANTHROPIC_PROXY_DISABLE_1M_CONTEXT (default false; strips context-1m-* beta flags)

   Browser OAuth login (optional — no CLI):
        ANTHROPIC_OAUTH_LOGIN_BASE_URL   Public origin of this proxy, no path, e.g.
                                         http://203.0.113.10:8090 (you can open /_oauth/login in
                                         the browser by this URL). If unset, GET /_oauth/login
                                         returns 503.
        ANTHROPIC_OAUTH_LOGIN_REDIRECT_PORT  Optional. Port for redirect_uri
                                         http://localhost:<port>/callback sent to Anthropic
                                         (Claude’s allowlist is localhost, not your public IP).
                                         If empty, the port is taken from ANTHROPIC_OAUTH_LOGIN_BASE_URL
                                         if it includes :port, else ANTHROPIC_PROXY_PORT.
        ANTHROPIC_OAUTH_LOGIN_REDIRECT_URI  Optional; full redirect_uri override (rare).
        ANTHROPIC_OAUTH_LOGIN_SECRET     Optional. If set, start login only with
                                         ?secret=... or header X-OAuth-Login-Secret.

   Then open in a browser (use localhost if possible, same port as the proxy):

        http://localhost:8090/_oauth/login
        http://localhost:8090/_oauth/login?name=my-laptop           # stored key name in DB
        http://localhost:8090/_oauth/login?name=work&secret=...    # if secret is configured

   Without automatic redirect (page with link + paste-the-code form):

        http://localhost:8090/_oauth/login?manual=1
        http://localhost:8090/_oauth/login?manual=1&name=my-laptop

   Same PKCE session as normal login; after Anthropic redirects to
   http://localhost:<port>/callback you can copy the code (or full URL) into the form;
   POST /_oauth/submit exchanges it. Use SSH port forwarding (-L 8090:localhost:8090)
   so that redirect hits this proxy when it runs only on a remote host.

   Flow (redirect mode): GET /_oauth/login → redirect to claude.ai → GET /callback
   (or /_oauth/callback) with ?code=&state= → tokens saved to anthropic_keys, proxy
   reloads keys from DB.
   Default name for the row if ?name= is omitted: oauth-login.

4. Point Claude Code at the proxy — in .claude/settings.json:

   {
     "env": {
       "ANTHROPIC_BASE_URL": "http://127.0.0.1:8090",
       "ANTHROPIC_AUTH_TOKEN": "sp-..."
     }
   }

5. Manage keys:

   .venv/bin/python -m smart_proxy anthropic-key list
   .venv/bin/python -m smart_proxy anthropic-key deactivate <id-prefix>

   After add-oauth / add-apikey / deactivate / activate, the CLI tries POST to
   http://127.0.0.1:<port>/_reload so a running proxy picks up DB changes.
   If you use sp-* proxy auth, local reload from 127.0.0.1 still works without
   extra headers. For reload from another host, or GET in a browser / bookmark:

        GET  or POST  http://<host>:8090/_reload
        GET  http://<host>:8090/_reload?key=sp-xxxxxxxx        # same as ?token=

   Also accepted: Authorization: Bearer sp-...  or  x-api-key: sp-...
   If there is at least one active sp-* in the DB, requests that are not from
   localhost must pass a valid sp-* (or sk-ant-* passthrough); 127.0.0.1 and ::1
   are exempt so local scripts work. Optional env for the CLI notify URL:

        ANTHROPIC_PROXY_RELOAD_KEY=<one of your sp-* keys>

6. View usage:

   CLI (whole app / all providers):

   .venv/bin/python -m smart_proxy usage            # all providers
   .venv/bin/python -m smart_proxy usage --by-key   # per proxy key

   Anthropic OAuth usage snapshot from the running proxy (Claude platform /api/oauth/usage
   per stored OAuth key):

        GET http://<host>:8090/_oauth_usage

   By default only OAuth rows with status active or low_balance are queried;
   inactive keys are skipped (no refresh, no upstream call). To include inactive
   OAuth keys as well:

        GET http://<host>:8090/_oauth_usage?include_inactive=1
        GET http://<host>:8090/_oauth_usage?all_oauth=1          # alias

   JSON includes include_inactive_oauth: true|false. Cache (if enabled) is separate
   per flag. Related env:

        ANTHROPIC_OAUTH_USAGE_REQUIRE_AUTH   If true, same sp-* auth as the proxy
        ANTHROPIC_OAUTH_USAGE_CACHE_SECONDS  Cache TTL for the aggregated JSON

How it works:
  - Client authenticates with an sp-* proxy key
  - Proxy picks a healthy key from anthropic_keys by role (primary, then a
    promoted standby, then a scoped paid fallback)
  - For OAuth keys: checks expires_at, refreshes automatically if < 5 min left
  - Injects x-api-key header + Claude Code OAuth beta headers
  - On 401/403: deactivates key, retries with next available key
  - On 429: cooldown (30s default, per-model, capped at 1h), retries with next key
  - Tracks input/output tokens → usage_daily table

Debug proxy (anthropic-debug-proxy)
-----------------------------------

Transparent pass-through. Does not manage keys — forwards whatever auth the
client sends. Optionally captures raw request/response to disk.

   .venv/bin/python -m smart_proxy anthropic-debug-proxy

   Env: ANTHROPIC_DEBUG_PROXY_PORT, ANTHROPIC_DEBUG_PROXY_UPSTREAM,
        ANTHROPIC_DEBUG_PROXY_RAW_DIR (for unredacted captures)

See README-capture.txt for raw capture instructions.
