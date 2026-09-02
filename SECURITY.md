# Security

## Reporting a vulnerability

Email **dragg.ko@gmail.com** with a description and, if possible, a minimal
reproduction. Please do not open a public issue for anything that exposes
credentials or lets an unauthenticated caller reach the upstream API. Expect an
acknowledgement within a few days.

## What this project holds

Run it and your deployment stores live credentials, in the database and in
`.env`:

- **Anthropic OAuth sessions** — access *and* refresh tokens for real Claude
  subscription accounts, stored in plaintext in the `anthropic_keys` table. A
  copy of the database is a copy of those accounts.
- **`sk-ant-…` API keys**, when a paid fallback key is configured.
- **`sp-…` proxy keys** (`proxy_api_keys`) — every one of them authenticates a
  caller against the whole pool, and signs in to `/_app/` read-only.
- **`ANTHROPIC_PROXY_DASHBOARD_SECRET`** — the operator password. It is what
  lets anyone attach or delete Anthropic accounts, change roles and edit spend
  limits. A caller's `sp-…` key deliberately cannot do any of that.
- **`.env`** — `DATABASE_URL` (with its password), the Telegram bot token, and
  `ANTHROPIC_PROXY_RELOAD_KEY`.

Consequences for an operator:

- Treat the database and `.env` as secret material. Back them up encrypted or
  not at all; `*.db`, `*.db.bak*`, `oauth*.json` and `.env` are gitignored
  precisely so they never reach a commit.
- The proxy listens on `0.0.0.0` by default. Put it behind a reverse proxy or a
  firewall — it is not hardened for the open internet. Every endpoint that
  changes anything requires the dashboard admin secret, but exposure still
  hands strangers your quota state and a login form to guess at.
- `GET /_oauth_usage` and `/_oauth_usage_history` require an `sp-…` key by
  default. They report quota state rather than secrets, but they do describe
  your accounts, so only set `ANTHROPIC_OAUTH_USAGE_REQUIRE_AUTH=false` on a
  network you trust.
- Anthropic rotates the OAuth refresh token on **every** refresh and retires the
  old one immediately. A refresh whose response is lost bricks that key until it
  is re-authenticated by hand. Do not run two instances against the same key row.
- Rotate anything that ever appeared in a log, a capture under
  `anthropic-proxy-test/`, or a shell history.

## Terms of service

The proxy sends Claude Code-shaped requests upstream and pools **subscription**
OAuth tokens across callers. That may conflict with the Anthropic Consumer or
Commercial Terms for your account and your use. This repository takes no
position on it and offers no legal advice: satisfying yourself that your
deployment is permitted is your responsibility, not the author's.

The software is provided as-is under the MIT License, with no warranty.
