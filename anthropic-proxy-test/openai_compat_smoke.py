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
