# OpenAI-Compatible Endpoint on the Anthropic Proxy — Design

Date: 2026-07-03
Status: draft, pending user review

## Problem

Coding agents that only speak the OpenAI protocol (Warp, Cline, aider, and
similar) cannot use the Anthropic proxy today. We want them served by the same
key pool (OAuth + API keys) without touching the native Anthropic pass-through
path, and we want visibility into which traffic arrives through the
translation layer.

Hard constraints:

1. The native Anthropic format handling (`_proxy_handler` and everything it
   calls) must not change behavior in any way.
2. Requests that go through the OpenAI→Anthropic transformer must be
   trackable — "know that this traffic exists and how much of it there is".

## Approach (chosen): loopback transformer

New module `src/smart_proxy/openai_compat.py`. Routes registered in `create_app`
**before** the catch-all:

- `POST /v1/chat/completions` — the OpenAI Chat Completions endpoint
  (streaming and non-streaming).
- `GET /v1/models` — OpenAI-style model list (Warp/Cline probe it).
- `GET /_openai_compat_stats` — tracking endpoint (see Tracking).

The chat handler:

1. Validates client auth with the existing `pool.check_auth`
   (same `sp-` tokens; `Authorization: Bearer` — already supported by
   `_extract_client_token`).
2. Converts the OpenAI request body to an Anthropic `/v1/messages` body
   (pure function, no I/O).
3. Sends it over local HTTP to the proxy itself
   (`http://127.0.0.1:{PROXY_PORT}/v1/messages`), forwarding the client's
   original bearer token, using a dedicated
   `app["openai_compat_http_client"]` (created/closed by lifecycle hooks
   registered in `setup_openai_compat`, unbounded connection limit). A
   dedicated client is required: with the shared `app["http_client"]` each
   compat request would hold two slots of one pool (outer loopback + inner
   upstream) and a compat burst could deadlock all proxy traffic until the
   pool timeout. The loopback base URL lives in
   `app["openai_compat_loopback_base"]` so tests can point it at a fake
   server.
4. Converts the response back: non-streaming JSON → OpenAI chat completion
   object; Anthropic SSE → OpenAI chunk SSE (incremental, no full buffering).

Why loopback and not a shared-core refactor: the inner request re-enters
`_proxy_handler` unchanged, so key picking, OAuth token refresh, billing
header injection, beta query param, retries, pre-commit SSE buffering, usage
recording, and error classification are all inherited with zero changes to
the battle-tested hot path. The `git diff` on `anthropic_proxy.py` is limited
to route registration and config plumbing.

Rejected alternatives:

- **Shared forwarding core (refactor)** — one hop less, but refactors the hot
  path and violates constraint 1's spirit.
- **Pass through to Anthropic's own `/v1/chat/completions` compat layer** —
  already reachable via the catch-all, but it does not work with OAuth keys
  (no billing/beta machinery outside `/v1/messages`), has reduced features,
  and gives us no tracking.

### Loopback request headers

Minimal clean set: `authorization: Bearer <client token>`, `content-type:
application/json`, `anthropic-version: 2023-06-01`, `accept` matching stream
mode, `user-agent: smart-proxy-openai-compat/<version>`. The compat user-agent
means `_is_real_claude_code_cli` stays false — no cache-TTL upgrade is
applied, which is correct.

## Request transformation (OpenAI → Anthropic)

`openai_chat_to_anthropic(payload: dict, *, default_max_tokens: int,
auto_cache: bool) -> dict` (pure, raises `OpenAICompatError(status, message)`
on invalid input):

- **model** — passthrough; strip a leading `anthropic/` prefix if present.
- **messages**:
  - `system`/`developer` roles → Anthropic `system` blocks (list of text
    blocks, in order).
  - `user` — string or content-part arrays; `text` parts → text blocks;
    `image_url` parts → Anthropic image blocks (`data:` URLs → base64 source,
    http(s) URLs → url source).
  - `assistant` — text content → text block; `tool_calls` → `tool_use` blocks
    (`function.arguments` JSON string parsed to object; invalid JSON → 400).
  - `tool` — `tool_result` block; consecutive `tool` messages merge into one
    `user` turn (Anthropic requires alternation).
- **tools** `[{type:"function", function:{name, description, parameters}}]` →
  `[{name, description, input_schema}]`.
- **tool_choice**: `"auto"`→`{type:"auto"}`, `"none"`→`{type:"none"}`,
  `"required"`→`{type:"any"}`,
  `{type:"function",function:{name}}`→`{type:"tool",name}`.
- **max_tokens** / **max_completion_tokens** → `max_tokens`; when absent, use
  configured default (`8192`).
- **temperature** — clamped to `[0, 1]` (OpenAI allows 0–2, Anthropic errors
  above 1).
- **top_p** → passthrough; **stop** (string or list) → `stop_sequences`;
  **stream** → passthrough; **user** → `metadata.user_id`.
- **stream_options.include_usage** — remembered by the handler; controls the
  final usage chunk in streaming mode.
- **Prompt-cache injection** (flag `anthropic_proxy_openai_compat_auto_cache`,
  default on):
  the OpenAI protocol has no `cache_control`, so without help this traffic
  gets zero prompt caching. When enabled, set a `cache_control` breakpoint on
  the last system block and on the final content block of the last message.
  TTL comes from `anthropic_proxy_openai_compat_cache_ttl` (`"5m"` |
  `"1h"`, default `"1h"`): `"5m"` → `{type:"ephemeral"}`, `"1h"` →
  `{type:"ephemeral", ttl:"1h"}`. The default matches the proxy's existing
  policy of upgrading interactive Claude Code main-thread traffic to 1h
  (interactive agents pause longer than 5 minutes; note the existing
  `_upgrade_cache_ttl` upgrade never fires here because the loopback
  user-agent is not `claude-cli/*`). Conversations grow by appending, so the
  cached prefix stays stable across turns.
- **Unsupported params**: `n > 1` → HTTP 400. `logprobs`, `presence_penalty`,
  `frequency_penalty`, `seed`, `response_format` → dropped with a warning log
  and an `ignored_params` counter bump (no hard failure — agents send these
  casually).

## Response transformation (Anthropic → OpenAI)

Non-streaming — `anthropic_response_to_openai(data, *, completion_id) -> dict`:

- `id`: `chatcmpl-<anthropic message id>`; `object: "chat.completion"`;
  `created`: server time; `model`: from Anthropic response.
- `choices[0].message`: `role: "assistant"`; `content` = concatenated text
  blocks (or `null` when only tool calls); `tool_use` blocks →
  `tool_calls[{id, type:"function", function:{name, arguments:
  json.dumps(input)}}]`. `thinking` blocks are skipped.
- `finish_reason` mapping: `end_turn`→`stop`, `stop_sequence`→`stop`,
  `max_tokens`→`length`, `tool_use`→`tool_calls`, `refusal`→`content_filter`.
- `usage`: `prompt_tokens = input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens`; `completion_tokens = output_tokens`;
  `total_tokens` = sum; `prompt_tokens_details.cached_tokens =
  cache_read_input_tokens`.

Streaming — incremental converter class fed raw bytes, emitting OpenAI SSE
lines (`data: {...}\n\n`, terminated by `data: [DONE]\n\n`). It parses SSE
event boundaries itself and must be robust to arbitrary chunk fragmentation:

- `message_start` → first chunk with `delta:{role:"assistant", content:""}`;
  captures message id + input usage.
- `content_block_start` (`tool_use`) → chunk with
  `delta.tool_calls[{index, id, type:"function", function:{name,
  arguments:""}}]`. Anthropic block indices map to sequential OpenAI
  tool-call indices (0-based among tool calls).
- `content_block_delta`: `text_delta` → `delta.content`;
  `input_json_delta` → `delta.tool_calls[].function.arguments` fragment;
  `thinking_delta`/`signature_delta` → skipped.
- `message_delta` → captures `stop_reason` and output usage → emits the
  finish chunk (`finish_reason` mapped as above), then a usage chunk when
  `stream_options.include_usage` was requested.
- `message_stop` → `data: [DONE]`.
- `ping` → skipped.
- mid-stream `error` event → `data: {"error": {...}}` line, then `[DONE]`,
  then close (nothing better exists in the OpenAI protocol mid-stream).

## Error mapping

- Outer auth failure → 401 `{"error":{"message":"unauthorized","type":
  "invalid_request_error","code":"invalid_api_key"}}`.
- Inner non-2xx (`{type:"error", error:{type, message}}`) → same HTTP status,
  body `{"error":{"message", "type": <mapped>, "code": <anthropic type>}}`.
  Type mapping: `rate_limit_error`→`rate_limit_error` (429),
  `overloaded_error` (529)→`server_error`, `authentication_error`/
  `permission_error`→`invalid_request_error`, everything else→`server_error`
  or `invalid_request_error` by status class. `retry-after` /
  `retry-after-ms` / `x-should-retry` headers are passed through.
- Loopback transport error → 502 OpenAI-style error body.

## /v1/models

`GET /v1/models` collides with the native Anthropic endpoint of the same
path, and a loopback fetch would recurse into our own route. Both problems
are solved the same way:

- Requests carrying an `anthropic-version` header (the Anthropic SDK always
  sends it; OpenAI SDKs never do) are delegated to the original catch-all
  pass-through handler, injected into the compat module as a parameter —
  native clients see exactly the same upstream response as today.
- All other requests get a static OpenAI-format list
  (`{object:"list", data:[{id, object:"model", created:0,
  owned_by:"anthropic"}]}`) derived from the `claude-*` keys of
  `usage.MODEL_PRICES`. No loopback, no cache needed.

## Tracking

Lightweight, no DB schema changes:

- **Logs**: every compat request logs `[openai-compat] >>> model=... stream=...
  op=<uuid>` and `[openai-compat] <<< status=... op=...` — grep-able marker.
- **Stats**: `app["openai_compat_stats"]` in-memory counters — total
  requests, streaming requests, client errors, upstream errors, per-model
  request counts, accumulated prompt/completion tokens (from converted
  responses), ignored-param counts, `started_at`, `last_request_at`. Exposed
  at `GET /_openai_compat_stats` (JSON), guarded by the same proxy auth
  (`pool.check_auth`). Counters reset on restart — acceptable for
  "know it's happening" tracking.
- **Response marker**: every response from the compat layer carries
  `x-smart-proxy-openai-compat: 1`.
- Usage attribution in the existing tracker/DB continues to work unchanged:
  the loopback forwards the client's own `sp-` token, so per-key and per-token
  usage records land exactly as they do for native traffic.

## Configuration

New `Settings` fields (config.py), passed through `create_app`:

- `anthropic_proxy_openai_compat_enabled` (default `true`) — registers the
  routes; off = proxy behaves exactly as today (`/v1/chat/completions` falls
  back to the catch-all pass-through).
- `anthropic_proxy_openai_compat_default_max_tokens` (default `8192`).
- `anthropic_proxy_openai_compat_auto_cache` (default `true`).
- `anthropic_proxy_openai_compat_cache_ttl` (`"5m"` | `"1h"`, default `"1h"`).

## Non-goals

- `/v1/responses` (Responses API — Codex CLI), embeddings, audio, images
  APIs, `n > 1`, logprobs, enforced `response_format` json_schema. Future
  work if a client needs it.
- Durable (DB) tracking of compat traffic.
- Model name aliasing (`gpt-4o` → claude). Clients are configured with real
  claude model ids.

## Testing

1. **Unit — request/response transforms** (`tests/test_openai_compat_transform.py`):
   messages (system/developer, user string vs parts, images, assistant
   tool_calls, tool-result merging), tools/tool_choice matrix, param
   clamping/defaults, unsupported-param handling, cache injection on/off,
   non-streaming response conversion (text-only, tools-only, mixed, thinking
   skipped, every stop_reason, usage math), error body mapping.
2. **Unit — SSE converter** (`tests/test_openai_compat_sse.py`): fixture
   Anthropic streams → exact expected OpenAI chunk sequences: text-only, tool
   call, multi-block, thinking deltas, mid-stream error, include_usage
   on/off; the same fixtures re-fed byte-by-byte to prove fragmentation
   safety.
3. **Integration — endpoint level** (`tests/test_openai_compat_endpoint.py`):
   app with loopback base pointed at a fake inner server (aiohttp test
   utilities, same style as existing proxy tests): non-stream roundtrip,
   stream roundtrip, 401, upstream 429 with retry-after passthrough, 529
   mapping, stats endpoint counting, `x-smart-proxy-openai-compat` header presence,
   `/v1/models` static list + native-client (`anthropic-version`) delegation, feature flag off → catch-all
   passthrough behavior.
4. **Anthropic-format regression**: the entire existing test suite must pass
   untouched, and the diff to `anthropic_proxy.py` must contain only route
   registration + config plumbing (reviewed at PR time).
5. **Manual smoke** (documented script, run against a locally started proxy
   with a real key): openai Python SDK — plain chat, streamed chat, tool
   call roundtrip; then a checklist item each for opencode/Warp/Cline
   pointed at the proxy.

## Rollout

Feature flag defaults on; deploy as usual (`deploy.sh`); observe
`[openai-compat]` log lines and `/_openai_compat_stats` for the first days.
Kill switch: set `anthropic_proxy_openai_compat_enabled=false` and restart.
