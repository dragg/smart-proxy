# smart-proxy

A reverse proxy that puts a pool of Anthropic accounts behind a single endpoint.

Once you have more than one Claude subscription or API key, and more than one
thing that wants to use them — Claude Code on two machines, a few agents, a
teammate — the same problems show up. Every tool needs its own credential. One
rate-limited account blocks everything behind it. OAuth access tokens expire
every few hours, and a refresh that goes wrong takes the account offline until
somebody logs in again by hand. And afterwards nobody can say which project
spent what.

smart-proxy sits in front of the accounts and handles that. Callers get one URL
and a proxy key (`sp-…`) you can revoke or cap. Per request, the proxy picks a
healthy account, refreshes its token before it expires, fails over when one is
rate-limited, and records what each key, model and session cost.

**What it does**

- **One endpoint, many accounts.** Subscription OAuth sessions and `sk-ant-…`
  API keys share a pool with `primary` / `standby` / `fallback` roles.
- **Keeps tokens alive.** Access tokens refresh ahead of expiry, and a standby
  is kept warm so a failover never has to wait on a refresh of its own.
- **Fails over on the request path.** `401`/`403` deactivates a key, `429` puts
  it on a per-model cooldown, and the request retries on the next healthy one.
- **Per-caller keys and budgets.** Each `sp-…` key can carry a 24h USD cap;
  past it the caller gets a `429` saying when the window resets.
- **Accounts for cost** per key, model, request kind (main / subagent / helper)
  and Claude Code session — in a dashboard and over HTTP.
- **Speaks OpenAI too**, so tools that only know `/v1/chat/completions` can use
  the same pool.
- **Says something when it breaks.** Telegram alerts for the failures that are
  otherwise silent: a bricked refresh token, a database outage, a background
  loop that exited.

**What it is not:** a hosted service, a multi-tenant product, or a legal
opinion about whether pooling subscription credentials is allowed for your
account and your use. Read [SECURITY.md](SECURITY.md) before you deploy it —
it also lists the live credentials a deployment ends up holding.

**Requirements:** Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pip and
a venv). Node 20+ *only* for the dashboard. PostgreSQL *only* for production —
local runs use SQLite with no setup.

## Contents

- [Quickstart](#quickstart-local-sqlite)
- [How it works](#how-it-works)
- [Dashboard](#dashboard)
- [Deployment](#deployment)
- [Paid fallback keys](#paid-fallback-keys)
- [OpenAI-compatible endpoint](#openai-compatible-endpoint)
- [Request classification](#request-classification)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## Quickstart (local, SQLite)

No PostgreSQL, no systemd, no dashboard build.

```bash
git clone https://github.com/dragg/smart-proxy && cd smart-proxy
uv sync                 # or: python3 -m venv .venv && .venv/bin/pip install -e .

# 1. Log in to Anthropic. Opens a browser, does the PKCE exchange, and stores
#    the session in the local database:
uv run smart-proxy anthropic-login --name my-claude

# 2. Mint a key for callers (printed once — save it):
uv run smart-proxy proxy-key add local

# 3. Run it:
uv run smart-proxy anthropic-proxy      # http://0.0.0.0:8090 -> https://api.anthropic.com
```

Check it end to end:

```bash
curl http://127.0.0.1:8090/v1/messages \
  -H "Authorization: Bearer sp-..." \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-5","max_tokens":32,
       "messages":[{"role":"user","content":"ping"}]}'
```

Then point Claude Code at it in `.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8090",
    "ANTHROPIC_AUTH_TOKEN": "sp-..."
  }
}
```

**Other ways to attach an account.** `anthropic-login` is the easy path; these
also work:

| | |
|---|---|
| `smart-proxy anthropic-key add-apikey sk-ant-api... --name console-key` | a plain API key from the Anthropic console |
| `smart-proxy anthropic-key add-oauth oauth.json --name my-claude` | import an OAuth session you already exported |
| the dashboard's **Anthropic** tab | the same flow with a UI |

`smart-proxy anthropic-key list` shows the pool; `deactivate` / `activate` take
an id prefix.

**Notes for the first run.** The SQLite schema is created on `connect()`, so
**there is no migrate step locally** — `db migrate` exists for PostgreSQL only.
The file lands at `DB_PATH` (`./smart-proxy.db` by default) and holds live
OAuth tokens: treat it as secret material.

Copy `.env.example` to `.env` for anything beyond the defaults. Note the three
variables it marks *process env only* (`ANTHROPIC_PROXY_PORT`,
`ANTHROPIC_PROXY_UPSTREAM`, `ANTHROPIC_OAUTH_TOKEN_URL`): nothing here loads
`.env` into the environment, so those must be exported by the shell or by
systemd's `EnvironmentFile=`.

## How it works

One long-running service, `python -m smart_proxy anthropic-proxy`, listening on
`8090` (`ANTHROPIC_PROXY_PORT`). Everything below happens inside it.

```
 caller (Claude Code / SDK / curl)        smart-proxy                 Anthropic
        │  sp-… key                            │                          │
        ├─────────────────────────────────────>│  1  authenticate caller  │
        │                                      │  2  check its 24h cap    │
        │                                      │  3  pick a healthy key   │
        │                                      │  4  refresh it if due    │
        │                                      ├─────────────────────────>│
        │                                      │<─────────────────────────┤
        │                                      │  5  401/403 -> disable   │
        │                                      │     429    -> cooldown   │
        │                                      │     then retry on next   │
        │<─────────────────────────────────────┤  6  record tokens + cost │
```

**The upstream pool** (`anthropic_keys`) holds accounts in three roles. A
`primary` serves traffic. A `standby` is kept refreshed and is promoted the
moment the primary can no longer serve. A `fallback` is a paid `sk-ant-…` key
that is never promoted and only engages under the strict conditions in
[Paid fallback keys](#paid-fallback-keys).

**Callers** (`proxy_api_keys`) authenticate with an `sp-…` key sent as
`Authorization: Bearer <key>` or `x-api-key: <key>` — those two headers are the
only place the proxy looks on `/v1/*`. There is **no localhost exemption** on the
proxy path: being on the same machine does not authenticate you. (`?key=` works
on `/_usage`, `/_reload` and `/_oauth_usage`, and only `POST /_reload` treats
`127.0.0.1` specially.) Each `sp-…` key can carry a 24h USD cap.

A configured `sp-…` key is the **only** thing that gets in. A token that merely
starts with `sk-ant-` is not a credential and is refused, and a proxy with no
keys yet refuses everyone rather than opening up — so mint the first one with
`smart-proxy proxy-key add <name>` before anything can use the proxy. It binds
`0.0.0.0`, so put it behind a firewall or a reverse proxy either way.

**Accounting** is written per request: tokens and cost land in `usage_daily`
and `usage_key_hourly`, request kind in `usage_kind_daily`, and Claude Code
sessions in `usage_session`. `GET /_oauth_usage` reports each upstream
account's remaining quota as Anthropic itself sees it.

**Storage** is SQLite by default and PostgreSQL when `DATABASE_URL` is set. If
the database goes away, the proxy keeps serving from memory and alerts — it
does not take the pool down with it.

## Dashboard

An operator SPA at `/_app/`, built from `web/`.

**Two credentials, two levels.** An `sp-…` proxy key signs in **read-only** —
a consumer can see what it spent. Everything that *changes* the pool requires
`ANTHROPIC_PROXY_DASHBOARD_SECRET`, the operator password, which is never read
from a URL and expires from the session after 12 hours. Leave it unset and the
dashboard is read-only for everyone: the proxy says so at startup, and each
refused change answers `403` naming the variable to set.

- **Usage / Traffic / Windows / Compat** — cost per key, model, request kind
  and session; OAuth quota windows; OpenAI-compatible traffic broken out on its
  own tab.
- **Setup** — generates a ready-to-paste Claude Code statusline script wired to
  this proxy's origin and a key you pick.
- **Anthropic** — the upstream pool. Add a session with browser PKCE login
  (paste the callback URL, or let an SSH `-L` forward complete it), enable,
  disable, rename, soft-delete, force a refresh. This and the `anthropic-login`
  CLI are the only two ways to attach an account: the old server-rendered
  `/_oauth/login` page is gone, because its secret was empty by default and its
  paste form had no authentication at all.
- **Keys** — the `sp-…` callers, and their **24h spend limits**. Blank means
  unlimited, which is the default. The window resets at local midnight in
  `ANTHROPIC_PROXY_LIMIT_WINDOW_TZ` (default `Europe/Paris`); set it to `UTC`
  to line up with the dashboard's UTC days. Over the cap, `POST /v1/messages`
  answers `429` with the time remaining, and editing the limit takes effect on
  the next request with no restart. `GET /_oauth_usage?key=sp-…` reports the
  same numbers as a `smartproxy_daily_usd` entry in that key's `usage.limits[]`.

### Editable installs only

The SPA is built into `src/smart_proxy/static/app/`, which is **gitignored** —
so hatchling leaves it out of the sdist and the wheel. A non-editable
`pip install .` therefore answers `/_app/` with **503 `dashboard not built`**
*even after a successful `npm run build`*: the output stays in your source tree
and never reaches `site-packages`.

That is deliberate — this is a project with a build step, not a prebuilt
wheel — not a packaging bug to work around. The supported install is:

```bash
cd web && npm ci && npm run build     # -> src/smart_proxy/static/app/
cd .. && pip install -e .             # editable: the proxy reads those files in place
```

## Deployment

The proxy runs as a single systemd service, `smart-proxy`, executing
`python -m smart_proxy anthropic-proxy`. Production uses PostgreSQL (`DATABASE_URL`)
and runs from `~/smart-proxy` on the host. The dashboard SPA build output is
gitignored, so it must be produced as part of deploying.

Routine updates are a `git pull` + reinstall + restart (see below). Adapt paths
to your host.

1. **Build the dashboard SPA** (needs Node; run it locally or on the host):

   ```bash
   cd web && npm ci && npm run build   # → src/smart_proxy/static/app/ (gitignored)
   ```

   `src/smart_proxy/static/app/` must exist on the host. Because it is gitignored, a
   `git pull` alone will not deliver it — either run this build on the host, or
   copy the built directory over (e.g.
   `rsync -a src/smart_proxy/static/app/ HOST:~/smart-proxy/src/smart_proxy/static/app/`).

2. **Update the code on the host** and reinstall the venv:

   ```bash
   cd ~/smart-proxy && git pull
   .venv/bin/pip install -e .          # first time: python3 -m venv .venv first
   ```

3. **Configure `.env`** (see `.env.example`); at minimum set `DATABASE_URL`.

4. **Apply DB migrations** (PostgreSQL):

   ```bash
   .venv/bin/python -m smart_proxy db migrate
   ```

   Optionally follow it with `db backfill-wipes`, which derives any missing
   `oauth_limit_wipe` rows from the drop log. The proxy does this hourly by
   itself, so this only saves waiting for the first pass; it is idempotent and
   safe to re-run at any time.

   ```bash
   .venv/bin/python -m smart_proxy db backfill-wipes
   ```

5. **Create a proxy key** if none exists yet (shown once — save it):

   ```bash
   .venv/bin/python -m smart_proxy proxy-key add server
   ```

6. **Run under systemd.** Example `/etc/systemd/system/smart-proxy.service`:

   ```ini
   [Unit]
   Description=smart-proxy anthropic proxy
   After=network.target

   [Service]
   Type=simple
   WorkingDirectory=/home/USER/smart-proxy
   EnvironmentFile=/home/USER/smart-proxy/.env
   ExecStart=/home/USER/smart-proxy/.venv/bin/python -m smart_proxy anthropic-proxy
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now smart-proxy
   sudo systemctl restart smart-proxy        # after each update
   sudo journalctl -u smart-proxy -f         # follow logs
   ```

After a deploy that only changed keys, reload the pool without a restart via
`POST /_reload` (or the dashboard's **Reload** button).

## Paid fallback keys

An Anthropic **API key** (`sk-ant-…`) can be parked in the pool as a paid backup
for named consumers instead of a general-purpose credential. Add it on the
Anthropic tab with **Add API key…** — the form takes the key, its role and the
consumers that may use it, and writes all three in one INSERT. That matters: a
key created as the default `primary` would be live in the general pool, Claude
Code included, for as long as it took to go and change its role afterwards. An
existing key can still be switched with **Make fallback** and **Scope…**.

A fallback key serves a request only when all three hold:

1. the subscription tier has no pickable key for this request — dead refresh
   token, rate-limit cooldown, or `low_balance`;
2. the caller's `sp-` key is in the scope (an empty scope means *nobody*, and
   the `claude-passthrough` bucket — anything authenticated by a key that is
   not `sp-`-prefixed — can never qualify);
3. the request carries no Claude Code fingerprint — user agent, `x-app: cli`,
   the sub-agent header, the oauth beta, the "You are Claude Code" system
   prompt, or Claude Code's `metadata.user_id` session payload. Claude Code
   keeps getting its usual 429 rather than quietly spending money.

Guard rails: a proxy key must have a spend limit before it can be added to a
scope, and the limit is re-checked on every request, so clearing it revokes
paid access immediately. A fallback key is never promoted to primary, is never
picked by anything else, and takes no part in the smoke pass or the usage
poller. The first request it serves for a given consumer raises a Telegram
alert and a `fallback_serve` audit event (throttled to one per 30 minutes), as
does its deactivation.

Note that the per-proxy-key limit is denominated in API list prices, so it also
counts subscription traffic that costs nothing — it bounds paid spend from
above, not exactly.

## OpenAI-compatible endpoint

The Anthropic proxy also speaks the OpenAI Chat Completions protocol, so
OpenAI-only clients (Warp, Cline, aider, the `openai` SDK, …) can use the same
Anthropic key pool. Requests are translated to Anthropic's `/v1/messages`
format and dispatched back through the proxy itself over localhost, so key
rotation, OAuth refresh, retries, and streaming all behave exactly as for
native Anthropic traffic.

The native Anthropic pass-through is unaffected. The whole layer is behind a
feature flag (`ANTHROPIC_PROXY_OPENAI_COMPAT_ENABLED`, default on); turn it off
and the proxy behaves exactly as before.

### Endpoints

| Method & path                | Purpose |
|------------------------------|---------|
| `POST /v1/chat/completions`  | Chat Completions (streaming and non-streaming). |
| `GET /v1/models`             | OpenAI-format model list (static, from the `claude-*` price table). Requests carrying an `anthropic-version` header are delegated to the native Anthropic pass-through, so the native `GET /v1/models` is unchanged. |
| `GET /_openai_compat_stats`  | In-memory counters for the compat layer (requests, streaming requests, errors, tokens, per-model). Resets on restart. Requires proxy auth. |

Auth is the same as the native proxy: `Authorization: Bearer <sp-token>` (or
`x-api-key: <sp-token>`).

### Client setup

Point any OpenAI client at the proxy and use a real Claude model id:

- **Base URL:** `http://<proxy-host>:8090/v1`
- **API key:** your `sp-*` proxy key
- **Model:** e.g. `claude-sonnet-5`, `claude-opus-4-8`, `claude-fable-5`

```bash
# non-streaming
curl -s http://127.0.0.1:8090/v1/chat/completions \
  -H "Authorization: Bearer sp-..." -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-5","max_tokens":64,
       "messages":[{"role":"user","content":"Reply with exactly: pong"}]}'

# streaming with usage
curl -sN http://127.0.0.1:8090/v1/chat/completions \
  -H "Authorization: Bearer sp-..." -H 'content-type: application/json' \
  -d '{"model":"claude-sonnet-5","max_tokens":64,"stream":true,
       "stream_options":{"include_usage":true},
       "messages":[{"role":"user","content":"Count from 1 to 5"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8090/v1", api_key="sp-...")
client.chat.completions.create(
    model="claude-sonnet-5",
    messages=[{"role": "user", "content": "Hello"}],
)
```

A manual smoke script covering plain chat, streaming, and a tool-call round
trip lives at `anthropic-proxy-test/openai_compat_smoke.py`:

```bash
uv run --with openai python anthropic-proxy-test/openai_compat_smoke.py \
    --base-url http://127.0.0.1:8090/v1 --api-key sp-...
```

### What's supported

- Streaming (`stream: true`) and non-streaming, including
  `stream_options.include_usage`.
- Tools / function calling: `tools`, `tool_choice`
  (`auto`/`none`/`required`/named), assistant `tool_calls`, and `tool`-role
  results.
- Vision: `image_url` parts (both `data:` base64 and `http(s)` URLs).
- `system` / `developer` messages, `temperature` (clamped to Anthropic's
  0–1 range), `top_p`, `stop`, `max_tokens` / `max_completion_tokens`, `user`.
- **Automatic prompt caching.** The OpenAI protocol has no `cache_control`, so
  the transformer injects cache breakpoints (last system block + last message
  block) into the translated request. TTL is configurable, default `1h`.

Not supported: `n > 1` (returns 400); `logprobs`, `presence_penalty`,
`frequency_penalty`, `seed`, `response_format` (accepted but ignored);
`/v1/responses`, embeddings, audio.

### Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `ANTHROPIC_PROXY_OPENAI_COMPAT_ENABLED` | `true` | Register the compat routes. Off ⇒ proxy behaves exactly as before. |
| `ANTHROPIC_PROXY_OPENAI_COMPAT_DEFAULT_MAX_TOKENS` | `8192` | `max_tokens` used when the client omits it. |
| `ANTHROPIC_PROXY_OPENAI_COMPAT_AUTO_CACHE` | `true` | Inject `cache_control` breakpoints into translated requests. |
| `ANTHROPIC_PROXY_OPENAI_COMPAT_CACHE_TTL` | `1h` | TTL for injected breakpoints: `1h` or `5m`. |

### Tracking

Traffic that goes through the transformer is distinguishable everywhere:

- **Logs:** every compat request logs `[openai-compat] >>> …` / `<<< …`; the
  loopback carries user-agent `smart-proxy-openai-compat/1.0`. Native traffic has
  neither.
- **Response header:** every compat response carries `x-smart-proxy-openai-compat: 1`.
- **Stats endpoint:** `GET /_openai_compat_stats`.
- **Usage dashboard:** usage is attributed to the compat layer via the
  `usage_daily.via_openai_compat` dimension. In `/_usage`, each key's compat
  traffic shows as its own **`… · OpenAI`** row (separate requests, tokens,
  and cost) alongside its native traffic.

## Request classification

Each Anthropic-proxy request is automatically classified into one of three
categories — **`helper`**, **`main`**, or **`subagent`** — to distinguish
background calls from full agent turns:

- **`helper`**: requests with no tools (background utilities like title/summary
  generation or quota checks).
- **`main`**: full agent turn from Claude Code with a large system prompt
  (~27 KB observed).
- **`subagent`**: agent turn spawned by the main session with a smaller,
  stripped system prompt (~3–4 KB observed).

Classification is a **heuristic** based on system-prompt size (tunable via
`MAIN_SYSTEM_MIN_CHARS` threshold, currently ~15 KB). It is version-dependent
and may need re-tuning across Claude Code releases — not a guaranteed API flag.
Unclassified requests default to `unknown`.

Additionally, each request's Claude Code `session_id` (extracted from
`metadata.user_id`) is recorded and attributed to the proxy key that made it.
This allows usage to be grouped per conversation (session) and per key, giving
visibility into "whose session" uses which keys.

Request-kind and per-session data land in two additive tables,
`usage_kind_daily` (daily counters per kind and model) and `usage_session`
(session-level audit trail). Both are surfaced in the dashboard's **Traffic**
tab via `/api/usage/kinds` and `/api/sessions` endpoints.

## Troubleshooting

| Symptom | What it means | Fix |
|---|---|---|
| `/_app/` returns `503 dashboard not built` | The SPA is missing from `src/smart_proxy/static/app/`, or you installed non-editable | `cd web && npm ci && npm run build`, then `pip install -e .`. See [Editable installs only](#editable-installs-only) — a plain `pip install .` can never serve it |
| Proxy answers `401` | The caller's `sp-...` key is missing, wrong, or deactivated | `smart-proxy proxy-key add <name>` for a new one, or check the Keys tab. The key must arrive as `Authorization: Bearer` or `x-api-key` — a `?key=` query parameter is not read on `/v1/*`, and there is no localhost exemption there either |
| `429` mentioning a 24h limit | The caller's own spend cap, not Anthropic's | Raise or clear the cap on the Keys tab; it applies from the next request, no restart |
| `429` without that message | Anthropic rate-limited the upstream account | Expected. That key goes on a per-model cooldown and the request retries on the next one. If every account is cooling down, the caller sees the upstream 429 |
| An account flipped to inactive with `invalid_grant` | Its refresh token is dead. Anthropic rotates the refresh token on **every** refresh and retires the old one immediately, so a lost or raced refresh bricks the row | Re-authenticate with `smart-proxy anthropic-login --name <label>`. It inserts a **new** row rather than reviving the dead one, so delete the old one afterwards (Anthropic tab, or `anthropic-key deactivate <id-prefix>`). Never run two instances against the same account row |
| No Telegram alerts at all | `ANTHROPIC_TELEGRAM_BOT_TOKEN` and `ANTHROPIC_TELEGRAM_CHAT_ID` must **both** be set | Set both. At startup the proxy logs `Telegram alerts DISABLED` when they are missing, and sends one "proxy started" message when they work |
| A dashboard change answers `403 admin secret not configured` | Nobody can administer the pool yet — an `sp-…` key is read-only by design | Set `ANTHROPIC_PROXY_DASHBOARD_SECRET` (16+ chars, not an `sp-`/`sk-ant-` key), restart, and sign in with it |
| A dashboard change answers `403 admin required` | You are signed in with an `sp-…` key, which reads but does not administer | Sign in with the admin secret instead |
| The proxy exits at startup with `ANTHROPIC_PROXY_DASHBOARD_SECRET must be...` | The secret is too short, or looks like an `sp-`/`sk-ant-` key | Generate a real password, e.g. `openssl rand -base64 24` |
| `db migrate` refuses to run: `DATABASE_URL is required...` | You are on SQLite, where the schema is created on connect | Nothing to do — the command is PostgreSQL-only, and the error is how it says so |
| Exactly one test fails: `test_database_wiring` | A `DATABASE_URL` exported in your shell reaches `Settings()` | `unset DATABASE_URL` — see [Development](#development) |

## Development

```bash
uv run pytest -q        # 623 passed, 10 skipped
```

The suite runs against a temporary SQLite database and needs no services. The
10 skips are Postgres-specific tests (BIGINT counters, the connection breaker,
session upserts). To run those too, point the suite at a **throwaway**
PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/smart_proxy_tests uv run pytest -q
```

`connect_test_database` wipes the database it connects to, so never aim
`TEST_DATABASE_URL` at anything you care about. It is the **only** variable that
selects the test backend: `DATABASE_URL` is deliberately ignored, so a
development database can never be picked up by accident.

One caveat — a `DATABASE_URL` exported **in your shell** still reaches
`Settings()` and flips `tests/test_database_wiring.py`, which asserts the
default SQLite wiring. A `DATABASE_URL` line in `.env` does not (that test
builds `Settings(_env_file=None)`). Unset it in the shell, or expect that one
failure.
