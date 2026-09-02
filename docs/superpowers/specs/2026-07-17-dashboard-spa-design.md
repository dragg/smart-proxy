# Dashboard SPA (Svelte + Vite) over the Anthropic Proxy — Design

Date: 2026-07-17
Status: draft, pending user review

## Problem

The proxy's UI layer today is a set of one-off, hand-written surfaces:

- `GET /_usage` — the only real dashboard page. ~130 lines of HTML/CSS baked
  into Python f-strings in `usage_dashboard.py` (cost table by proxy-key /
  model, a date-range form). Hard to extend, no charts, no live refresh.
- `GET /_oauth_usage`, `GET /_oauth_usage_history`, `GET /_openai_compat_stats`
  — JSON endpoints with **no visualization**. The data exists; nothing renders
  it.
- `GET /_oauth/login`, `GET /callback`, `POST /_oauth/submit` — tiny
  transactional OAuth-flow HTML pages.

We want to stop writing throwaway HTML strings and stand up a maintainable UI
that (1) moves markup out of Python f-strings, and (2) finally visualizes the
already-collected JSON (OAuth quotas, compat stats) with charts and live
refresh, plus a few **operator actions** (reload proxy, enable/disable a proxy
key).

The proxy core itself (SSE streaming, OAuth, cache-TTL rewriting) is deeply
aiohttp and is **out of scope** — it is not rewritten.

## Decisions (from brainstorming)

- **Scope: dashboards + actions.** Read-only metrics *plus* a small set of
  operator actions (reload, key enable/disable). Not a full multi-page control
  panel.
- **Frontend: Svelte 5 + Vite + TypeScript.** A real component framework with a
  build step, chosen deliberately over no-build (HTMX/Alpine) because the user
  wants a modern JS stack.
- **Backend: stay on aiohttp.** Add JSON `/api/*` routes to the existing proxy
  app and serve the built SPA as static from the same process. No FastAPI —
  aiohttp is not ASGI, so FastAPI alongside it would mean a second server /
  process for a handful of endpoints that already have `db`/auth/pricing wired
  into the aiohttp app.
- **Charts: Chart.js** (simple DX, canvas time-series). uPlot considered and
  rejected for the first cut (leaner but harsher API).
- **Styling: a small CSS-token set, no Tailwind** on the first cut (palette
  carried over from the current `/_usage`). Tailwind can come later if the UI
  grows.
- **SPA lives under `/_app/`**, not replacing `/_usage` immediately. `/_usage`
  stays live until the SPA reaches parity, then is removed in a follow-up
  commit (no regression during transition).
- **Auth is a single `sp-*` token = operator password.** No user table. This is
  an internal single-operator tool.

## Repository layout & tooling

```
smart-proxy/
├─ src/smart_proxy/
│  ├─ dashboard_api.py       # NEW: registers /api/* + SPA static serving
│  └─ static/app/            # NEW: Vite build output (gitignored)
└─ web/                      # NEW: frontend project
   ├─ package.json           # npm; Svelte 5 + Vite + TypeScript
   ├─ vite.config.ts         # dev proxy /api,/_* → :8090; build → ../src/smart_proxy/static/app
   ├─ tsconfig.json
   └─ src/
      ├─ main.ts
      ├─ App.svelte
      ├─ lib/api.ts          # typed fetch client (sends credentials)
      ├─ lib/auth.ts         # login state
      └─ views/              # UsageView, OAuthUsageView, CompatStatsView, KeysView
```

- Package manager: **npm** (simplest for solo/CI, no extra tooling).
- TypeScript on (standard for Svelte + Vite).
- `src/smart_proxy/static/app/` is gitignored; the build artifact ships via deploy,
  not via git.

## Backend: aiohttp serves API + static

New module `src/smart_proxy/dashboard_api.py`, wired in `create_app()` **before**
the catch-all routes (`add_route("*", "/", ...)` and `"/{path:.+}"`), mirroring
how `register_usage_dashboard` is wired today.

Routing:

- `/api/*` — JSON endpoints (see below).
- `GET /_app/` and `GET /_app/{path:.*}` — serve the SPA. `/_app/` and unknown
  sub-paths return `index.html` (client-side routing fallback); hashed asset
  requests are served from `static/app/`.
- Registered before `_root_handler` (`/`) and `_proxy_handler` (`/{path:.+}`),
  so proxy traffic is unaffected.

The module follows the existing injection pattern (`authorize`, `get_db`
callables passed in) so it stays decoupled from the concrete app layout, like
`usage_dashboard.py`.

### API endpoints

Read (JSON reshaping of data that already exists):

| Endpoint | Source |
|---|---|
| `GET /api/usage?start&end` | reuses `_build_usage_cost_groups` (same logic as `/_usage`, JSON out) |
| `GET /api/oauth/usage` | wraps existing `_oauth_usage_handler` data |
| `GET /api/oauth/usage/history` | wraps existing `_oauth_usage_history_handler` data |
| `GET /api/openai-compat/stats` | wraps existing `openai_compat._stats_handler` data |
| `GET /api/keys` | `db.list_proxy_keys()` → `sp-*` list with `active` flag |

Actions:

| Endpoint | Behavior |
|---|---|
| `POST /api/reload` | calls the existing reload path |
| `POST /api/keys/{key}/active` `{active: bool}` | enable/disable a proxy key |

- `deactivate` already exists in `db`; an **activate** method is added
  (`UPDATE proxy_api_keys SET active = 1 WHERE key = ?`).
- Adding OAuth accounts is **not** rebuilt in the SPA — the existing
  `/_oauth/login` flow is linked to from the dashboard. Keeps the first cut
  small.

Response shaping may use Pydantic models internally (already a dependency via
`pydantic-settings`), but that is an implementation detail, not required.

## Auth for the SPA and actions

Today `/_usage` accepts an `sp-*` token via `?key=` or header; `/_oauth_usage`
is open on the LAN. Actions (reload, key toggle) cannot be open. Design:

- `POST /api/session` — accepts an `sp-*` token, validates it via
  `AnthropicKeyPool.check_auth`, and sets a **signed httpOnly cookie** on
  success. The SPA login screen is a single "paste sp- token" field.
- All `/api/*` authorize via **cookie OR** `Authorization: Bearer` / `x-api-key`
  (backward-compatible for curl).
- Action endpoints (`POST /api/*`) always require auth.
- The cookie is signed (e.g. `itsdangerous`-style HMAC over the validated
  token or a random session id kept in-memory) so it cannot be forged. Exact
  mechanism is an implementation detail for the plan; no persistent session
  store is required for a single-operator tool.

`/_oauth_usage`'s existing `ANTHROPIC_OAUTH_USAGE_REQUIRE_AUTH` behavior is
unchanged; the new `/api/oauth/usage` wrapper requires auth like the rest of
`/api/*`.

## Frontend: Svelte + charts + style

- **Svelte 5** (runes). Client-side navigation is minimal — tabs on the first
  cut, no router needed (add `svelte-spa-router` only if views multiply).
- **Chart.js** for time-series (usage over the date range, OAuth quota history).
- **Styling:** one small CSS-token file carrying the current `/_usage` palette
  (`#111827`, `#6b7280`, etc.). No Tailwind on the first cut.
- **Views (first cut):**
  - Usage — the cost table (parity with `/_usage`) + a per-day cost chart.
  - OAuth usage — current quota + history chart.
  - Compat stats — the OpenAI-compat counters.
  - Keys — list of `sp-*` keys with enable/disable toggles + a Reload button.

## Dev workflow

`npm run dev` starts Vite on :5173 with HMR and a dev proxy forwarding `/api`
and `/_*` to the aiohttp proxy on :8090. Edit Svelte live against the real API.
Production serves only the built static; the dev server is not involved.

## Deploy

`deploy.sh` gains a frontend build step **on the deploying machine** (node
required locally, not on prod):

```
cd web && npm ci && npm run build      # → src/smart_proxy/static/app
```

then rsyncs `src/smart_proxy/static/app` with the rest of the source. The prod host
stays node-free. `static/app/` is gitignored.

## Testing

- **Backend:** unit tests for `/api/*` mirroring `test_usage_dashboard.py` —
  auth (cookie/header/unauthorized), `usage` response shape, `keys` list, and
  the actions (`reload`, key `active` toggle). These run in the existing Python
  suite.
- **Frontend:** no test runner on the first cut (YAGNI). Add Vitest only if the
  UI grows.

## Migration / coexistence

1. Ship the SPA at `/_app/` alongside the untouched `/_usage`.
2. Once the Usage view reaches parity with `/_usage`, remove `/_usage`
   (`usage_dashboard.py` route) in a separate commit.
3. `/_oauth/login` and other transactional OAuth pages stay as server-rendered
   HTML — they are not part of the SPA.

## Out of scope (first cut)

- Rewriting the proxy core or swapping its web framework.
- Rebuilding the OAuth login/add-account flow in the SPA (linked to instead).
- Tailwind, a client-side router, a frontend test runner, a persistent session
  store, or a multi-user auth model. All deferred until the UI demonstrably
  needs them.
