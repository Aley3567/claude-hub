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


class CanonicalRequestContractTests(unittest.TestCase):
    def test_native_system_role_is_promoted_without_losing_metadata_or_extensions(self) -> None:
        payload = {
            "model": "strict-anthropic-model",
            "system": [
                {
                    "type": "text",
                    "text": "top-level",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "machine context",
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                },
                {"role": "user", "content": "hello"},
            ],
            "future_native_extension": {"opaque": True},
        }

        prepared = protocol.prepare_request(payload, "anthropic")

        self.assertEqual(prepared.endpoint, "/v1/messages")
        self.assertEqual(
            prepared.payload["system"],
            [
                {
                    "type": "text",
                    "text": "top-level",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": "machine context",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
            ],
        )
        self.assertEqual(prepared.payload["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(prepared.payload["future_native_extension"], {"opaque": True})
        self.assertIn("HUB_DEGRADE_SYSTEM_ROLE_PROMOTED", prepared.plan.warning_codes)
        self.assertEqual(payload["messages"][0]["role"], "system")


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
                    with self.assertRaises(protocol.ProtocolRequestError) as caught:
                        protocol.transform_request(payload, api_format)
                    exc = caught.exception
                    self.assertEqual(exc.code, "HUB_INVALID_TOOL_CAUSALITY")
                    self.assertEqual(exc.http_status, 400)
                    self.assertEqual(exc.phase, "request")
                    self.assertTrue(exc.path)

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
                                "content": '{"ok":true}',
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

    def test_empty_content_arrays_fail_closed_for_both_formats(self) -> None:
        payload = {
            "model": "empty-content-model",
            "messages": [{"role": "user", "content": []}],
        }
        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolRequestError) as caught:
                    protocol.transform_request(payload, api_format)
                exc = caught.exception
                self.assertEqual(exc.code, "HUB_INVALID_CONTENT_BLOCK")
                self.assertEqual(exc.http_status, 400)
                self.assertEqual(exc.path, "$.messages[0].content")

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
    def test_response_wrapper_terminal_conflicts_fail_closed(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "choices": [
                        {
                            "index": 1,
                            "message": {"content": "alternate"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                "openai_chat",
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_MULTI_CHOICE_UNSUPPORTED",
        )

        for status, details in (
            ("completed", None),
            ("incomplete", {"reason": "max_output_tokens"}),
        ):
            with self.subTest(status=status):
                body = {
                    "status": status,
                    "output": [],
                    "error": {"code": "upstream_failed"},
                }
                if details is not None:
                    body["incomplete_details"] = details
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, "openai_responses")
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_RESPONSE_INVALID",
                )

        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "index": 1,
                                        "id": "call_wrong_index",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                "openai_chat",
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_TOOL_CALL_INVALID",
        )

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
            },
        )

    def test_explicit_truncation_or_refusal_cannot_be_masked_by_tool_output(self) -> None:
        for finish_reason in ("length", "content_filter"):
            with self.subTest(api_format="openai_chat", reason=finish_reason):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "content": "",
                                        "tool_calls": [
                                            {
                                                "id": "call_conflict",
                                                "type": "function",
                                                "function": {
                                                    "name": "lookup",
                                                    "arguments": "{}",
                                                },
                                            }
                                        ],
                                    },
                                    "finish_reason": finish_reason,
                                }
                            ]
                        },
                        "openai_chat",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
                )

        for incomplete_reason in ("max_output_tokens", "content_filter"):
            with self.subTest(
                api_format="openai_responses",
                reason=incomplete_reason,
            ):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "status": "incomplete",
                            "incomplete_details": {"reason": incomplete_reason},
                            "output": [
                                {
                                    "type": "function_call",
                                    "call_id": "call_conflict",
                                    "name": "lookup",
                                    "status": "completed",
                                    "arguments": "{}",
                                }
                            ],
                        },
                        "openai_responses",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
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
                "status": "completed",
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
    def test_responses_non_tool_output_item_added_requires_an_output_index(self) -> None:
        for item in (
            {
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
            {
                "type": "reasoning",
                "status": "in_progress",
                "summary": [],
                "content": [],
            },
        ):
            with self.subTest(item_type=item["type"]):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.output_item.added",
                        json.dumps(
                            {
                                "type": "response.output_item.added",
                                "item": item,
                            }
                        ),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_SSE_ORDER_VIOLATION",
                )

    def _feed_responses_item(self, bridge, event: str, payload: dict) -> None:
        bridge.feed(event, json.dumps({"type": event, **payload}))

    def test_response_output_item_registries_are_bounded(self) -> None:
        item = {
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
        }
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        for index in range(protocol.MAX_STREAM_BLOCKS):
            bridge.response_output_items[str(index)] = ("message", None)
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            self._feed_responses_item(
                bridge,
                "response.output_item.added",
                {"output_index": protocol.MAX_STREAM_BLOCKS, "item": item},
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_TOO_MANY_BLOCKS")
        self.assertEqual(raised.exception.path, "$.output_index")

        bridge = protocol.AnthropicStreamBridge("openai_responses")
        for index in range(protocol.MAX_STREAM_BLOCKS):
            bridge.response_output_item_ids[f"item_{index}"] = str(index)
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            self._feed_responses_item(
                bridge,
                "response.output_item.added",
                {
                    "output_index": 0,
                    "item": {**item, "id": "item_overflow"},
                },
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_TOO_MANY_BLOCKS")
        self.assertEqual(raised.exception.path, "$.item.id")

    def test_response_output_item_id_length_is_bounded(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            self._feed_responses_item(
                bridge,
                "response.output_item.added",
                {
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "x" * (protocol.MAX_STREAM_ITEM_ID_CHARS + 1),
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            )
        self.assertEqual(
            raised.exception.code, "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED"
        )
        self.assertEqual(raised.exception.path, "$.item.id")

    def test_response_redacted_snapshots_are_bounded(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        for index in range(protocol.MAX_STREAM_BLOCKS):
            bridge.response_redacted_snapshots[f"output:prefill_{index}"] = "enc"
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            self._feed_responses_item(
                bridge,
                "response.output_item.done",
                {
                    "output_index": 0,
                    "item": {
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "encrypted-snapshot",
                    },
                },
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_TOO_MANY_BLOCKS")
        self.assertEqual(raised.exception.path, "$.item.encrypted_content")

    def test_chat_stream_rejects_a_nonzero_choice_index(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            bridge.feed(
                "message",
                json.dumps(
                    {
                        "choices": [
                            {
                                "index": 1,
                                "delta": {"content": "alternate"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_MULTI_CHOICE_UNSUPPORTED",
        )

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
        self.assertEqual(
            protocol.SSEParser(max_buffer=8).feed(b"data: 12\n\n"),
            [("message", "12")],
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.SSEParser(max_buffer=8).feed(b"data: 123456789\n\n")
        self.assertEqual(raised.exception.code, "HUB_SSE_EVENT_TOO_LARGE")

        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.SSEParser(max_buffer=8).feed(b"123456789")
        self.assertEqual(raised.exception.code, "HUB_SSE_EVENT_TOO_LARGE")

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

    def test_responses_tool_argument_snapshot_only_emits_missing_suffix(self) -> None:
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
            ['{"q":', '"x"}'],
        )

    def test_response_terminal_rejects_late_content(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        bridge.feed(
            "response.completed",
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {"status": "completed"},
                }
            ),
        )
        with self.assertRaisesRegex(
            protocol.ProtocolTransformError, "after terminal"
        ):
            bridge.feed(
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
        ) as raised:
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
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")
        terminal = b"".join(bridge.finish())
        self.assertIn(b'"output_tokens":5', terminal)

    def test_chat_finish_reason_requires_nonempty_string(self) -> None:
        for finish_reason in (False, 0, "", {}):
            bridge = protocol.AnthropicStreamBridge("openai_chat")
            with self.subTest(finish_reason=finish_reason), self.assertRaisesRegex(
                protocol.ProtocolTransformError, "finish_reason"
            ) as raised:
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
            self.assertEqual(
                raised.exception.code, "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE"
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
                                    "function": {
                                        "name": "lookup",
                                        "arguments": "{}",
                                    },
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
        self.assertEqual(deltas, [0, 1, 2])
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
        self.assertIn(
            "HUB_DEGRADE_SYNTHETIC_TOOL_ID",
            bridge.warning_codes,
        )

        strict = protocol.AnthropicStreamBridge(
            "openai_responses",
            compatibility_mode="strict",
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            strict.feed(
                "response.output_item.added",
                json.dumps(
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {"type": "function_call", "name": "lookup"},
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_CALL_INVALID")

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
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            bridge.feed(
                "response.function_call_arguments.delta",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.delta",
                        "delta": "unidentifiable",
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")
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


class UsageReceiptTests(unittest.TestCase):
    """Usage evidence → canonical usage：complete/stream 共用一套规则。"""

    def test_complete_anthropic_usage_passes_through_unchanged(self) -> None:
        receipt = protocol.UsageReceipt.from_upstream(
            {
                "input_tokens": 120,
                "output_tokens": 34,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 60,
            }
        )
        self.assertEqual(
            receipt.as_anthropic(),
            {
                "input_tokens": 120,
                "output_tokens": 34,
                "cache_read_input_tokens": 500,
                "cache_creation_input_tokens": 60,
            },
        )
        self.assertEqual(receipt.source, "upstream")

    def test_nested_openai_carrier_proves_inclusive_base(self) -> None:
        receipt = protocol.UsageReceipt.from_upstream(
            {
                "prompt_tokens": 620,
                "completion_tokens": 34,
                "prompt_tokens_details": {"cached_tokens": 500},
            },
            input_key="prompt_tokens",
            output_key="completion_tokens",
        )
        # 标准 nested carrier 有证据表明 base 已包含 cached tokens，
        # 转 Anthropic 语义时必须排除。
        self.assertEqual(receipt.input_tokens, 120)
        self.assertEqual(receipt.cache_read, 500)

    def test_top_level_compat_cache_field_alone_is_ambiguous(self) -> None:
        receipt = protocol.UsageReceipt.from_upstream(
            {
                "prompt_tokens": 620,
                "completion_tokens": 34,
                "cache_read_tokens": 500,
            },
            input_key="prompt_tokens",
            output_key="completion_tokens",
        )
        # 顶层兼容字段无法单独证明 base 是 inclusive 还是 split：
        # 既不从 base 扣减，也不产出 cache-read —— 拒绝猜测。
        self.assertIsNone(receipt.cache_read)
        self.assertEqual(receipt.input_tokens, 620)

    def test_complete_conversion_omits_unobserved_usage_counters(self) -> None:
        body = protocol.transform_response(
            {
                "id": "chatcmpl-1",
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
            },
            "openai_chat",
        )
        # 上游没报 base usage：schema 完整性要求发 0，但 provenance
        # 降级必须可观测（记账侧据此剥掉这些字段）。
        self.assertEqual(body["usage"]["input_tokens"], 0)
        self.assertEqual(body["usage"]["output_tokens"], 0)
        self.assertIn(
            "HUB_USAGE_PROVENANCE_UNAVAILABLE",
            protocol.prepare_response(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                "openai_chat",
            ).plan.warning_codes,
        )

    def test_responses_conversion_omits_unobserved_usage_counters(self) -> None:
        body = protocol.transform_response(
            {
                "id": "resp_2",
                "model": "responses-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "answer"}],
                    }
                ],
            },
            "openai_responses",
        )
        # Responses 上游同样：0 + provenance 降级可观测。
        self.assertEqual(body["usage"]["input_tokens"], 0)
        self.assertEqual(body["usage"]["output_tokens"], 0)

    def test_conflicting_cache_read_carriers_are_rejected(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as ctx:
            protocol.UsageReceipt.from_upstream(
                {
                    "prompt_tokens": 620,
                    "completion_tokens": 34,
                    "prompt_tokens_details": {"cached_tokens": 500},
                    "cache_read_input_tokens": 480,
                },
                input_key="prompt_tokens",
                output_key="completion_tokens",
            )
        self.assertEqual(
            ctx.exception.code, "HUB_UPSTREAM_USAGE_INVALID"
        )

    def test_chat_stream_applies_same_cache_evidence_rule_as_complete(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        bridge.feed(
            "message",
            json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        )
        bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 40},
                    },
                }
            ),
        )
        events = [line for line in b"".join(bridge.finish()).splitlines()]
        delta_usage = None
        for line in events:
            if line.startswith(b"data:"):
                payload = json.loads(line[5:].strip())
                if payload.get("type") == "message_delta":
                    delta_usage = payload.get("usage")
        # complete 转换对同一输入产出 input_tokens=60（nested 证据扣 40），
        # stream 必须共用同一规则，不能保留 inclusive base 造成双计。
        self.assertIsNotNone(delta_usage)
        self.assertEqual(delta_usage.get("input_tokens"), 60)
        self.assertEqual(delta_usage.get("cache_read_input_tokens"), 40)

    def _delta_usage(self, chunks) -> dict:
        """取 message_delta 事件里的 usage。"""
        usage: dict = {}
        for line in b"".join(chunks).splitlines():
            if not line.startswith(b"data:"):
                continue
            payload = json.loads(line[5:].strip())
            if payload.get("type") == "message_delta":
                usage = payload.get("usage") or {}
        return usage

    def _finish_chat_stream(self, usage: dict):
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        bridge.feed(
            "message",
            json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        )
        bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": usage,
                }
            ),
        )
        return bridge, self._delta_usage(bridge.finish())

    def test_stream_accounting_view_matches_downstream_usage(self) -> None:
        bridge, delta_usage = self._finish_chat_stream(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        )
        # 记账必须读下游同一张 receipt。曾经直接读累计属性，于是同一次流
        # 下游拿到扣减后的 60，账本却记 inclusive 的 100 外加 cache_read 40。
        self.assertEqual(delta_usage.get("input_tokens"), 60)
        self.assertEqual(bridge.usage_for_accounting().get("input_tokens"), 60)
        # 扣减只发生在导出路径，累计属性保持上游原值，不可直接用于记账。
        self.assertEqual(bridge.input_tokens, 100)

    def test_stream_accounting_view_drops_ambiguous_cache_carrier(self) -> None:
        bridge, delta_usage = self._finish_chat_stream(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cache_read_tokens": 40,
            }
        )
        accounting = bridge.usage_for_accounting()
        # 顶层兼容字段证明不了 base 是否 inclusive，下游已经拒猜，
        # 账本同样不能把这 40 记进去。
        self.assertNotIn("cache_read_input_tokens", delta_usage)
        self.assertNotIn("cache_read_input_tokens", accounting)
        self.assertEqual(accounting.get("input_tokens"), 100)

    def test_stream_accounting_view_omits_unobserved_counters(self) -> None:
        bridge, _ = self._finish_chat_stream({"completion_tokens": 20})
        accounting = bridge.usage_for_accounting()
        # 未观测的计数器省略而不是伪造 0，记账侧无需再靠 plan 反查剥零。
        self.assertNotIn("input_tokens", accounting)
        self.assertEqual(accounting.get("output_tokens"), 20)

    def test_stream_mixed_cache_evidence_nested_carrier_wins(self) -> None:
        bridge, delta_usage = self._finish_chat_stream(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
                "cache_read_input_tokens": 40,
            }
        )
        # nested carrier 与官方 key 并存且数值一致时按 nested（inclusive）
        # 解读：base 扣掉 40。complete 路径对同输入产出相同结果（见
        # test_matching_cache_carriers_are_coalesced_without_fabrication）；
        # 数值冲突则整体拒转（见 test_conflicting_cache_read_carriers_are_rejected）。
        self.assertEqual(delta_usage.get("input_tokens"), 60)
        self.assertEqual(delta_usage.get("cache_read_input_tokens"), 40)
        self.assertEqual(bridge.usage_for_accounting().get("input_tokens"), 60)
