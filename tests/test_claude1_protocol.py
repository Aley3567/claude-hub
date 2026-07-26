from __future__ import annotations

import json
import unittest

import claude1_protocol as protocol


class ProviderFormatTests(unittest.TestCase):
    def test_format_precedence_matches_cc_switch_contract(self) -> None:
        self.assertEqual(
            protocol.provider_api_format(
                override="openai_chat",
                meta={"apiFormat": "anthropic"},
                provider_type="codex_oauth",
            ),
            "openai_chat",
        )
        self.assertEqual(
            protocol.provider_api_format(provider_type="codex_oauth"),
            "openai_responses",
        )
        self.assertEqual(
            protocol.provider_api_format(meta={"apiFormat": "openai_chat"}),
            "openai_chat",
        )
        self.assertEqual(protocol.provider_api_format(), "anthropic")


class RequestTransformTests(unittest.TestCase):
    def test_chat_request_preserves_tools_and_tool_results(self) -> None:
        endpoint, body = protocol.transform_request(
            {
                "model": "gpt-5-test",
                "max_tokens": 100,
                "system": "Be concise.",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "lookup",
                                "input": {"q": "x"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": {"ok": True},
                            }
                        ],
                    },
                ],
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Lookup",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "format": "uri"}
                            },
                        },
                    }
                ],
            },
            "openai_chat",
        )
        self.assertEqual(endpoint, "/v1/chat/completions")
        self.assertEqual(body["max_completion_tokens"], 100)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(body["messages"][2]["role"], "tool")
        parameters = body["tools"][0]["function"]["parameters"]
        self.assertNotIn("format", parameters["properties"]["url"])

    def test_codex_responses_request_includes_encrypted_reasoning(self) -> None:
        endpoint, body = protocol.transform_request(
            {
                "model": "codex-test",
                "messages": [{"role": "user", "content": "hello"}],
                "thinking": {"type": "enabled", "budget_tokens": 20_000},
            },
            "openai_responses",
            provider_type="codex_oauth",
        )
        self.assertEqual(endpoint, "/v1/responses")
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"]["effort"], "high")
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])


class ResponseTransformTests(unittest.TestCase):
    def test_chat_tool_call_becomes_anthropic_tool_use(self) -> None:
        body = protocol.transform_response(
            {
                "id": "chat_1",
                "model": "model-test",
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"x"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
            "openai_chat",
        )
        self.assertEqual(body["stop_reason"], "tool_use")
        self.assertEqual(body["content"][0]["type"], "tool_use")
        self.assertEqual(body["content"][0]["input"], {"q": "x"})
        self.assertEqual(body["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_cache_aware_usage_and_encrypted_reasoning_are_preserved(self) -> None:
        chat = protocol.transform_response(
            {
                "id": "chat-cache",
                "model": "fixture-model",
                "choices": [
                    {
                        "message": {"content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 12},
                },
            },
            "openai_chat",
        )
        self.assertEqual(chat["usage"]["cache_read_input_tokens"], 12)

        responses = protocol.transform_response(
            {
                "id": "response-cache",
                "model": "fixture-model",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "encrypted_content": "encrypted-fixture",
                        "summary": [{"text": "summary"}],
                    }
                ],
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 18},
                },
            },
            "openai_responses",
        )
        self.assertEqual(
            responses["content"][0],
            {"type": "redacted_thinking", "data": "encrypted-fixture"},
        )
        self.assertEqual(
            responses["usage"]["cache_read_input_tokens"],
            18,
        )

    def test_upstream_error_is_redacted_to_anthropic_shape(self) -> None:
        body = protocol.transform_error(
            {"error": {"message": "rate limited", "type": "quota"}},
            429,
        )
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        self.assertEqual(
            body["error"]["message"],
            "upstream request failed (HTTP 429)",
        )
        self.assertNotIn("rate limited", body["error"]["message"])


class StreamingTransformTests(unittest.TestCase):
    def test_chat_sse_is_emitted_as_terminal_anthropic_stream(self) -> None:
        upstream = [
            b'data: {"id":"chat_1","model":"model-test","choices":'
            b'[{"delta":{"content":"Hi"},"finish_reason":null}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n',
            b"data: [DONE]\n\n",
        ]
        translated = b"".join(
            protocol.translate_sse_chunks("openai_chat", upstream)
        ).decode()
        self.assertIn("event: message_start", translated)
        self.assertIn("event: content_block_delta", translated)
        self.assertIn("event: message_stop", translated)
        payloads = [
            json.loads(line[6:])
            for line in translated.splitlines()
            if line.startswith("data: ")
        ]
        self.assertTrue(any(item.get("type") == "message_stop" for item in payloads))
        terminal = next(
            item for item in payloads if item.get("type") == "message_delta"
        )
        self.assertEqual(terminal["usage"]["input_tokens"], 2)
        self.assertEqual(terminal["usage"]["output_tokens"], 1)

    def test_interim_usage_never_emits_a_second_stable_anchor(self) -> None:
        upstream = [
            b'data: {"id":"chat_2","choices":[{"delta":{"content":"A"},'
            b'"finish_reason":null}],"usage":{"prompt_tokens":180,'
            b'"completion_tokens":1}}\n\n',
            b'data: {"choices":[{"delta":{"content":"B"},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":120,'
            b'"completion_tokens":2,"prompt_tokens_details":'
            b'{"cached_tokens":40}}}\n\n',
            b"data: [DONE]\n\n",
        ]
        translated = b"".join(
            protocol.translate_sse_chunks("openai_chat", upstream)
        ).decode()
        payloads = [
            json.loads(line[6:])
            for line in translated.splitlines()
            if line.startswith("data: ")
        ]
        terminal = [
            item for item in payloads if item.get("type") == "message_delta"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["usage"]["input_tokens"], 120)
        self.assertEqual(
            terminal[0]["usage"]["cache_read_input_tokens"],
            40,
        )


if __name__ == "__main__":
    unittest.main()
