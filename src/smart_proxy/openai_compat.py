"""OpenAI-compatible protocol layer for the Anthropic proxy.

Translates OpenAI Chat Completions requests into Anthropic /v1/messages
requests, dispatches them back through the proxy itself over localhost
(inheriting key pool / OAuth / retry machinery untouched), and translates
responses back. See docs/superpowers/specs/2026-07-03-openai-compat-endpoint-design.md.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from aiohttp import web

from smart_proxy.usage import MODEL_PRICES

logger = logging.getLogger("openai_compat")

# Matches the proxy's own upstream timeout profile (see anthropic_proxy._UPSTREAM_TIMEOUT).
# Not imported from anthropic_proxy to avoid a circular import.
_LOOPBACK_TIMEOUT = httpx.Timeout(600.0, connect=30.0)

_IGNORED_PARAMS = (
    "logprobs",
    "top_logprobs",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "parallel_tool_calls",
)


class OpenAICompatError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        err_type: str = "invalid_request_error",
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.err_type = err_type
        self.code = code


def _text_blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return [
            {"type": "text", "text": part.get("text", "")}
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        ]
    return []


def _image_block(part: dict) -> dict:
    url = str(((part.get("image_url") or {}).get("url")) or "")
    if url.startswith("data:"):
        try:
            header, b64 = url.split(",", 1)
        except ValueError:
            raise OpenAICompatError(400, "malformed data: image URL")
        media = header.split(";")[0].removeprefix("data:") or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": b64},
        }
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise OpenAICompatError(400, "unsupported image_url; use data: or http(s) URLs")


def _user_content(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        blocks: list[dict] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                blocks.append(_image_block(part))
        return blocks
    return []


def _assistant_content(msg: dict) -> list[dict]:
    blocks = _text_blocks(msg.get("content"))
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        args_raw = fn.get("arguments")
        if isinstance(args_raw, str) and args_raw.strip():
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                raise OpenAICompatError(
                    400,
                    f"tool call arguments are not valid JSON for {fn.get('name')!r}",
                )
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        if not isinstance(args, dict):
            args = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(fn.get("name") or ""),
                "input": args,
            }
        )
    return blocks


def _tool_result_content(content: object) -> str | list:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = _text_blocks(content)
        return blocks if blocks else ""
    return "" if content is None else str(content)


def _convert_tool(tool: object) -> dict:
    if not isinstance(tool, dict) or tool.get("type") != "function":
        raise OpenAICompatError(400, "only tools of type 'function' are supported")
    fn = tool.get("function") or {}
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise OpenAICompatError(400, "tool function name is required")
    out: dict = {
        "name": name,
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }
    if fn.get("description"):
        out["description"] = fn["description"]
    return out


def _convert_tool_choice(tc: object) -> dict | None:
    if tc is None:
        return None
    if tc == "auto":
        return {"type": "auto"}
    if tc == "none":
        return {"type": "none"}
    if tc == "required":
        return {"type": "any"}
    if isinstance(tc, dict) and tc.get("type") == "function":
        name = str(((tc.get("function") or {}).get("name")) or "")
        if name:
            return {"type": "tool", "name": name}
    raise OpenAICompatError(400, f"unsupported tool_choice: {tc!r}")


_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

_ERROR_TYPE_MAP = {
    "rate_limit_error": "rate_limit_error",
    "authentication_error": "invalid_request_error",
    "permission_error": "invalid_request_error",
    "invalid_request_error": "invalid_request_error",
    "not_found_error": "invalid_request_error",
    "request_too_large": "invalid_request_error",
    "overloaded_error": "server_error",
    "api_error": "server_error",
}


def map_finish_reason(stop_reason: str | None) -> str | None:
    if not stop_reason:
        return None
    return _FINISH_REASON_MAP.get(stop_reason, "stop")


def _usage_to_openai(usage: dict | None) -> dict:
    u = usage or {}
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cache_read = int(u.get("cache_read_input_tokens") or 0)
    cache_create = int(u.get("cache_creation_input_tokens") or 0)
    prompt = inp + cache_read + cache_create
    return {
        "prompt_tokens": prompt,
        "completion_tokens": out,
        "total_tokens": prompt + out,
        "prompt_tokens_details": {"cached_tokens": cache_read},
    }


def anthropic_response_to_openai(data: dict, *, created: int) -> dict:
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
        # thinking / redacted_thinking blocks are dropped: no OpenAI equivalent

    message: dict = {
        "role": "assistant",
        "content": "".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{data.get('id', '')}",
        "object": "chat.completion",
        "created": created,
        "model": data.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": map_finish_reason(data.get("stop_reason")) or "stop",
            }
        ],
        "usage": _usage_to_openai(data.get("usage")),
    }


def anthropic_error_to_openai(status: int, body: bytes) -> dict:
    code = ""
    message = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            err = data.get("error") or {}
            code = str(err.get("type") or "")
            message = str(err.get("message") or "")
    except (json.JSONDecodeError, ValueError):
        pass
    if not message:
        message = body.decode("utf-8", errors="replace")[:300] or f"upstream error (HTTP {status})"
    err_type = _ERROR_TYPE_MAP.get(code) or (
        "server_error" if status >= 500 else "invalid_request_error"
    )
    return {"error": {"message": message, "type": err_type, "code": code or None}}


def _inject_cache_control(body: dict, ttl: str) -> None:
    """Mark the stable prefix cacheable: last system block + last content block.

    The OpenAI protocol has no cache_control, so compat traffic would get zero
    prompt caching without this. Conversations grow by appending, so these two
    breakpoints keep the prefix cache-hot across turns. The proxy's own
    _upgrade_cache_ttl never fires for this traffic (loopback UA is not
    claude-cli/*), so the TTL must be set here.
    """
    cache_control = (
        {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}
    )
    system = body.get("system")
    if isinstance(system, list) and system:
        system[-1] = {**system[-1], "cache_control": cache_control}
    msgs = body.get("messages") or []
    if msgs:
        content = msgs[-1].get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1] = {**content[-1], "cache_control": cache_control}


def openai_chat_to_anthropic(
    payload: dict,
    *,
    default_max_tokens: int = 8192,
    auto_cache: bool = True,
    cache_ttl: str = "1h",
) -> tuple[dict, list[str]]:
    """Convert an OpenAI Chat Completions body to an Anthropic /v1/messages body.

    Returns (anthropic_body, ignored_param_names). Raises OpenAICompatError
    with an HTTP status for invalid input.
    """
    if not isinstance(payload, dict):
        raise OpenAICompatError(400, "request body must be a JSON object")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise OpenAICompatError(400, "'model' is required")
    model = model.strip().removeprefix("anthropic/")

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise OpenAICompatError(400, "'messages' must be a non-empty array")

    n = payload.get("n")
    if isinstance(n, int) and n > 1:
        raise OpenAICompatError(400, "'n' > 1 is not supported")

    ignored = [
        p for p in _IGNORED_PARAMS if p in payload and payload[p] not in (None, False)
    ]

    system_blocks: list[dict] = []
    out_messages: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            raise OpenAICompatError(400, "each message must be an object")
        role = msg.get("role")
        if role in ("system", "developer"):
            system_blocks.extend(_text_blocks(msg.get("content")))
        elif role == "user":
            blocks = _user_content(msg.get("content"))
            if blocks:
                out_messages.append({"role": "user", "content": blocks})
        elif role == "assistant":
            blocks = _assistant_content(msg)
            if blocks:
                out_messages.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or ""),
                "content": _tool_result_content(msg.get("content")),
            }
            if (
                out_messages
                and out_messages[-1]["role"] == "user"
                and isinstance(out_messages[-1]["content"], list)
                and all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in out_messages[-1]["content"]
                )
            ):
                out_messages[-1]["content"].append(block)
            else:
                out_messages.append({"role": "user", "content": [block]})
        else:
            raise OpenAICompatError(400, f"unsupported message role: {role!r}")

    body: dict = {"model": model, "messages": out_messages}

    raw_max = payload.get("max_completion_tokens") or payload.get("max_tokens")
    try:
        body["max_tokens"] = int(raw_max) if raw_max else int(default_max_tokens)
    except (TypeError, ValueError):
        raise OpenAICompatError(400, "'max_tokens' must be an integer")

    if system_blocks:
        body["system"] = system_blocks

    temp = payload.get("temperature")
    if isinstance(temp, (int, float)) and not isinstance(temp, bool):
        body["temperature"] = max(0.0, min(1.0, float(temp)))

    top_p = payload.get("top_p")
    if isinstance(top_p, (int, float)) and not isinstance(top_p, bool):
        body["top_p"] = float(top_p)

    stop = payload.get("stop")
    if isinstance(stop, str) and stop:
        body["stop_sequences"] = [stop]
    elif isinstance(stop, list):
        seqs = [str(s) for s in stop if s]
        if seqs:
            body["stop_sequences"] = seqs

    if payload.get("stream"):
        body["stream"] = True

    user = payload.get("user")
    if isinstance(user, str) and user:
        body["metadata"] = {"user_id": user}

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        body["tools"] = [_convert_tool(t) for t in tools]
        tc = _convert_tool_choice(payload.get("tool_choice"))
        if tc is not None:
            body["tool_choice"] = tc

    if auto_cache:
        _inject_cache_control(body, cache_ttl)

    return body, ignored


class AnthropicToOpenAIStream:
    """Incremental Anthropic SSE → OpenAI chat.completion.chunk SSE converter.

    Feed raw upstream bytes in arbitrary fragments; each complete Anthropic
    event is converted as soon as its terminating blank line arrives.
    """

    def __init__(self, *, created: int, include_usage: bool = False) -> None:
        self._created = created
        self._include_usage = include_usage
        self._buf = b""
        self.completion_id = "chatcmpl-unknown"
        self.model = ""
        self.usage: dict | None = None
        self._input_usage: dict = {}
        self._tool_index_by_block: dict[int, int] = {}
        self._next_tool_index = 0
        self._finish_reason: str | None = None
        self._done = False

    def feed(self, chunk: bytes) -> bytes:
        self._buf = (self._buf + chunk).replace(b"\r\n", b"\n")
        out: list[bytes] = []
        while b"\n\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            evt = self._parse_event(raw)
            if evt is not None:
                out.append(self._handle(evt))
        return b"".join(o for o in out if o)

    def finish(self) -> bytes:
        """Close out a stream that ended without message_stop."""
        return self._final_bytes()

    @staticmethod
    def _parse_event(raw: bytes) -> dict | None:
        data_lines = [
            line[len(b"data:") :].strip()
            for line in raw.split(b"\n")
            if line.startswith(b"data:")
        ]
        if not data_lines:
            return None
        try:
            evt = json.loads(b"\n".join(data_lines))
        except (json.JSONDecodeError, ValueError):
            return None
        return evt if isinstance(evt, dict) else None

    def _sse(self, payload: dict) -> bytes:
        return (
            b"data: "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n\n"
        )

    def _chunk(self, delta: dict, finish_reason: str | None = None) -> dict:
        return {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def _handle(self, evt: dict) -> bytes:
        if self._done:
            return b""
        etype = evt.get("type")
        if etype == "message_start":
            msg = evt.get("message") or {}
            self.completion_id = f"chatcmpl-{msg.get('id', '')}"
            self.model = msg.get("model", "")
            self._input_usage = msg.get("usage") or {}
            return self._sse(self._chunk({"role": "assistant", "content": ""}))
        if etype == "content_block_start":
            block = evt.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = self._next_tool_index
                self._next_tool_index += 1
                self._tool_index_by_block[evt.get("index")] = idx
                return self._sse(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": block.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name", ""),
                                        "arguments": "",
                                    },
                                }
                            ]
                        }
                    )
                )
            return b""
        if etype == "content_block_delta":
            delta = evt.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return self._sse(self._chunk({"content": delta.get("text", "")}))
            if dtype == "input_json_delta":
                idx = self._tool_index_by_block.get(evt.get("index"))
                if idx is None:
                    return b""
                return self._sse(
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "function": {
                                        "arguments": delta.get("partial_json", "")
                                    },
                                }
                            ]
                        }
                    )
                )
            return b""  # thinking_delta, signature_delta
        if etype == "message_delta":
            d = evt.get("delta") or {}
            if d.get("stop_reason"):
                self._finish_reason = map_finish_reason(d["stop_reason"])
            merged = dict(self._input_usage)
            merged.update(evt.get("usage") or {})
            self.usage = _usage_to_openai(merged)
            return b""
        if etype == "message_stop":
            return self._final_bytes()
        if etype == "error":
            self._done = True
            err = anthropic_error_to_openai(
                500, json.dumps(evt, ensure_ascii=False).encode()
            )
            return self._sse(err) + b"data: [DONE]\n\n"
        return b""  # ping and future event types

    def _final_bytes(self) -> bytes:
        if self._done:
            return b""
        self._done = True
        parts = [self._sse(self._chunk({}, finish_reason=self._finish_reason or "stop"))]
        if self._include_usage and self.usage is not None:
            parts.append(
                self._sse(
                    {
                        "id": self.completion_id,
                        "object": "chat.completion.chunk",
                        "created": self._created,
                        "model": self.model,
                        "choices": [],
                        "usage": self.usage,
                    }
                )
            )
        parts.append(b"data: [DONE]\n\n")
        return b"".join(parts)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class OpenAICompatStats:
    started_at: str = field(default_factory=_utc_now_iso)
    last_request_at: str | None = None
    requests: int = 0
    streaming_requests: int = 0
    client_errors: int = 0
    upstream_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    ignored_params: dict[str, int] = field(default_factory=dict)

    def record_request(self, *, model: str, stream: bool) -> None:
        self.requests += 1
        if stream:
            self.streaming_requests += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        self.last_request_at = _utc_now_iso()

    def record_client_error(self) -> None:
        self.client_errors += 1

    def record_upstream_error(self) -> None:
        self.upstream_errors += 1

    def record_usage(self, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)

    def record_ignored(self, params: list[str]) -> None:
        for p in params:
            self.ignored_params[p] = self.ignored_params.get(p, 0) + 1

    def snapshot(self) -> dict:
        return {
            "started_at": self.started_at,
            "last_request_at": self.last_request_at,
            "requests": self.requests,
            "streaming_requests": self.streaming_requests,
            "client_errors": self.client_errors,
            "upstream_errors": self.upstream_errors,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "by_model": dict(self.by_model),
            "ignored_params": dict(self.ignored_params),
        }


_MARKER_HEADER = "x-smart-proxy-openai-compat"
_ANTHROPIC_VERSION = "2023-06-01"
_COMPAT_USER_AGENT = "smart-proxy-openai-compat/1.0"
_PASSTHROUGH_ERROR_HEADERS = ("retry-after", "retry-after-ms", "x-should-retry")


def _bearer_token(request: web.Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _json_response(
    status: int, payload: dict, *, headers: dict[str, str] | None = None
) -> web.Response:
    resp = web.Response(
        status=status,
        body=json.dumps(payload, ensure_ascii=False).encode(),
        content_type="application/json",
    )
    resp.headers[_MARKER_HEADER] = "1"
    for name, value in (headers or {}).items():
        resp.headers[name] = value
    return resp


def _error_response(
    status: int,
    message: str,
    *,
    err_type: str = "invalid_request_error",
    code: str | None = None,
) -> web.Response:
    return _json_response(
        status, {"error": {"message": message, "type": err_type, "code": code}}
    )


async def _chat_completions_handler(request: web.Request) -> web.StreamResponse:
    stats: OpenAICompatStats = request.app["openai_compat_stats"]
    pool = request.app["anthropic_pool"]
    op_id = uuid4().hex[:12]

    token = _bearer_token(request)
    if not pool.check_auth(token):
        stats.record_client_error()
        logger.info("[openai-compat] <<< status=401 op=%s", op_id)
        return _error_response(401, "unauthorized", code="invalid_api_key")

    try:
        payload = json.loads(await request.read())
    except (json.JSONDecodeError, ValueError):
        stats.record_client_error()
        logger.info("[openai-compat] <<< status=400 op=%s", op_id)
        return _error_response(400, "request body is not valid JSON")

    try:
        body, ignored = openai_chat_to_anthropic(
            payload,
            default_max_tokens=request.app["openai_compat_default_max_tokens"],
            auto_cache=request.app["openai_compat_auto_cache"],
            cache_ttl=request.app["openai_compat_cache_ttl"],
        )
    except OpenAICompatError as exc:
        stats.record_client_error()
        logger.info("[openai-compat] <<< status=%d op=%s", exc.status, op_id)
        return _error_response(
            exc.status, exc.message, err_type=exc.err_type, code=exc.code
        )

    stream = bool(body.get("stream"))
    include_usage = bool(
        isinstance(payload.get("stream_options"), dict)
        and payload["stream_options"].get("include_usage")
    )
    stats.record_request(model=body["model"], stream=stream)
    stats.record_ignored(ignored)
    if ignored:
        logger.warning("[openai-compat] ignored params %s op=%s", ignored, op_id)
    logger.info(
        "[openai-compat] >>> model=%s stream=%s op=%s", body["model"], stream, op_id
    )

    client: httpx.AsyncClient = request.app["openai_compat_http_client"]
    base = request.app["openai_compat_loopback_base"]
    loop_headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "anthropic-version": _ANTHROPIC_VERSION,
        "accept": "text/event-stream" if stream else "application/json",
        "user-agent": _COMPAT_USER_AGENT,
        # Tags the loopback so the native handler attributes usage to the
        # OpenAI-compat layer (dropped before the request leaves for upstream).
        _MARKER_HEADER: "1",
    }
    try:
        req = client.build_request(
            "POST",
            f"{base}/v1/messages",
            headers=loop_headers,
            content=json.dumps(body).encode(),
        )
        r = await client.send(req, stream=True)
    except httpx.TransportError as exc:
        stats.record_upstream_error()
        logger.error("[openai-compat] loopback transport error op=%s: %s", op_id, exc)
        logger.info("[openai-compat] <<< status=502 op=%s", op_id)
        return _error_response(
            502, f"upstream connection error: {exc}", err_type="server_error"
        )

    if r.status_code != 200:
        try:
            raw = await r.aread()
        finally:
            await r.aclose()
        stats.record_upstream_error()
        err_body = anthropic_error_to_openai(r.status_code, raw)
        passthrough = {
            name: value
            for name, value in r.headers.items()
            if name.lower() in _PASSTHROUGH_ERROR_HEADERS
        }
        logger.info("[openai-compat] <<< status=%d op=%s", r.status_code, op_id)
        return _json_response(r.status_code, err_body, headers=passthrough)

    if not stream:
        try:
            raw = await r.aread()
        finally:
            await r.aclose()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            stats.record_upstream_error()
            logger.info("[openai-compat] <<< status=502 op=%s", op_id)
            return _error_response(
                502, "upstream returned a non-JSON response", err_type="server_error"
            )
        out = anthropic_response_to_openai(data, created=int(time.time()))
        usage = out.get("usage") or {}
        stats.record_usage(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        logger.info("[openai-compat] <<< status=200 op=%s", op_id)
        return _json_response(200, out)

    conv = AnthropicToOpenAIStream(created=int(time.time()), include_usage=include_usage)
    resp = web.StreamResponse(status=200)
    resp.headers["content-type"] = "text/event-stream; charset=utf-8"
    resp.headers["cache-control"] = "no-cache"
    resp.headers[_MARKER_HEADER] = "1"
    try:
        await resp.prepare(request)
        async for chunk in r.aiter_bytes():
            out_bytes = conv.feed(chunk)
            if out_bytes:
                await resp.write(out_bytes)
        tail = conv.finish()
        if tail:
            await resp.write(tail)
    except httpx.TransportError as exc:
        stats.record_upstream_error()
        logger.warning("[openai-compat] mid-stream read error op=%s: %s", op_id, exc)
        tail = conv.finish()
        if tail:
            try:
                await resp.write(tail)
            except (ConnectionResetError, ConnectionError):
                pass
    except (ConnectionResetError, ConnectionError) as exc:
        logger.debug("[openai-compat] client disconnected op=%s: %s", op_id, exc)
    finally:
        await r.aclose()
    try:
        await resp.write_eof()
    except (ConnectionResetError, ConnectionError) as exc:
        logger.debug("[openai-compat] write_eof ignored op=%s: %s", op_id, exc)
    if conv.usage:
        stats.record_usage(
            prompt_tokens=conv.usage.get("prompt_tokens", 0),
            completion_tokens=conv.usage.get("completion_tokens", 0),
        )
    logger.info("[openai-compat] <<< status=200 stream-done op=%s", op_id)
    return resp


async def _stats_handler(request: web.Request) -> web.Response:
    pool = request.app["anthropic_pool"]
    if not pool.check_auth(_bearer_token(request)):
        return _error_response(401, "unauthorized", code="invalid_api_key")
    stats: OpenAICompatStats = request.app["openai_compat_stats"]
    return _json_response(200, stats.snapshot())


def _fallback_models() -> list[dict]:
    ids = sorted(m for m in MODEL_PRICES if m.startswith("claude-"))
    return [
        {"id": mid, "object": "model", "created": 0, "owned_by": "anthropic"}
        for mid in ids
    ]


async def _models_handler(request: web.Request) -> web.StreamResponse:
    # Native Anthropic clients (the SDK always sends anthropic-version) get
    # the untouched pass-through response — this route must not change their
    # behavior. It also prevents any loopback recursion into this route.
    if "anthropic-version" in request.headers:
        passthrough = request.app.get("openai_compat_models_passthrough")
        if passthrough is not None:
            return await passthrough(request)

    pool = request.app["anthropic_pool"]
    if not pool.check_auth(_bearer_token(request)):
        return _error_response(401, "unauthorized", code="invalid_api_key")
    return _json_response(200, {"object": "list", "data": _fallback_models()})


async def _openai_compat_on_startup(app: web.Application) -> None:
    app["openai_compat_http_client"] = httpx.AsyncClient(
        timeout=_LOOPBACK_TIMEOUT,
        limits=httpx.Limits(max_connections=None),
    )


async def _openai_compat_on_cleanup(app: web.Application) -> None:
    client = app.get("openai_compat_http_client")
    if client is not None:
        await client.aclose()


def setup_openai_compat(
    app: web.Application,
    *,
    default_max_tokens: int = 8192,
    auto_cache: bool = True,
    cache_ttl: str = "1h",
    models_passthrough=None,
) -> None:
    """Register OpenAI-compatible routes. Must be called BEFORE the catch-all route."""
    if cache_ttl not in ("5m", "1h"):
        logger.warning(
            "[openai-compat] unknown cache_ttl %r, falling back to 5m behavior", cache_ttl
        )
    app["openai_compat_stats"] = OpenAICompatStats()
    app["openai_compat_default_max_tokens"] = default_max_tokens
    app["openai_compat_auto_cache"] = auto_cache
    app["openai_compat_cache_ttl"] = cache_ttl
    app["openai_compat_models_passthrough"] = models_passthrough
    app.router.add_post("/v1/chat/completions", _chat_completions_handler)
    app.router.add_get("/v1/models", _models_handler)
    app.router.add_get("/_openai_compat_stats", _stats_handler)
    app.on_startup.append(_openai_compat_on_startup)
    app.on_cleanup.append(_openai_compat_on_cleanup)
