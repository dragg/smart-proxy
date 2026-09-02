# Anthropic keys tab + OAuth login migration into the dashboard

Date: 2026-07-18
Status: approved

## Goal

Migrate the legacy browser OAuth login flow
(`https://proxy.example.com/_oauth/login?manual=true&name=...`) into the Svelte
dashboard (`/_app/`), and add a new **Anthropic** tab that lists the
`anthropic_keys` pool with full management: add a new OAuth session,
enable/disable, rename, soft-delete, and force a token refresh.

The legacy server-rendered flow (`/_oauth/login`, `/_oauth/submit`,
`/callback`) **stays working as a fallback**, sharing the same core logic.

## Background (current state)

- `anthropic_proxy.py` implements the whole PKCE flow server-side:
  `_oauth_login_start` (HTML page when `?manual=true`), `_oauth_manual_submit`
  (POST pasted code), `_oauth_login_callback` (`/callback`,
  `/_oauth/callback`), with in-memory PKCE sessions in
  `app["_oauth_login_sessions"]` (TTL 600 s) and an optional
  `oauth_login_secret` gate.
- The OAuth `redirect_uri` is always `http://localhost:PORT/callback`
  (Anthropic whitelist), so on a remote deployment the practical path is
  pasting the callback URL/code manually; with an SSH port-forward the
  `/callback` page completes automatically and notifies the opener tab via
  `BroadcastChannel('smart_proxy_oauth')` + `localStorage`.
- The dashboard is a Svelte 5 SPA (`web/`) served at `/_app/` with JSON API in
  `dashboard_api.py`. Read endpoints use `_dashboard_authorized` (any valid
  token incl. cookie), mutating endpoints use `_action_authorized`
  (configured `sp-*` proxy key only).
- `anthropic_keys` table: `id`, `key_type` (`oauth`/`api_key`), `status`,
  `api_key`, `access_token`, `refresh_token`, `client_id`, `expires_at` (epoch
  ms), `scopes`, `subscription_type`, `rate_limit_tier`, `name`, `created_at`,
  `updated_at`. The pool loads only `status IN ('active', 'low_balance')`.
- Existing DB methods: `list_anthropic_keys()`, `set_anthropic_key_status()`
  (with audit kwargs), `insert_anthropic_key()`,
  `update_anthropic_oauth_tokens()`. There is no rename method yet.

## Design

### 1. Shared OAuth exchange core (`anthropic_proxy.py`)

Extract the body of `_oauth_run_code_exchange` into a helper:

```
async def _oauth_exchange_and_store(app, *, code: str, state: str) -> dict
```

- Pops the PKCE session for `state` (error if unknown/expired).
- Exchanges the code (`exchange_authorization_code`), inserts the row into
  `anthropic_keys`, reloads the pool.
- Returns `{"key_id": ..., "name": ...}` on success; raises a small typed
  exception (`OAuthExchangeError(status, message)`) on failure so both the
  HTML and JSON paths can render it their own way.
- `_oauth_run_code_exchange` (HTML flow) becomes a thin wrapper — legacy
  behavior unchanged, including the BroadcastChannel notify script.

### 2. New JSON endpoints (`dashboard_api.py`)

Read (guard: `_dashboard_authorized`):

- `GET /api/anthropic/keys` — rows from `db.list_anthropic_keys()` excluding
  `status='deleted'`. Fields per key: `id`, `key_type`, `status`, `name`,
  `subscription_type`, `rate_limit_tier`, `created_at`, `updated_at`,
  `expires_at`, `has_refresh_token` (bool). **Token material (`access_token`,
  `refresh_token`, `api_key`) is never returned.**

Mutations (guard: `_action_authorized`; audit via existing
`set_anthropic_key_status` audit kwargs with `audit_source="dashboard"`):

- `POST /api/anthropic/oauth/start` `{name}` → creates a PKCE session in the
  same `app["_oauth_login_sessions"]` store → `{state, authorize_url,
  redirect_uri}`. No `oauth_login_secret` needed (dashboard auth replaces it);
  works even when `ANTHROPIC_OAUTH_LOGIN_BASE_URL` is unset, since the API
  does not need the base URL — only the redirect port resolution
  (`_oauth_redirect_uri`).
- `POST /api/anthropic/oauth/submit` `{state, code}` — `code` may be a bare
  code or a whole callback URL (`_normalize_pasted_auth_code`). Calls the
  shared core → `{ok: true, key_id}`. Errors map to 400 (bad/expired state,
  missing code) or 502 (exchange failed) with a JSON error message.
- `POST /api/anthropic/keys/status` `{id, active: bool}` →
  `set_anthropic_key_status(id, 'active'|'inactive')` + pool reload.
  404 if the id doesn't exist.
- `POST /api/anthropic/keys/rename` `{id, name}` → new DB method
  `set_anthropic_key_name(id, name)` (updates `name`, `updated_at`).
  400 on empty name, 404 on unknown id. Pool reload after rename so in-memory
  key labels match the DB.
- `POST /api/anthropic/keys/delete` `{id}` → soft delete:
  `set_anthropic_key_status(id, 'deleted')` + pool reload. History
  (usage, windows, audit) stays intact; row recoverable via SQL.
- `POST /api/anthropic/keys/refresh` `{id}` — oauth keys only (400 for
  `api_key` type or missing refresh token). Forces `refresh_oauth_token` +
  `update_anthropic_oauth_tokens` + the activation request sequence
  (`activate_oauth_access_token`), then pool reload → `{ok, expires_at}`.
  502 with the error text on failure.

No DB schema migration: `deleted` is just a new `status` value the pool
already ignores; rename uses the existing `name` column.

### 3. Frontend (`web/`)

- New `web/src/views/AnthropicView.svelte`; new tab `anthropic` in
  `App.svelte` (label "Anthropic", between Windows and Compat).
- Key table: name, type (`oauth`/`api_key`), status, subscription + tier,
  token expiry (relative, e.g. "in 3h" / "expired"), created date. Row
  actions: Enable/Disable, Rename (inline input), Refresh (oauth only,
  shows new expiry), Delete (with `confirm()`).
- "Add OAuth session" panel:
  1. Name input → **Start** → `POST /api/anthropic/oauth/start`; open
     `authorize_url` in a new tab (fallback link if the popup is blocked).
  2. Show a paste field: "callback URL or code" → **Save** →
     `POST /api/anthropic/oauth/submit {state, code}`.
  3. In parallel, subscribe to `BroadcastChannel('smart_proxy_oauth')` and the
     `storage` event for key `smart_proxy_oauth_<state>` — when the SSH-forwarded
     `/callback` page completes the exchange, the tab auto-shows success.
  4. On success (either path): show the new key id, clear the wizard, reload
     the key list.
- The redirect note from the legacy page is preserved in the UI: Anthropic
  redirects to `http://localhost:PORT/callback`; if the proxy is remote, use
  an SSH `-L` forward or paste the callback URL.

### 4. Testing

pytest, following `tests/test_dashboard_api.py` patterns (aiohttp test
client, fake pool):

- Auth gates: reads accept dashboard token, mutations reject non-proxy keys.
- `GET /api/anthropic/keys`: token material absent, `deleted` rows excluded.
- `oauth/start` → session created, authorize URL sane; `oauth/submit` with a
  mocked token endpoint → row inserted, pool reloaded; expired/unknown state
  → 400.
- status/rename/delete/refresh happy paths + 400/404 validation.
- Legacy flow regression: `/_oauth/login` + `/_oauth/submit` still work via
  the shared core.
- Frontend: `npm run build` passes; manual smoke via the dashboard.

### 5. Deployment

Standard cycle, no DB migration: build SPA (`cd web && npm ci && npm run
build`), `git pull` + restart `smart-proxy`. All work happens on branch
`worktree-anthropic-keys-tab` in a git worktree to avoid disturbing the main
checkout.

## Out of scope

- Adding raw `sk-ant-*` API keys from the dashboard (CLI `anthropic-key
  add-apikey` remains the path).
- Removing the legacy `/_oauth/login` flow or its secret gate.
- Hard deletion / purging token material of deleted keys.
