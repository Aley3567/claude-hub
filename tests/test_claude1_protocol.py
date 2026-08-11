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
    def test_system_message_role_is_preserved_for_current_claude_code(self) -> None:
        payload = {
            "model": "system-message-model",
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "machine context"}]},
                {"role": "user", "content": "hello"},
            ],
        }

        _, chat = protocol.transform_request(payload, "openai_chat")
        _, responses = protocol.transform_request(payload, "openai_responses")

        self.assertEqual(
            chat["messages"][:2],
            [
                {"role": "system", "content": "machine context"},
                {"role": "user", "content": "hello"},
            ],
        )
        self.assertEqual(
            responses["input"][:2],
            [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": "machine context"}],
                },
                {"role": "user", "content": "hello"},
            ],
        )

    def test_orphan_or_duplicate_tool_results_fail_closed_for_both_formats(self) -> None:
        orphan = {
            "model": "tool-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "missing", "content": "result"}
                    ],
                }
            ],
        }
        duplicate = {
            "model": "tool-model",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "call_1", "name": "t", "input": {}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "one"},
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "two"},
                    ],
                },
            ],
        }
        for api_format in ("openai_chat", "openai_responses"):
            for kind, payload in (("orphan", orphan), ("duplicate", duplicate)):
                with self.subTest(api_format=api_format, kind=kind):
                    with self.assertRaises(protocol.ProtocolTransformError):
                        protocol.transform_request(payload, api_format)

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

    def test_url_images_are_preserved_for_chat_and_responses(self) -> None:
        payload = {
            "model": "vision-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://example.test/a.png"},
                        }
                    ],
                }
            ],
        }
        _, chat = protocol.transform_request(payload, "openai_chat")
        _, responses = protocol.transform_request(payload, "openai_responses")
        self.assertEqual(
            chat["messages"][0]["content"],
            [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}],
        )
        self.assertEqual(
            responses["input"][0]["content"],
            [{"type": "input_image", "image_url": "https://example.test/a.png"}],
        )

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

    def test_current_claude_output_config_maps_xhigh_effort(self) -> None:
        payload = {
            "model": "reasoning-model",
            "messages": [{"role": "user", "content": "hello"}],
            "output_config": {"effort": "xhigh"},
        }
        _, chat = protocol.transform_request(payload, "openai_chat")
        _, responses = protocol.transform_request(payload, "openai_responses")
        self.assertEqual(chat["reasoning_effort"], "xhigh")
        self.assertEqual(responses["reasoning"], {"effort": "xhigh", "summary": "auto"})

    def test_chat_request_keeps_thinking_only_assistant_turn(self) -> None:
        _, body = protocol.transform_request(
            {
                "model": "reasoning-model",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "work"}],
                    }
                ],
            },
            "openai_chat",
        )
        self.assertEqual(
            body["messages"],
            [{"role": "assistant", "content": None, "reasoning_content": "work"}],
        )

    def test_chat_request_maps_xhigh_thinking_to_reasoning_effort(self) -> None:
        _, body = protocol.transform_request(
            {
                "model": "reasoning-model",
                "messages": [{"role": "user", "content": "hello"}],
                "thinking": {"type": "adaptive", "effort": "xhigh"},
            },
            "openai_chat",
        )
        self.assertEqual(body["reasoning_effort"], "xhigh")

    def test_chat_request_places_tool_results_before_later_user_text(self) -> None:
        _, body = protocol.transform_request(
            {
                "model": "chat-model",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "lookup",
                                "input": {},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "continue"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": "result",
                            },
                        ],
                    },
                ],
            },
            "openai_chat",
        )
        self.assertEqual([message["role"] for message in body["messages"]], ["assistant", "tool", "user"])
        self.assertEqual(body["messages"][1]["tool_call_id"], "call_1")
        self.assertEqual(body["messages"][2]["content"], "continue")

    def test_responses_request_preserves_interleaved_function_call_order(self) -> None:
        _, body = protocol.transform_request(
            {
                "model": "responses-model",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "before"},
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "lookup",
                                "input": {"q": "x"},
                            },
                            {"type": "text", "text": "after"},
                        ],
                    }
                ],
            },
            "openai_responses",
        )
        self.assertEqual(
            body["input"],
            [
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "before"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": '{"q":"x"}',
                },
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "after"}],
                },
            ],
        )


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
        self.assertEqual(
            body["usage"],
            {
                "input_tokens": 7,
                "output_tokens": 3,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        )

    def test_upstream_error_is_redacted_to_anthropic_shape(self) -> None:
        body = protocol.transform_error(
            {"error": {"message": "rate limited", "type": "quota"}},
            429,
        )
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "rate_limit_error")

    def test_responses_response_becomes_anthropic_message(self) -> None:
        body = protocol.transform_response(
            {
                "id": "resp_1",
                "model": "responses-model",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "think"}],
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "answer"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": '{"q":"x"}',
                    },
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
            "openai_responses",
        )
        self.assertEqual(body["id"], "resp_1")
        self.assertEqual(
            body["content"],
            [
                {"type": "thinking", "thinking": "think"},
                {"type": "text", "text": "answer"},
                {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}},
            ],
        )
        self.assertEqual(body["stop_reason"], "tool_use")
        self.assertEqual(body["usage"]["input_tokens"], 4)

    def test_responses_accepts_numeric_string_usage_and_input_tool_arguments(self) -> None:
        body = protocol.transform_response(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "lookup",
                        "input": {"q": "x"},
                    }
                ],
                "usage": {"input_tokens": "4", "output_tokens": "2"},
            },
            "openai_responses",
        )
        self.assertEqual(body["content"][0]["input"], {"q": "x"})
        self.assertEqual(body["usage"]["input_tokens"], 4)
        self.assertEqual(body["usage"]["output_tokens"], 2)


class StreamingTransformTests(unittest.TestCase):
    def test_translated_sse_clean_eof_without_terminal_fails_closed(self) -> None:
        with self.assertRaisesRegex(protocol.ProtocolTransformError, "terminal event"):
            list(
                protocol.translate_sse_chunks(
                    "openai_chat",
                    [
                        b'data: {"choices":[{"delta":{"content":"partial"},'
                        b'"finish_reason":null}]}\n\n'
                    ],
                )
            )

    def test_sse_parser_limits_each_event_not_the_transport_chunk(self) -> None:
        parser = protocol.SSEParser(max_buffer=8)
        self.assertEqual(
            parser.feed(b"data: 1\n\ndata: 2\n\n"),
            [("message", "1"), ("message", "2")],
        )
        with self.assertRaises(protocol.ProtocolTransformError):
            protocol.SSEParser(max_buffer=8).feed(b"data: 123456789\n\n")

    def test_responses_failure_is_terminal_without_normal_message_stop(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.failed",
            json.dumps({"type": "response.failed", "error": {"message": "boom"}}),
        )
        chunks.extend(bridge.finish())
        self.assertEqual(
            [chunk.decode().splitlines()[0] for chunk in chunks],
            ["event: error"],
        )

    def test_duplicate_responses_done_is_idempotent(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "A",
                }
            ),
        )
        done = json.dumps(
            {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": "A",
            }
        )
        chunks.extend(bridge.feed("response.output_text.done", done))
        chunks.extend(bridge.feed("response.output_text.done", done))
        deltas = [chunk for chunk in chunks if b'"type":"content_block_delta"' in chunk]
        self.assertEqual(len(deltas), 1)

    def test_responses_item_snapshot_preserves_tool_arguments(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.added",
            json.dumps(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "lookup",
                    },
                }
            ),
        )
        chunks.extend(
            bridge.feed(
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "lookup",
                            "arguments": '{"q":"x"}',
                        },
                    }
                ),
            )
        )
        events = [json.loads(chunk.decode().split("\ndata: ", 1)[1]) for chunk in chunks]
        self.assertEqual(
            [event["delta"]["partial_json"] for event in events if event["type"] == "content_block_delta"],
            ['{"q":"x"}'],
        )
        self.assertEqual(
            [event["index"] for event in events if event["type"] == "content_block_stop"],
            [0],
        )

    def test_responses_tool_argument_aliases_do_not_duplicate_snapshot(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.added",
            json.dumps(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "lookup",
                    },
                }
            ),
        )
        chunks.extend(
            bridge.feed(
                "response.function_call_arguments.delta",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.delta",
                        "call_id": "call_1",
                        "delta": '{"q":',
                    }
                ),
            )
        )
        chunks.extend(
            bridge.feed(
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "arguments": '{"q":"x"}',
                        },
                    }
                ),
            )
        )
        events = [json.loads(chunk.decode().split("\ndata: ", 1)[1]) for chunk in chunks]
        self.assertEqual(
            [
                event["delta"]["partial_json"]
                for event in events
                if event["type"] == "content_block_delta"
            ],
            ['{"q":'],
        )

    def test_response_terminal_ignores_late_content(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        bridge.feed(
            "response.completed",
            json.dumps({"type": "response.completed", "response": {}}),
        )
        chunks = bridge.feed(
            "response.output_text.done",
            json.dumps(
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "late",
                }
            ),
        )
        self.assertEqual(chunks, [])

    def test_tool_blocks_require_protocol_roles(self) -> None:
        bad_payloads = [
            {
                "model": "m",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_use", "id": "call_1", "name": "lookup"}
                        ],
                    }
                ],
            },
            {
                "model": "m",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "call_1", "name": "lookup"}
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "call_1"}
                        ],
                    },
                ],
            },
        ]
        for payload in bad_payloads:
            for api_format in ("openai_chat", "openai_responses"):
                with self.subTest(payload=payload, api_format=api_format), self.assertRaises(
                    protocol.ProtocolTransformError
                ):
                    protocol.transform_request(payload, api_format)

    def test_all_anthropic_messages_require_user_or_assistant_role(self) -> None:
        payload = {
            "model": "m",
            "messages": [{"role": "tool", "content": "orphan"}],
        }
        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format), self.assertRaisesRegex(
                protocol.ProtocolTransformError, "roles"
            ):
                protocol.transform_request(payload, api_format)

    def test_chat_terminal_accepts_usage_tail_and_rejects_late_content(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        bridge.feed(
            "message",
            json.dumps(
                {"choices": [{"delta": {}, "finish_reason": "stop"}]}
            ),
        )
        usage_tail = bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 13, "completion_tokens": 5},
                }
            ),
        )
        self.assertEqual(usage_tail, [])
        self.assertEqual((bridge.input_tokens, bridge.output_tokens), (13, 5))
        with self.assertRaisesRegex(
            protocol.ProtocolTransformError, "after finish_reason"
        ):
            bridge.feed(
                "message",
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {"content": "late"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
        terminal = b"".join(bridge.finish())
        self.assertIn(b'"output_tokens":5', terminal)

    def test_chat_finish_reason_requires_nonempty_string(self) -> None:
        for finish_reason in (False, 0, "", {}):
            bridge = protocol.AnthropicStreamBridge("openai_chat")
            with self.subTest(finish_reason=finish_reason), self.assertRaisesRegex(
                protocol.ProtocolTransformError, "finish_reason"
            ):
                bridge.feed(
                    "message",
                    json.dumps(
                        {
                            "choices": [
                                {"delta": {}, "finish_reason": finish_reason}
                            ]
                        }
                    ),
                )

    def test_chat_text_starts_after_reasoning_block_stops(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        chunks = bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "think",
                                "content": "answer",
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
        )
        events = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
        ]

        self.assertEqual(
            [
                (event["type"], event.get("index"))
                for event in events
                if event["type"] != "message_start"
            ],
            [
                ("content_block_start", 0),
                ("content_block_delta", 0),
                ("content_block_stop", 0),
                ("content_block_start", 1),
                ("content_block_delta", 1),
            ],
        )

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

    def test_chat_text_after_tool_starts_a_fresh_valid_content_block(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        chunks = []
        for payload in (
            {"choices": [{"delta": {"content": "before"}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "lookup"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {"content": "after"}, "finish_reason": None}]},
        ):
            chunks.extend(bridge.feed("message", json.dumps(payload)))
        events = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
        ]
        starts = [event["index"] for event in events if event["type"] == "content_block_start"]
        deltas = [event["index"] for event in events if event["type"] == "content_block_delta"]
        stops = [event["index"] for event in events if event["type"] == "content_block_stop"]
        self.assertEqual(starts, [0, 1, 2])
        self.assertEqual(deltas, [0, 2])
        self.assertEqual(stops, [0, 1])

    def test_responses_done_only_text_is_streamed_once(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_text.done",
            json.dumps(
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "done only",
                }
            ),
        )
        deltas = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
            if b'"type":"content_block_delta"' in chunk
        ]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["delta"]["text"], "done only")

    def test_responses_done_after_delta_does_not_duplicate_text(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "partial",
                }
            ),
        )
        chunks.extend(
            bridge.feed(
                "response.output_text.done",
                json.dumps(
                    {
                        "type": "response.output_text.done",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "partial",
                    }
                ),
            )
        )
        deltas = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
            if b'"type":"content_block_delta"' in chunk
        ]
        self.assertEqual([delta["delta"]["text"] for delta in deltas], ["partial"])

    def test_responses_zero_output_index_is_a_tool_identifier(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.added",
            json.dumps(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "function_call", "name": "lookup"},
                }
            ),
        )
        chunks.extend(
            bridge.feed(
                "response.function_call_arguments.done",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 0,
                        "arguments": '{"q":"zero"}',
                    }
                ),
            )
        )
        events = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
        ]

        tool = next(
            event["content_block"]
            for event in events
            if event["type"] == "content_block_start"
        )
        self.assertEqual(tool["id"], "0")
        self.assertEqual(
            [
                event["delta"]["partial_json"]
                for event in events
                if event["type"] == "content_block_delta"
            ],
            ['{"q":"zero"}'],
        )

    def test_responses_anonymous_tool_snapshots_stay_distinct(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = []
        for name, arguments in (
            ("first", '{"value":1}'),
            ("second", '{"value":2}'),
        ):
            chunks.extend(
                bridge.feed(
                    "response.output_item.added",
                    json.dumps(
                        {
                            "type": "response.output_item.added",
                            "item": {
                                "type": "function_call",
                                "name": name,
                                "arguments": arguments,
                            },
                        }
                    ),
                )
            )
        chunks.extend(
            bridge.feed(
                "response.function_call_arguments.delta",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.delta",
                        "delta": "unidentifiable",
                    }
                ),
            )
        )
        events = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
        ]

        self.assertEqual(
            [
                (event["index"], event["content_block"]["id"])
                for event in events
                if event["type"] == "content_block_start"
            ],
            [
                (0, "response_function_call_0"),
                (1, "response_function_call_1"),
            ],
        )
        self.assertEqual(
            [
                (event["index"], event["delta"]["partial_json"])
                for event in events
                if event["type"] == "content_block_delta"
            ],
            [(0, '{"value":1}'), (1, '{"value":2}')],
        )

    def test_responses_single_anonymous_tool_accepts_argument_events(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.added",
            json.dumps(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "name": "lookup",
                    },
                }
            ),
        )
        for delta in ('{"q":', '"x"}'):
            chunks.extend(
                bridge.feed(
                    "response.function_call_arguments.delta",
                    json.dumps(
                        {
                            "type": "response.function_call_arguments.delta",
                            "delta": delta,
                        }
                    ),
                )
            )
        chunks.extend(
            bridge.feed(
                "response.function_call_arguments.done",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.done",
                        "arguments": '{"q":"x"}',
                    }
                ),
            )
        )
        events = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
        ]

        self.assertEqual(
            [
                event["delta"]["partial_json"]
                for event in events
                if event["type"] == "content_block_delta"
            ],
            ['{"q":', '"x"}'],
        )
        self.assertEqual(
            [event["index"] for event in events if event["type"] == "content_block_stop"],
            [0],
        )

    def test_responses_tool_arguments_done_accepts_call_id_and_arguments(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.added",
            json.dumps(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "lookup",
                    },
                }
            ),
        )
        chunks.extend(
            bridge.feed(
                "response.function_call_arguments.done",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.done",
                        "call_id": "call_1",
                        "arguments": '{"q":"x"}',
                    }
                ),
            )
        )
        events = [
            json.loads(chunk.decode().split("\ndata: ", 1)[1])
            for chunk in chunks
        ]
        self.assertEqual(
            [event["delta"]["partial_json"] for event in events if event["type"] == "content_block_delta"],
            ['{"q":"x"}'],
        )
        self.assertEqual(
            [event["index"] for event in events if event["type"] == "content_block_stop"],
            [0],
        )


if __name__ == "__main__":
    unittest.main()
