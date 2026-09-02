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

    def test_crlf_with_byte_fragmentation(self):
        raw = TEXT_STREAM.replace(b"\n", b"\r\n")
        whole, _ = _collect(raw)
        fragmented, _ = _collect(raw, chunk_size=1)
        self.assertEqual(whole, fragmented)
        lf_whole, _ = _collect(TEXT_STREAM)
        self.assertEqual(whole, lf_whole)

    def test_input_json_delta_for_unknown_block_dropped(self):
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
                        "type": "content_block_delta",
                        "index": 5,
                        "delta": {"type": "input_json_delta", "partial_json": "{}"},
                    }
                ),
                _evt({"type": "message_stop"}),
            ]
        )
        chunks, _ = _collect(raw)
        for c in chunks:
            if isinstance(c, dict) and c["choices"]:
                self.assertNotIn("tool_calls", c["choices"][0]["delta"])

    def test_finish_after_message_stop_is_empty(self):
        conv = AnthropicToOpenAIStream(created=1)
        conv.feed(TEXT_STREAM)
        self.assertEqual(conv.finish(), b"")
        self.assertEqual(conv.finish(), b"")


if __name__ == "__main__":
    unittest.main()
