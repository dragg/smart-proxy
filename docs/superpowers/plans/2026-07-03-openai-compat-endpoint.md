# OpenAI-Compatible Endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve OpenAI Chat Completions clients (Warp, Cline, aider, opencode) from the Anthropic proxy's existing key pool via a loopback transformer, with zero behavior change to the native Anthropic pass-through path and lightweight tracking of transformer traffic.

**Architecture:** New pure-transform module `openai_compat.py` registers `POST /v1/chat/completions`, `GET /v1/models`, `GET /_openai_compat_stats` ahead of the catch-all. The chat handler converts OpenAI→Anthropic, POSTs to the proxy's own `/v1/messages` over localhost (inheriting key pool, OAuth, retries, SSE pre-commit buffering), then converts the JSON/SSE response back to OpenAI format. Spec: `docs/superpowers/specs/2026-07-03-openai-compat-endpoint-design.md`.

**Tech Stack:** Python, aiohttp (server), httpx (loopback client), unittest, `uv run pytest`.

## Global Constraints

- `_proxy_handler` and every function it calls in `src/smart_proxy/anthropic_proxy.py` must not change. The only allowed diff there: new `create_app` kwargs + a conditional `setup_openai_compat(...)` call, and `main()` passing new settings.
- No new runtime dependencies (litellm must NOT be reintroduced — `tests/test_litellm_removal.py` guards this). The `openai` package is used only in the manual smoke script via `uv run --with openai`.
- `openai_compat.py` must NOT import `anthropic_proxy` (would be a circular import). Anything it needs from the app (loopback base URL, pool, http client, passthrough handler) is read from `request.app[...]` or injected via `setup_openai_compat` parameters.
- Tests are unittest-style with the repo's `sys.path` shim header; run with `uv run pytest tests/<file> -q`.
- New config fields follow the existing naming prefix: `anthropic_proxy_openai_compat_*`.
- Every response produced by the compat layer carries header `x-smart-proxy-openai-compat: 1`; every compat log line starts with `[openai-compat]`.

---

### Task 1: Module skeleton + basic request transform (roles, params)

**Files:**
- Create: `src/smart_proxy/openai_compat.py`
- Test: `tests/test_openai_compat_transform.py`

**Interfaces:**
- Produces: `OpenAICompatError(status: int, message: str, *, err_type: str = "invalid_request_error", code: str | None = None)` with attributes `.status`, `.message`, `.err_type`, `.code`.
- Produces: `openai_chat_to_anthropic(payload: dict, *, default_max_tokens: int = 8192, auto_cache: bool = True) -> tuple[dict, list[str]]` returning `(anthropic_body, ignored_param_names)`. Tool/image/cache support arrives in Task 2; this task covers roles system/developer/user/assistant(text-only), params, and unsupported-param policy.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_openai_compat_transform.py`:

```python
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.openai_compat import OpenAICompatError, openai_chat_to_anthropic


def _convert(payload, **kw):
    kw.setdefault("default_max_tokens", 8192)
    kw.setdefault("auto_cache", False)
    return openai_chat_to_anthropic(payload, **kw)


class RequestTransformBasicsTests(unittest.TestCase):
    def test_minimal_user_message(self):
        body, ignored = _convert(
            {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(body["model"], "claude-sonnet-5")
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(
            body["messages"],
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        )
        self.assertEqual(ignored, [])
        self.assertNotIn("system", body)
        self.assertNotIn("stream", body)

    def test_model_anthropic_prefix_stripped(self):
        body, _ = _convert(
            {"model": "anthropic/claude-opus-4-8", "messages": [{"role": "user", "content": "x"}]}
        )
        self.assertEqual(body["model"], "claude-opus-4-8")

    def test_system_and_developer_roles_become_system_blocks(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "developer", "content": "and kind"},
                    {"role": "user", "content": "hi"},
                ],
            }
        )
        self.assertEqual(
            body["system"],
            [
                {"type": "text", "text": "be brief"},
                {"type": "text", "text": "and kind"},
            ],
        )
        self.assertEqual(len(body["messages"]), 1)

    def test_assistant_text_message(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"},
                    {"role": "user", "content": "q2"},
                ],
            }
        )
        self.assertEqual(
            body["messages"][1],
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        )

    def test_empty_assistant_message_skipped(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": "q"},
                    {"role": "assistant", "content": ""},
                    {"role": "user", "content": "q2"},
                ],
            }
        )
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "user"])

    def test_max_completion_tokens_wins_over_default(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x"}],
                "max_completion_tokens": 555,
            }
        )
        self.assertEqual(body["max_tokens"], 555)

    def test_max_tokens_passthrough(self):
        body, _ = _convert(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "max_tokens": 42}
        )
        self.assertEqual(body["max_tokens"], 42)

    def test_temperature_clamped_to_anthropic_range(self):
        body, _ = _convert(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "temperature": 1.7}
        )
        self.assertEqual(body["temperature"], 1.0)

    def test_stop_string_and_list(self):
        body, _ = _convert(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "stop": "END"}
        )
        self.assertEqual(body["stop_sequences"], ["END"])
        body, _ = _convert(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "stop": ["a", "b"]}
        )
        self.assertEqual(body["stop_sequences"], ["a", "b"])

    def test_stream_user_top_p_passthrough(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x"}],
                "stream": True,
                "top_p": 0.9,
                "user": "u-1",
            }
        )
        self.assertIs(body["stream"], True)
        self.assertEqual(body["top_p"], 0.9)
        self.assertEqual(body["metadata"], {"user_id": "u-1"})

    def test_unsupported_params_ignored_and_reported(self):
        _, ignored = _convert(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x"}],
                "seed": 7,
                "presence_penalty": 0.5,
                "response_format": {"type": "json_object"},
            }
        )
        self.assertEqual(sorted(ignored), ["presence_penalty", "response_format", "seed"])

    def test_n_greater_than_one_rejected(self):
        with self.assertRaises(OpenAICompatError) as ctx:
            _convert({"model": "m", "messages": [{"role": "user", "content": "x"}], "n": 3})
        self.assertEqual(ctx.exception.status, 400)

    def test_missing_model_rejected(self):
        with self.assertRaises(OpenAICompatError):
            _convert({"messages": [{"role": "user", "content": "x"}]})

    def test_empty_messages_rejected(self):
        with self.assertRaises(OpenAICompatError):
            _convert({"model": "m", "messages": []})

    def test_unknown_role_rejected(self):
        with self.assertRaises(OpenAICompatError):
            _convert({"model": "m", "messages": [{"role": "robot", "content": "x"}]})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'smart_proxy.openai_compat'`

- [ ] **Step 3: Write minimal implementation**

Create `src/smart_proxy/openai_compat.py`:

```python
"""OpenAI-compatible protocol layer for the Anthropic proxy.

Translates OpenAI Chat Completions requests into Anthropic /v1/messages
requests, dispatches them back through the proxy itself over localhost
(inheriting key pool / OAuth / retry machinery untouched), and translates
responses back. See docs/superpowers/specs/2026-07-03-openai-compat-endpoint-design.md.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("openai_compat")

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


def _user_content(content: object) -> list[dict]:
    # Image parts are added in Task 2.
    return _text_blocks(content)


def _assistant_content(msg: dict) -> list[dict]:
    # tool_calls conversion is added in Task 2.
    return _text_blocks(msg.get("content"))


def openai_chat_to_anthropic(
    payload: dict,
    *,
    default_max_tokens: int = 8192,
    auto_cache: bool = True,
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

    return body, ignored
```

Note: `json` is imported now because Task 2 uses it in this module; keep it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/openai_compat.py tests/test_openai_compat_transform.py
git commit -m "feat(openai-compat): request transform basics (roles, params, validation)"
```

---

### Task 2: Request transform — tools, tool messages, images, cache injection

**Files:**
- Modify: `src/smart_proxy/openai_compat.py`
- Test: `tests/test_openai_compat_transform.py` (append)

**Interfaces:**
- Consumes: `openai_chat_to_anthropic`, `OpenAICompatError` from Task 1.
- Produces (same function, extended): OpenAI `tools`/`tool_choice`/`tool_calls`/`tool` messages/`image_url` parts handled; new keyword `cache_ttl: str = "1h"`; `auto_cache=True` adds a `cache_control` breakpoint to the last system block and the final content block of the last message — `{"type": "ephemeral", "ttl": "1h"}` when `cache_ttl == "1h"` (default), plain `{"type": "ephemeral"}` when `"5m"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_openai_compat_transform.py`:

```python
class RequestTransformToolsTests(unittest.TestCase):
    def test_tools_converted_to_anthropic_schema(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get weather",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        },
                    }
                ],
            }
        )
        self.assertEqual(
            body["tools"],
            [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
        )

    def test_tool_without_parameters_gets_empty_schema(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "function", "function": {"name": "ping"}}],
            }
        )
        self.assertEqual(
            body["tools"][0]["input_schema"], {"type": "object", "properties": {}}
        )

    def test_tool_choice_mapping(self):
        base = {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
        cases = [
            ("auto", {"type": "auto"}),
            ("none", {"type": "none"}),
            ("required", {"type": "any"}),
            ({"type": "function", "function": {"name": "f"}}, {"type": "tool", "name": "f"}),
        ]
        for tc, expected in cases:
            body, _ = _convert({**base, "tool_choice": tc})
            self.assertEqual(body["tool_choice"], expected, tc)
        body, _ = _convert(base)
        self.assertNotIn("tool_choice", body)

    def test_assistant_tool_calls_become_tool_use_blocks(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": "weather?"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{\"city\": \"Paris\"}",
                                },
                            }
                        ],
                    },
                ],
            }
        )
        self.assertEqual(
            body["messages"][1],
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    }
                ],
            },
        )

    def test_invalid_tool_call_arguments_rejected(self):
        with self.assertRaises(OpenAICompatError):
            _convert(
                {
                    "model": "m",
                    "messages": [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "type": "function",
                                    "function": {"name": "f", "arguments": "{broken"},
                                }
                            ],
                        }
                    ],
                }
            )

    def test_tool_messages_become_tool_result_user_turn_and_merge(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {"role": "user", "content": "q"},
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                            {"id": "c2", "type": "function", "function": {"name": "g", "arguments": "{}"}},
                        ],
                    },
                    {"role": "tool", "tool_call_id": "c1", "content": "r1"},
                    {"role": "tool", "tool_call_id": "c2", "content": "r2"},
                ],
            }
        )
        self.assertEqual(len(body["messages"]), 3)
        self.assertEqual(
            body["messages"][2],
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "r1"},
                    {"type": "tool_result", "tool_use_id": "c2", "content": "r2"},
                ],
            },
        )

    def test_image_data_url_becomes_base64_source(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is this"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aGk="},
                            },
                        ],
                    }
                ],
            }
        )
        self.assertEqual(
            body["messages"][0]["content"][1],
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
            },
        )

    def test_image_http_url_becomes_url_source(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}
                        ],
                    }
                ],
            }
        )
        self.assertEqual(
            body["messages"][0]["content"][0]["source"],
            {"type": "url", "url": "https://x.test/a.png"},
        )


class CacheInjectionTests(unittest.TestCase):
    def test_auto_cache_marks_system_and_last_message_with_1h_default(self):
        body, _ = openai_chat_to_anthropic(
            {
                "model": "m",
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                    {"role": "user", "content": "three"},
                ],
            },
            default_max_tokens=8192,
            auto_cache=True,
        )
        expected = {"type": "ephemeral", "ttl": "1h"}
        self.assertEqual(body["system"][-1]["cache_control"], expected)
        last_block = body["messages"][-1]["content"][-1]
        self.assertEqual(last_block["cache_control"], expected)
        first_block = body["messages"][0]["content"][-1]
        self.assertNotIn("cache_control", first_block)

    def test_auto_cache_5m_ttl_uses_plain_ephemeral(self):
        body, _ = openai_chat_to_anthropic(
            {
                "model": "m",
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "one"},
                ],
            },
            default_max_tokens=8192,
            auto_cache=True,
            cache_ttl="5m",
        )
        self.assertEqual(body["system"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(
            body["messages"][-1]["content"][-1]["cache_control"],
            {"type": "ephemeral"},
        )

    def test_auto_cache_off_adds_nothing(self):
        body, _ = _convert(
            {
                "model": "m",
                "messages": [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "one"},
                ],
            }
        )
        self.assertNotIn("cache_control", body["system"][-1])
        self.assertNotIn("cache_control", body["messages"][-1]["content"][-1])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: new tests FAIL (tools not in body / KeyError / no OpenAICompatError raised); Task 1 tests still PASS.

- [ ] **Step 3: Implement**

In `src/smart_proxy/openai_compat.py`, replace the Task 1 stubs `_user_content` and `_assistant_content`, and add the helpers below (place them after `_text_blocks`):

```python
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
```

Change the `openai_chat_to_anthropic` signature to accept the TTL:

```python
def openai_chat_to_anthropic(
    payload: dict,
    *,
    default_max_tokens: int = 8192,
    auto_cache: bool = True,
    cache_ttl: str = "1h",
) -> tuple[dict, list[str]]:
```

In `openai_chat_to_anthropic`, extend the message loop with the `tool` role branch (insert before the `else:` that raises):

```python
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
```

And at the end of `openai_chat_to_anthropic`, before `return body, ignored`:

```python
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        body["tools"] = [_convert_tool(t) for t in tools]
        tc = _convert_tool_choice(payload.get("tool_choice"))
        if tc is not None:
            body["tool_choice"] = tc

    if auto_cache:
        _inject_cache_control(body, cache_ttl)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/openai_compat.py tests/test_openai_compat_transform.py
git commit -m "feat(openai-compat): tools, tool results, images, prompt-cache injection"
```

---

### Task 3: Non-streaming response transform + error mapping

**Files:**
- Modify: `src/smart_proxy/openai_compat.py`
- Test: `tests/test_openai_compat_transform.py` (append)

**Interfaces:**
- Produces: `map_finish_reason(stop_reason: str | None) -> str | None` — `None`→`None`, `end_turn`/`stop_sequence`→`"stop"`, `max_tokens`→`"length"`, `tool_use`→`"tool_calls"`, `refusal`→`"content_filter"`, unknown→`"stop"`.
- Produces: `anthropic_response_to_openai(data: dict, *, created: int) -> dict` — full OpenAI chat.completion object.
- Produces: `anthropic_error_to_openai(status: int, body: bytes) -> dict` — OpenAI error body `{"error": {"message", "type", "code"}}`.
- Produces (internal, reused by Task 4): `_usage_to_openai(usage: dict | None) -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_openai_compat_transform.py`:

```python
from smart_proxy.openai_compat import (  # noqa: E402  (keep with top imports if preferred)
    anthropic_error_to_openai,
    anthropic_response_to_openai,
    map_finish_reason,
)


class ResponseTransformTests(unittest.TestCase):
    def test_text_response(self):
        out = anthropic_response_to_openai(
            {
                "id": "msg_01",
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 90,
                    "cache_creation_input_tokens": 5,
                },
            },
            created=1750000000,
        )
        self.assertEqual(out["id"], "chatcmpl-msg_01")
        self.assertEqual(out["object"], "chat.completion")
        self.assertEqual(out["created"], 1750000000)
        self.assertEqual(out["model"], "claude-sonnet-5")
        choice = out["choices"][0]
        self.assertEqual(choice["index"], 0)
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"], {"role": "assistant", "content": "Hello"})
        self.assertEqual(
            out["usage"],
            {
                "prompt_tokens": 105,
                "completion_tokens": 4,
                "total_tokens": 109,
                "prompt_tokens_details": {"cached_tokens": 90},
            },
        )

    def test_tool_use_response(self):
        out = anthropic_response_to_openai(
            {
                "id": "msg_02",
                "model": "m",
                "content": [
                    {"type": "text", "text": "Checking. "},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            created=1,
        )
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["content"], "Checking. ")
        self.assertEqual(
            msg["tool_calls"],
            [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": "{\"city\": \"Paris\"}",
                    },
                }
            ],
        )
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")

    def test_tool_only_response_has_null_content(self):
        out = anthropic_response_to_openai(
            {
                "id": "m",
                "model": "m",
                "content": [
                    {"type": "tool_use", "id": "t", "name": "f", "input": {}}
                ],
                "stop_reason": "tool_use",
                "usage": {},
            },
            created=1,
        )
        self.assertIsNone(out["choices"][0]["message"]["content"])

    def test_thinking_blocks_skipped(self):
        out = anthropic_response_to_openai(
            {
                "id": "m",
                "model": "m",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "s"},
                    {"type": "text", "text": "answer"},
                ],
                "stop_reason": "end_turn",
                "usage": {},
            },
            created=1,
        )
        self.assertEqual(out["choices"][0]["message"]["content"], "answer")

    def test_finish_reason_map(self):
        self.assertIsNone(map_finish_reason(None))
        self.assertEqual(map_finish_reason("end_turn"), "stop")
        self.assertEqual(map_finish_reason("stop_sequence"), "stop")
        self.assertEqual(map_finish_reason("max_tokens"), "length")
        self.assertEqual(map_finish_reason("tool_use"), "tool_calls")
        self.assertEqual(map_finish_reason("refusal"), "content_filter")
        self.assertEqual(map_finish_reason("something_new"), "stop")


class ErrorMappingTests(unittest.TestCase):
    def test_rate_limit_error(self):
        out = anthropic_error_to_openai(
            429,
            b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}',
        )
        self.assertEqual(
            out,
            {
                "error": {
                    "message": "slow down",
                    "type": "rate_limit_error",
                    "code": "rate_limit_error",
                }
            },
        )

    def test_overloaded_maps_to_server_error(self):
        out = anthropic_error_to_openai(
            529,
            b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
        )
        self.assertEqual(out["error"]["type"], "server_error")

    def test_auth_error_maps_to_invalid_request(self):
        out = anthropic_error_to_openai(
            401,
            b'{"type":"error","error":{"type":"authentication_error","message":"bad key"}}',
        )
        self.assertEqual(out["error"]["type"], "invalid_request_error")

    def test_non_json_body_falls_back_by_status(self):
        out = anthropic_error_to_openai(502, b"Bad Gateway")
        self.assertEqual(out["error"]["type"], "server_error")
        self.assertEqual(out["error"]["message"], "Bad Gateway")
        self.assertIsNone(out["error"]["code"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: ImportError on the new names.

- [ ] **Step 3: Implement**

Add to `src/smart_proxy/openai_compat.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/openai_compat.py tests/test_openai_compat_transform.py
git commit -m "feat(openai-compat): response and error transforms"
```

---

### Task 4: Streaming SSE converter

**Files:**
- Modify: `src/smart_proxy/openai_compat.py`
- Test: `tests/test_openai_compat_sse.py` (create)

**Interfaces:**
- Consumes: `map_finish_reason`, `_usage_to_openai`, `anthropic_error_to_openai` from Task 3.
- Produces: `class AnthropicToOpenAIStream` —
  - `__init__(self, *, created: int, include_usage: bool = False)`
  - `feed(self, chunk: bytes) -> bytes` — feed raw upstream bytes, get OpenAI SSE bytes to forward (may be empty).
  - `finish(self) -> bytes` — call after upstream EOF; emits the finish chunk + `[DONE]` if the stream ended without `message_stop`.
  - properties `usage: dict | None` (OpenAI-format usage, for stats), `model: str`, `completion_id: str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_openai_compat_sse.py`:

```python
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.openai_compat import AnthropicToOpenAIStream


def _evt(payload: dict) -> bytes:
    return (
        f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n".encode()
    )


TEXT_STREAM = b"".join(
    [
        _evt(
            {
                "type": "message_start",
                "message": {
                    "id": "msg_01",
                    "model": "claude-sonnet-5",
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 90},
                },
            }
        ),
        _evt(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        ),
        _evt(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hel"},
            }
        ),
        _evt(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "lo"},
            }
        ),
        _evt({"type": "content_block_stop", "index": 0}),
        _evt(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            }
        ),
        _evt({"type": "message_stop"}),
    ]
)

TOOL_STREAM = b"".join(
    [
        _evt(
            {
                "type": "message_start",
                "message": {"id": "msg_02", "model": "m", "usage": {"input_tokens": 3}},
            }
        ),
        _evt(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                },
            }
        ),
        _evt(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{\"ci"},
            }
        ),
        _evt(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "ty\": \"Paris\"}"},
            }
        ),
        _evt({"type": "content_block_stop", "index": 0}),
        _evt(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 9},
            }
        ),
        _evt({"type": "message_stop"}),
    ]
)


def _collect(raw: bytes, *, include_usage: bool = False, chunk_size: int | None = None):
    """Feed raw bytes (optionally fragmented) and parse produced OpenAI SSE."""
    conv = AnthropicToOpenAIStream(created=1750000000, include_usage=include_usage)
    out = b""
    if chunk_size is None:
        out += conv.feed(raw)
    else:
        for i in range(0, len(raw), chunk_size):
            out += conv.feed(raw[i : i + chunk_size])
    out += conv.finish()
    lines = [l for l in out.decode().split("\n\n") if l.strip()]
    parsed = []
    for line in lines:
        assert line.startswith("data: "), line
        payload = line[len("data: ") :]
        parsed.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return parsed, conv


class TextStreamTests(unittest.TestCase):
    def test_text_stream_conversion(self):
        chunks, conv = _collect(TEXT_STREAM)
        self.assertEqual(chunks[-1], "[DONE]")
        self.assertEqual(chunks[0]["object"], "chat.completion.chunk")
        self.assertEqual(chunks[0]["id"], "chatcmpl-msg_01")
        self.assertEqual(chunks[0]["model"], "claude-sonnet-5")
        self.assertEqual(
            chunks[0]["choices"][0]["delta"], {"role": "assistant", "content": ""}
        )
        texts = [
            c["choices"][0]["delta"].get("content")
            for c in chunks[1:-2]
            if isinstance(c, dict)
        ]
        self.assertEqual("".join(t for t in texts if t), "Hello")
        finish = chunks[-2]
        self.assertEqual(finish["choices"][0]["finish_reason"], "stop")
        self.assertEqual(finish["choices"][0]["delta"], {})
        self.assertEqual(
            conv.usage,
            {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "total_tokens": 105,
                "prompt_tokens_details": {"cached_tokens": 90},
            },
        )

    def test_byte_by_byte_fragmentation_is_equivalent(self):
        whole, _ = _collect(TEXT_STREAM)
        fragmented, _ = _collect(TEXT_STREAM, chunk_size=1)
        self.assertEqual(whole, fragmented)

    def test_include_usage_appends_usage_chunk(self):
        chunks, _ = _collect(TEXT_STREAM, include_usage=True)
        usage_chunk = chunks[-2]
        self.assertEqual(usage_chunk["choices"], [])
        self.assertEqual(usage_chunk["usage"]["completion_tokens"], 5)
        self.assertEqual(chunks[-1], "[DONE]")

    def test_without_include_usage_no_usage_chunk(self):
        chunks, _ = _collect(TEXT_STREAM)
        for c in chunks:
            if isinstance(c, dict):
                self.assertNotIn("usage", c)


class ToolStreamTests(unittest.TestCase):
    def test_tool_call_stream(self):
        chunks, _ = _collect(TOOL_STREAM)
        start = next(
            c
            for c in chunks
            if isinstance(c, dict) and c["choices"] and "tool_calls" in c["choices"][0]["delta"]
        )
        tc = start["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(tc["index"], 0)
        self.assertEqual(tc["id"], "toolu_1")
        self.assertEqual(tc["function"], {"name": "get_weather", "arguments": ""})
        args = "".join(
            c["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
            for c in chunks
            if isinstance(c, dict)
            and c["choices"]
            and "tool_calls" in c["choices"][0]["delta"]
        )
        self.assertEqual(args, "{\"city\": \"Paris\"}")
        finish = chunks[-2]
        self.assertEqual(finish["choices"][0]["finish_reason"], "tool_calls")


class StreamEdgeCaseTests(unittest.TestCase):
    def test_ping_and_thinking_events_skipped(self):
        raw = b"".join(
            [
                _evt(
                    {
                        "type": "message_start",
                        "message": {"id": "m", "model": "m", "usage": {}},
                    }
                ),
                b"event: ping\ndata: {\"type\": \"ping\"}\n\n",
                _evt(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "..."},
                    }
                ),
                _evt({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}),
                _evt({"type": "message_stop"}),
            ]
        )
        chunks, _ = _collect(raw)
        self.assertEqual(len(chunks), 3)  # role chunk, finish chunk, [DONE]

    def test_mid_stream_error_event(self):
        raw = b"".join(
            [
                _evt(
                    {
                        "type": "message_start",
                        "message": {"id": "m", "model": "m", "usage": {}},
                    }
                ),
                _evt(
                    {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "Overloaded"},
                    }
                ),
            ]
        )
        chunks, _ = _collect(raw)
        self.assertEqual(chunks[-1], "[DONE]")
        err = chunks[-2]
        self.assertEqual(err["error"]["type"], "server_error")
        self.assertEqual(err["error"]["message"], "Overloaded")

    def test_truncated_stream_finish_closes_out(self):
        raw = _evt(
            {
                "type": "message_start",
                "message": {"id": "m", "model": "m", "usage": {}},
            }
        )
        chunks, _ = _collect(raw)
        self.assertEqual(chunks[-1], "[DONE]")
        self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "stop")

    def test_crlf_line_endings_handled(self):
        raw = TEXT_STREAM.replace(b"\n", b"\r\n")
        chunks, _ = _collect(raw)
        self.assertEqual(chunks[-1], "[DONE]")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_openai_compat_sse.py -q`
Expected: ImportError — `AnthropicToOpenAIStream` not defined.

- [ ] **Step 3: Implement**

Add to `src/smart_proxy/openai_compat.py`:

```python
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
        self._buf += chunk.replace(b"\r\n", b"\n")
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_openai_compat_sse.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/openai_compat.py tests/test_openai_compat_sse.py
git commit -m "feat(openai-compat): incremental anthropic-to-openai SSE converter"
```

---

### Task 5: Tracking stats

**Files:**
- Modify: `src/smart_proxy/openai_compat.py`
- Test: `tests/test_openai_compat_transform.py` (append)

**Interfaces:**
- Produces: `class OpenAICompatStats` — methods `record_request(*, model: str, stream: bool)`, `record_client_error()`, `record_upstream_error()`, `record_usage(*, prompt_tokens: int, completion_tokens: int)`, `record_ignored(params: list[str])`, `snapshot() -> dict`. Snapshot keys: `started_at`, `last_request_at`, `requests`, `streaming_requests`, `client_errors`, `upstream_errors`, `prompt_tokens`, `completion_tokens`, `by_model`, `ignored_params`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_openai_compat_transform.py`:

```python
from smart_proxy.openai_compat import OpenAICompatStats  # noqa: E402


class StatsTests(unittest.TestCase):
    def test_snapshot_counts(self):
        stats = OpenAICompatStats()
        stats.record_request(model="claude-sonnet-5", stream=True)
        stats.record_request(model="claude-sonnet-5", stream=False)
        stats.record_request(model="claude-opus-4-8", stream=False)
        stats.record_client_error()
        stats.record_upstream_error()
        stats.record_usage(prompt_tokens=100, completion_tokens=20)
        stats.record_usage(prompt_tokens=1, completion_tokens=2)
        stats.record_ignored(["seed", "seed", "logprobs"])
        snap = stats.snapshot()
        self.assertEqual(snap["requests"], 3)
        self.assertEqual(snap["streaming_requests"], 1)
        self.assertEqual(snap["client_errors"], 1)
        self.assertEqual(snap["upstream_errors"], 1)
        self.assertEqual(snap["prompt_tokens"], 101)
        self.assertEqual(snap["completion_tokens"], 22)
        self.assertEqual(
            snap["by_model"], {"claude-sonnet-5": 2, "claude-opus-4-8": 1}
        )
        self.assertEqual(snap["ignored_params"], {"seed": 2, "logprobs": 1})
        self.assertIsNotNone(snap["started_at"])
        self.assertIsNotNone(snap["last_request_at"])

    def test_fresh_snapshot(self):
        snap = OpenAICompatStats().snapshot()
        self.assertEqual(snap["requests"], 0)
        self.assertIsNone(snap["last_request_at"])
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: ImportError — `OpenAICompatStats` not defined.

- [ ] **Step 3: Implement**

Add to `src/smart_proxy/openai_compat.py` (extend the module imports with `from dataclasses import dataclass, field` and `from datetime import datetime, timezone`):

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_openai_compat_transform.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/openai_compat.py tests/test_openai_compat_transform.py
git commit -m "feat(openai-compat): in-memory tracking stats"
```

---

### Task 6: HTTP handlers + route setup + integration tests

**Files:**
- Modify: `src/smart_proxy/openai_compat.py`
- Test: `tests/test_openai_compat_endpoint.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: `setup_openai_compat(app: web.Application, *, default_max_tokens: int = 8192, auto_cache: bool = True, cache_ttl: str = "1h", models_passthrough=None) -> None` — sets `app["openai_compat_stats"]`, `app["openai_compat_default_max_tokens"]`, `app["openai_compat_auto_cache"]`, `app["openai_compat_cache_ttl"]`, `app["openai_compat_models_passthrough"]`, and registers `POST /v1/chat/completions`, `GET /v1/models`, `GET /_openai_compat_stats`. `models_passthrough` is an aiohttp handler used for `GET /v1/models` requests that carry an `anthropic-version` header (native Anthropic clients) — Task 7 wires the actual delegation; this task registers and stores it.
- Reads at request time (set by the embedding app): `app["anthropic_pool"]` (only `.check_auth(token)` is used), `app["http_client"]` (httpx.AsyncClient), `app["openai_compat_loopback_base"]` (e.g. `http://127.0.0.1:8090`).
- Produces: marker header constant `_MARKER_HEADER = "x-smart-proxy-openai-compat"` on every compat response.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_openai_compat_endpoint.py`:

```python
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import aiohttp
import httpx
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from smart_proxy.openai_compat import OpenAICompatStats, setup_openai_compat

ANTHROPIC_JSON = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "Hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 4},
}

ANTHROPIC_SSE = "".join(
    f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
    for e in [
        {
            "type": "message_start",
            "message": {"id": "msg_02", "model": "claude-sonnet-5", "usage": {"input_tokens": 7}},
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]
).encode()


class _FakeInner:
    """Stands in for the proxy's own /v1/messages endpoint."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.response: tuple = ("json", 200, ANTHROPIC_JSON, {})

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            {"body": json.loads(await request.read()), "headers": dict(request.headers)}
        )
        kind, status, payload, headers = self.response
        if kind == "json":
            return web.Response(
                status=status,
                body=json.dumps(payload).encode(),
                content_type="application/json",
                headers=headers,
            )
        resp = web.StreamResponse(status=status)
        resp.headers["content-type"] = "text/event-stream; charset=utf-8"
        await resp.prepare(request)
        await resp.write(payload)
        await resp.write_eof()
        return resp


class OpenAICompatEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.inner = _FakeInner()
        inner_app = web.Application()
        inner_app.router.add_post("/v1/messages", self.inner.handle)
        self.inner_server = TestServer(inner_app)
        await self.inner_server.start_server()

        outer_app = web.Application()
        outer_app["anthropic_pool"] = SimpleNamespace(
            check_auth=lambda token: token == "sp-test"
        )
        outer_app["openai_compat_loopback_base"] = str(
            self.inner_server.make_url("")
        ).rstrip("/")
        self.httpx_client = httpx.AsyncClient()
        outer_app["http_client"] = self.httpx_client
        setup_openai_compat(outer_app, default_max_tokens=8192, auto_cache=True)
        self.outer_app = outer_app
        self.client = TestClient(TestServer(outer_app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.httpx_client.aclose()
        await self.inner_server.close()

    def _chat(self, **overrides):
        payload = {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
        }
        payload.update(overrides)
        return payload

    async def test_non_streaming_roundtrip(self):
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer sp-test"},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("x-smart-proxy-openai-compat"), "1")
        data = await resp.json()
        self.assertEqual(data["object"], "chat.completion")
        self.assertEqual(data["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        # loopback carried the client token and anthropic body
        sent = self.inner.requests[0]
        self.assertEqual(sent["headers"].get("Authorization"), "Bearer sp-test")
        self.assertEqual(sent["body"]["model"], "claude-sonnet-5")
        self.assertIn("max_tokens", sent["body"])
        last_block = sent["body"]["messages"][-1]["content"][-1]
        self.assertEqual(
            last_block["cache_control"], {"type": "ephemeral", "ttl": "1h"}
        )

    async def test_streaming_roundtrip(self):
        self.inner.response = ("sse", 200, ANTHROPIC_SSE, {})
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(stream=True),
            headers={"Authorization": "Bearer sp-test"},
        )
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
        self.assertEqual(resp.headers.get("x-smart-proxy-openai-compat"), "1")
        body = await resp.read()
        text = body.decode()
        self.assertIn('"chat.completion.chunk"', text)
        self.assertIn('"content":"Hi"', text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"))
        # inner saw stream:true
        self.assertIs(self.inner.requests[0]["body"].get("stream"), True)

    async def test_auth_rejected(self):
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer wrong"},
        )
        self.assertEqual(resp.status, 401)
        data = await resp.json()
        self.assertEqual(data["error"]["code"], "invalid_api_key")

    async def test_invalid_body_rejected(self):
        resp = await self.client.post(
            "/v1/chat/completions",
            data=b"{not json",
            headers={
                "Authorization": "Bearer sp-test",
                "content-type": "application/json",
            },
        )
        self.assertEqual(resp.status, 400)

    async def test_upstream_429_passthrough(self):
        self.inner.response = (
            "json",
            429,
            {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
            {"retry-after": "17", "x-should-retry": "false"},
        )
        resp = await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer sp-test"},
        )
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers.get("retry-after"), "17")
        self.assertEqual(resp.headers.get("x-should-retry"), "false")
        data = await resp.json()
        self.assertEqual(data["error"]["type"], "rate_limit_error")

    async def test_stats_counting_and_endpoint(self):
        await self.client.post(
            "/v1/chat/completions",
            json=self._chat(),
            headers={"Authorization": "Bearer sp-test"},
        )
        resp = await self.client.get(
            "/_openai_compat_stats", headers={"Authorization": "Bearer sp-test"}
        )
        self.assertEqual(resp.status, 200)
        snap = await resp.json()
        self.assertEqual(snap["requests"], 1)
        self.assertEqual(snap["by_model"], {"claude-sonnet-5": 1})
        self.assertEqual(snap["prompt_tokens"], 10)
        self.assertEqual(snap["completion_tokens"], 4)

    async def test_stats_endpoint_requires_auth(self):
        resp = await self.client.get("/_openai_compat_stats")
        self.assertEqual(resp.status, 401)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_openai_compat_endpoint.py -q`
Expected: ImportError — `setup_openai_compat` not defined.

- [ ] **Step 3: Implement**

Add to `src/smart_proxy/openai_compat.py` (extend module imports with `import time`, `from uuid import uuid4`, `import httpx`, `from aiohttp import web`):

```python
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
        return _error_response(401, "unauthorized", code="invalid_api_key")

    try:
        payload = json.loads(await request.read())
    except (json.JSONDecodeError, ValueError):
        stats.record_client_error()
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

    client: httpx.AsyncClient = request.app["http_client"]
    base = request.app["openai_compat_loopback_base"]
    loop_headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "anthropic-version": _ANTHROPIC_VERSION,
        "accept": "text/event-stream" if stream else "application/json",
        "user-agent": _COMPAT_USER_AGENT,
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
        return _error_response(
            502, f"upstream connection error: {exc}", err_type="server_error"
        )

    if r.status_code != 200:
        raw = await r.aread()
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
        raw = await r.aread()
        await r.aclose()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            stats.record_upstream_error()
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
    await resp.prepare(request)
    try:
        async for chunk in r.aiter_bytes():
            out_bytes = conv.feed(chunk)
            if out_bytes:
                await resp.write(out_bytes)
        tail = conv.finish()
        if tail:
            await resp.write(tail)
    except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
        logger.warning("[openai-compat] mid-stream read error op=%s: %s", op_id, exc)
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


def setup_openai_compat(
    app: web.Application,
    *,
    default_max_tokens: int = 8192,
    auto_cache: bool = True,
    cache_ttl: str = "1h",
    models_passthrough=None,
) -> None:
    """Register OpenAI-compatible routes. Must be called BEFORE the catch-all route."""
    app["openai_compat_stats"] = OpenAICompatStats()
    app["openai_compat_default_max_tokens"] = default_max_tokens
    app["openai_compat_auto_cache"] = auto_cache
    app["openai_compat_cache_ttl"] = cache_ttl
    app["openai_compat_models_passthrough"] = models_passthrough
    app.router.add_post("/v1/chat/completions", _chat_completions_handler)
    app.router.add_get("/v1/models", _models_handler)
    app.router.add_get("/_openai_compat_stats", _stats_handler)
```

Also add a temporary `_models_handler` stub so the module imports (Task 7 replaces it with the real implementation):

```python
async def _models_handler(request: web.Request) -> web.Response:
    return _error_response(501, "not implemented yet", err_type="server_error")
```

Place `_models_handler` above `setup_openai_compat`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_openai_compat_endpoint.py tests/test_openai_compat_transform.py tests/test_openai_compat_sse.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/openai_compat.py tests/test_openai_compat_endpoint.py
git commit -m "feat(openai-compat): chat completions handler with loopback dispatch and stats endpoint"
```

---

### Task 7: /v1/models — static list + native-client delegation

**Files:**
- Modify: `src/smart_proxy/openai_compat.py` (replace the `_models_handler` stub)
- Test: `tests/test_openai_compat_endpoint.py` (append)

**Interfaces:**
- Consumes: `setup_openai_compat(..., models_passthrough=...)` from Task 6; `MODEL_PRICES` from `smart_proxy.usage`.
- Produces: `_models_handler` — requests with an `anthropic-version` header are delegated to `app["openai_compat_models_passthrough"]` (native Anthropic clients keep today's behavior); all others get a static OpenAI-format model list derived from `claude-*` keys of `MODEL_PRICES`. No loopback (a loopback GET /v1/models would recurse into this very route).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_openai_compat_endpoint.py` (inside a new test class):

```python
class ModelsEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.passthrough_calls: list[str] = []

        async def fake_passthrough(request: web.Request) -> web.Response:
            self.passthrough_calls.append(request.path)
            return web.Response(
                body=b'{"native": true}', content_type="application/json"
            )

        app = web.Application()
        app["anthropic_pool"] = SimpleNamespace(check_auth=lambda t: t == "sp-test")
        setup_openai_compat(app, models_passthrough=fake_passthrough)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_openai_client_gets_static_list(self):
        resp = await self.client.get(
            "/v1/models", headers={"Authorization": "Bearer sp-test"}
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("x-smart-proxy-openai-compat"), "1")
        data = await resp.json()
        self.assertEqual(data["object"], "list")
        ids = [m["id"] for m in data["data"]]
        self.assertIn("claude-sonnet-5", ids)
        self.assertTrue(all(i.startswith("claude-") for i in ids))
        self.assertEqual(data["data"][0]["object"], "model")
        self.assertEqual(data["data"][0]["owned_by"], "anthropic")
        self.assertEqual(self.passthrough_calls, [])

    async def test_native_anthropic_client_is_delegated(self):
        resp = await self.client.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer sp-test",
                "anthropic-version": "2023-06-01",
            },
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"native": True})
        self.assertEqual(self.passthrough_calls, ["/v1/models"])

    async def test_models_requires_auth(self):
        resp = await self.client.get("/v1/models")
        self.assertEqual(resp.status, 401)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_openai_compat_endpoint.py -q`
Expected: ModelsEndpointTests FAIL (501 from the stub / delegation not happening).

- [ ] **Step 3: Implement**

In `src/smart_proxy/openai_compat.py`, add `from smart_proxy.usage import MODEL_PRICES` to the imports and replace the `_models_handler` stub with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_openai_compat_endpoint.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/smart_proxy/openai_compat.py tests/test_openai_compat_endpoint.py
git commit -m "feat(openai-compat): /v1/models with native-client passthrough delegation"
```

---

### Task 8: Wire into create_app, config, and main

**Files:**
- Modify: `src/smart_proxy/config.py` (add 3 fields next to the other `anthropic_proxy_*` fields)
- Modify: `src/smart_proxy/anthropic_proxy.py` (`create_app` signature + registration, `main()`)
- Test: `tests/test_openai_compat_endpoint.py` (append)

**Interfaces:**
- Consumes: `setup_openai_compat` from Task 6.
- Produces: `create_app(..., openai_compat_enabled: bool = True, openai_compat_default_max_tokens: int = 8192, openai_compat_auto_cache: bool = True, openai_compat_cache_ttl: str = "1h")`; Settings fields `anthropic_proxy_openai_compat_enabled: bool = True`, `anthropic_proxy_openai_compat_default_max_tokens: int = 8192`, `anthropic_proxy_openai_compat_auto_cache: bool = True`, `anthropic_proxy_openai_compat_cache_ttl: str = "1h"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_openai_compat_endpoint.py`:

```python
from smart_proxy.anthropic_proxy import create_app  # noqa: E402
from smart_proxy.config import Settings  # noqa: E402


class CreateAppWiringTests(unittest.TestCase):
    def _canonicals(self, app) -> set[str]:
        return {r.canonical for r in app.router.resources()}

    def test_compat_routes_registered_by_default(self):
        app = create_app("./ignored.db", oauth_smoke_enabled=False)
        canonicals = self._canonicals(app)
        self.assertIn("/v1/chat/completions", canonicals)
        self.assertIn("/v1/models", canonicals)
        self.assertIn("/_openai_compat_stats", canonicals)
        self.assertIsNotNone(app.get("openai_compat_loopback_base"))
        self.assertEqual(app["openai_compat_default_max_tokens"], 8192)

    def test_compat_routes_absent_when_disabled(self):
        app = create_app(
            "./ignored.db", oauth_smoke_enabled=False, openai_compat_enabled=False
        )
        canonicals = self._canonicals(app)
        self.assertNotIn("/v1/chat/completions", canonicals)
        self.assertNotIn("/_openai_compat_stats", canonicals)

    def test_compat_routes_precede_catch_all(self):
        app = create_app("./ignored.db", oauth_smoke_enabled=False)
        canonicals = [r.canonical for r in app.router.resources()]
        self.assertLess(
            canonicals.index("/v1/chat/completions"),
            canonicals.index("/{path}"),
        )

    def test_settings_defaults(self):
        s = Settings(credentials_api_key="x")
        self.assertTrue(s.anthropic_proxy_openai_compat_enabled)
        self.assertEqual(s.anthropic_proxy_openai_compat_default_max_tokens, 8192)
        self.assertTrue(s.anthropic_proxy_openai_compat_auto_cache)
        self.assertEqual(s.anthropic_proxy_openai_compat_cache_ttl, "1h")
```

Note: if `canonicals.index("/{path}")` fails because aiohttp renders the catch-all canonical differently, print `canonicals` once and use the actual literal (it is `/{path}` for `add_route("*", "/{path:.+}", ...)` on current aiohttp).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_openai_compat_endpoint.py -q`
Expected: `TypeError: create_app() got an unexpected keyword argument 'openai_compat_enabled'` and Settings attribute errors.

- [ ] **Step 3: Implement**

In `src/smart_proxy/config.py`, after `anthropic_proxy_reload_key: str = ""` add:

```python
    # OpenAI-compatible endpoint (/v1/chat/completions) on the anthropic proxy.
    anthropic_proxy_openai_compat_enabled: bool = True
    anthropic_proxy_openai_compat_default_max_tokens: int = 8192
    # Inject cache_control breakpoints (system + last message) into translated
    # requests; the OpenAI protocol has no prompt caching of its own.
    anthropic_proxy_openai_compat_auto_cache: bool = True
    # TTL for injected breakpoints: "1h" (default; interactive agents pause
    # longer than 5m) or "5m" (cheaper cache writes).
    anthropic_proxy_openai_compat_cache_ttl: str = "1h"
```

In `src/smart_proxy/anthropic_proxy.py`:

1. Add the import near the other `smart_proxy` imports at the top of the file:

```python
from smart_proxy.openai_compat import setup_openai_compat
```

2. Extend the `create_app` signature (after `oauth_login_redirect_port: str = ""`):

```python
    openai_compat_enabled: bool = True,
    openai_compat_default_max_tokens: int = 8192,
    openai_compat_auto_cache: bool = True,
    openai_compat_cache_ttl: str = "1h",
```

3. In the `create_app` body, insert immediately BEFORE the `app.router.add_route("*", "/", _root_handler)` line:

```python
    if openai_compat_enabled:
        app.setdefault(
            "openai_compat_loopback_base", f"http://127.0.0.1:{PROXY_PORT}"
        )
        setup_openai_compat(
            app,
            default_max_tokens=openai_compat_default_max_tokens,
            auto_cache=openai_compat_auto_cache,
            cache_ttl=openai_compat_cache_ttl,
            models_passthrough=_proxy_handler,
        )
```

4. In `main()`, add to the `create_app(...)` call:

```python
        openai_compat_enabled=settings.anthropic_proxy_openai_compat_enabled,
        openai_compat_default_max_tokens=settings.anthropic_proxy_openai_compat_default_max_tokens,
        openai_compat_auto_cache=settings.anthropic_proxy_openai_compat_auto_cache,
        openai_compat_cache_ttl=settings.anthropic_proxy_openai_compat_cache_ttl,
```

No other lines in `anthropic_proxy.py` change.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_openai_compat_endpoint.py -q`
Expected: all PASS

- [ ] **Step 5: Run the FULL suite — Anthropic-format regression gate**

Run: `uv run pytest tests/ -q`
Expected: everything passes. Then verify the hot path is untouched:

Run: `git diff HEAD~4 --stat -- src/smart_proxy/anthropic_proxy.py` (adjust the ref to the last pre-feature commit)
Expected: only the import line, `create_app` signature/body, and `main()` — no hunks inside `_proxy_handler` or its helpers.

- [ ] **Step 6: Commit**

```bash
git add src/smart_proxy/config.py src/smart_proxy/anthropic_proxy.py tests/test_openai_compat_endpoint.py
git commit -m "feat(anthropic-proxy): wire OpenAI-compat routes behind a feature flag"
```

---

### Task 9: Manual smoke script + docs

**Files:**
- Create: `anthropic-proxy-test/openai_compat_smoke.py`
- Modify: `src/smart_proxy/anthropic_proxy.py:1-10` (module docstring only — mention the compat endpoint)

**Interfaces:**
- Consumes: a running proxy (`uv run python -m smart_proxy.anthropic_proxy` or the deployed instance) and a valid `sp-` token.

- [ ] **Step 1: Write the smoke script**

Create `anthropic-proxy-test/openai_compat_smoke.py`:

```python
"""Manual smoke test for the OpenAI-compat endpoint.

Usage:
    uv run --with openai python anthropic-proxy-test/openai_compat_smoke.py \
        --base-url http://127.0.0.1:8090/v1 --api-key sp-... [--model claude-sonnet-5]

Runs three checks: plain chat, streamed chat, tool-call roundtrip.
Exit code 0 = all passed.
"""
from __future__ import annotations

import argparse
import json
import sys

from openai import OpenAI


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--model", default="claude-sonnet-5")
    args = ap.parse_args()

    client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    failures = 0

    # 1. plain chat
    r = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": "Reply with exactly: pong"}],
        max_tokens=16,
    )
    text = r.choices[0].message.content or ""
    ok = "pong" in text.lower()
    print(f"[1] plain chat: {'OK' if ok else 'FAIL'} content={text!r} usage={r.usage}")
    failures += 0 if ok else 1

    # 2. streamed chat
    stream = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": "Count from 1 to 5, digits only."}],
        max_tokens=64,
        stream=True,
        stream_options={"include_usage": True},
    )
    collected = ""
    saw_usage = False
    for chunk in stream:
        if chunk.usage:
            saw_usage = True
        if chunk.choices and chunk.choices[0].delta.content:
            collected += chunk.choices[0].delta.content
    ok = "5" in collected and saw_usage
    print(f"[2] streaming: {'OK' if ok else 'FAIL'} text={collected!r} usage_chunk={saw_usage}")
    failures += 0 if ok else 1

    # 3. tool-call roundtrip
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    messages = [{"role": "user", "content": "What's the weather in Paris? Use the tool."}]
    r = client.chat.completions.create(
        model=args.model, messages=messages, tools=tools, max_tokens=256
    )
    call = (r.choices[0].message.tool_calls or [None])[0]
    if call is None:
        print("[3] tool call: FAIL — model did not call the tool")
        failures += 1
    else:
        args_ok = "paris" in call.function.arguments.lower()
        messages.append(
            {
                "role": "assistant",
                "content": r.choices[0].message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps({"temp_c": 21, "condition": "sunny"}),
            }
        )
        r2 = client.chat.completions.create(
            model=args.model, messages=messages, tools=tools, max_tokens=128
        )
        final = r2.choices[0].message.content or ""
        ok = args_ok and "21" in final
        print(f"[3] tool roundtrip: {'OK' if ok else 'FAIL'} final={final!r}")
        failures += 0 if ok else 1

    print(f"\n{'ALL OK' if failures == 0 else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Update the proxy module docstring**

In `src/smart_proxy/anthropic_proxy.py`, extend the module docstring (top of file, before any code) with one line:

```
Also exposes an OpenAI-compatible POST /v1/chat/completions (see smart_proxy.openai_compat);
translated traffic is marked with the x-smart-proxy-openai-compat response header and
counted at GET /_openai_compat_stats.
```

- [ ] **Step 3: Run the smoke against a locally started proxy (needs a working key in smart-proxy.db)**

```bash
uv run python -m smart_proxy.anthropic_proxy &   # or use the already-running instance
uv run --with openai python anthropic-proxy-test/openai_compat_smoke.py \
    --base-url http://127.0.0.1:8090/v1 --api-key <sp-token>
```

Expected: `ALL OK`, and the proxy log shows `[openai-compat] >>>`/`<<<` lines. Then `curl -H "Authorization: Bearer <sp-token>" http://127.0.0.1:8090/_openai_compat_stats` shows non-zero `requests`. If no real key is available locally, defer this step to post-deploy verification — the step is still required before calling the feature done.

- [ ] **Step 4: Final full-suite run**

Run: `uv run pytest tests/ -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add anthropic-proxy-test/openai_compat_smoke.py src/smart_proxy/anthropic_proxy.py
git commit -m "test(openai-compat): manual smoke script; document compat endpoint"
```

---

## Post-merge checklist (manual, after deploy)

- [ ] Point one coding agent (opencode / Cline / Warp custom model) at `https://<proxy>/v1` with an `sp-` token; run a short agentic session with tool use.
- [ ] `GET /_openai_compat_stats` — confirm requests/tokens accumulate; check `ignored_params` for anything unexpectedly popular.
- [ ] Grep proxy logs for `[openai-compat]` and confirm no `ignored params`/error spam.
- [ ] Confirm native Claude Code traffic is unaffected (existing dashboards / `_oauth_usage`).
- [ ] Kill switch documented: `ANTHROPIC_PROXY_OPENAI_COMPAT_ENABLED=false` + restart.
