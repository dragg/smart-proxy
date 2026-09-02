from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.openai_compat import (
    OpenAICompatError,
    openai_chat_to_anthropic,
    anthropic_error_to_openai,
    anthropic_response_to_openai,
    map_finish_reason,
    OpenAICompatStats,  # noqa: E402
)


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


if __name__ == "__main__":
    unittest.main()
