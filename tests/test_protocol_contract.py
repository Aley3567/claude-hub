from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import claude1_protocol as protocol


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "anthropic_protocol" / "request"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


class RequestCapabilityContractTests(unittest.TestCase):
    def test_claude_code_system_role_golden_contract_for_all_adapters(self) -> None:
        source = _fixture("claude_code_system_role.input.json")
        for api_format in ("anthropic", "openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                prepared = protocol.prepare_request(source, api_format)
                expected = _fixture(
                    f"claude_code_system_role.{api_format}.golden.json"
                )
                self.assertEqual(prepared.payload, expected)

    def test_empty_system_role_content_is_rejected_by_all_adapters(self) -> None:
        for content in (None, []):
            payload = {
                "model": "fixture-model",
                "messages": [{"role": "system", "content": content}],
            }
            for api_format in (
                "anthropic",
                "openai_chat",
                "openai_responses",
            ):
                for compatibility_mode in ("visible_lossy", "strict"):
                    with self.subTest(
                        api_format=api_format,
                        content=content,
                        compatibility_mode=compatibility_mode,
                    ):
                        with self.assertRaises(
                            protocol.ProtocolRequestError
                        ) as raised:
                            protocol.prepare_request(
                                payload,
                                api_format,
                                compatibility_mode=compatibility_mode,
                            )
                        self.assertEqual(
                            raised.exception.code,
                            "HUB_INVALID_SYSTEM_BLOCK",
                        )
                        self.assertEqual(
                            raised.exception.path,
                            "$.messages[0].content",
                        )

    def test_system_role_requires_nonempty_text_blocks(self) -> None:
        for content in (
            "",
            [{"type": "text", "text": ""}],
            [
                {"type": "text", "text": "valid"},
                {"type": "text", "text": ""},
            ],
        ):
            payload = {
                "model": "fixture-model",
                "messages": [{"role": "system", "content": content}],
            }
            for api_format in (
                "anthropic",
                "openai_chat",
                "openai_responses",
            ):
                with self.subTest(api_format=api_format, content=content):
                    with self.assertRaises(protocol.ProtocolRequestError) as raised:
                        protocol.prepare_request(payload, api_format)
                    self.assertEqual(
                        raised.exception.code,
                        "HUB_INVALID_SYSTEM_BLOCK",
                    )

    def test_capability_matrix_keeps_gemini_reserved_as_an_independent_adapter(self) -> None:
        matrix = protocol.protocol_capability_matrix()
        self.assertEqual(matrix["system_text"]["anthropic"], "exact")
        self.assertEqual(
            matrix["document"]["openai_chat"],
            "observable_degradation",
        )
        self.assertEqual(matrix["server_tool"]["openai_responses"], "reject")
        self.assertEqual(
            matrix["tool_search"]["openai_responses"],
            "observable_degradation",
        )
        self.assertEqual(matrix["client_tool"]["gemini_generate_content"], "reject")
        self.assertEqual(
            matrix["tool_result"]["openai_chat"],
            "observable_degradation",
        )
        self.assertEqual(matrix["tool_result_text"]["openai_chat"], "exact")
        self.assertEqual(
            matrix["thinking_signature"]["openai_responses"],
            "observable_degradation",
        )
        self.assertEqual(matrix["redacted_thinking"]["openai_responses"], "reject")
        self.assertEqual(
            matrix["responses_provenanced_redacted_thinking"][
                "openai_responses"
            ],
            "exact",
        )
        self.assertEqual(matrix["stop_sequences"]["openai_chat"], "exact")
        self.assertEqual(
            matrix["stop_sequences"]["openai_responses"],
            "observable_degradation",
        )
        self.assertEqual(
            matrix["usage_base"]["openai_chat"],
            "observable_degradation",
        )
        self.assertEqual(matrix["usage_base_present"]["openai_chat"], "exact")

        payload = {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        with self.assertRaises(protocol.ProtocolRequestError) as unavailable:
            protocol.prepare_request(payload, "gemini_generate_content")
        self.assertEqual(unavailable.exception.code, "HUB_ADAPTER_UNAVAILABLE")
        with self.assertRaises(protocol.ProtocolRequestError) as unknown:
            protocol.prepare_request(payload, "future_protocol")
        self.assertEqual(unknown.exception.code, "HUB_API_FORMAT_UNSUPPORTED")

    def test_unknown_content_block_is_native_transparent_and_cross_protocol_rejected(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "future_native_block",
                            "opaque": {"version": 1},
                        }
                    ],
                }
            ],
        }

        native = protocol.prepare_request(payload, "anthropic")
        self.assertEqual(native.payload, payload)

        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolRequestError) as raised:
                    protocol.prepare_request(payload, api_format)
                self.assertEqual(
                    raised.exception.code, "HUB_UNSUPPORTED_CONTENT_BLOCK"
                )
                self.assertEqual(
                    raised.exception.path, "$.messages[0].content[0]"
                )

    def test_unknown_field_inside_known_block_is_not_silently_dropped(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "hello",
                            "future_block_field": {"opaque": True},
                        }
                    ],
                }
            ],
        }
        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolRequestError) as raised:
                    protocol.prepare_request(payload, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UNSUPPORTED_CONTENT_FIELD",
                )
                self.assertEqual(
                    raised.exception.path,
                    "$.messages[0].content[0].future_block_field",
                )

    def test_nested_and_message_fields_cannot_bypass_the_canonical_ir(self) -> None:
        cases = (
            (
                {
                    "model": "fixture-model",
                    "messages": [
                        {
                            "role": "user",
                            "content": "hello",
                            "future_message_field": True,
                        }
                    ],
                },
                "HUB_UNSUPPORTED_MESSAGE_FIELD",
                "$.messages[0].future_message_field",
            ),
            (
                {
                    "model": "fixture-model",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": "ZmFrZQ==",
                                        "future_source_field": True,
                                    },
                                }
                            ],
                        }
                    ],
                },
                "HUB_UNSUPPORTED_CONTENT_FIELD",
                "$.messages[0].content[0].source.future_source_field",
            ),
            (
                {
                    "model": "fixture-model",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "document",
                                    "source": {
                                        "type": "content",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "document body",
                                                "future_nested_field": True,
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    ],
                },
                "HUB_UNSUPPORTED_CONTENT_FIELD",
                "$.messages[0].content[0].source.content[0].future_nested_field",
            ),
            (
                {
                    "model": "fixture-model",
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
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call_1",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "answer",
                                            "future_nested_field": True,
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
                "HUB_UNSUPPORTED_CONTENT_FIELD",
                "$.messages[1].content[0].content[0].future_nested_field",
            ),
            (
                {
                    "model": "fixture-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "tools": [
                        {
                            "name": "lookup",
                            "input_schema": {"type": "object"},
                            "future_tool_field": True,
                        }
                    ],
                },
                "HUB_UNSUPPORTED_TOOL_FIELD",
                "$.tools[0].future_tool_field",
            ),
        )
        for payload, expected_code, expected_path in cases:
            for api_format in ("openai_chat", "openai_responses"):
                with self.subTest(api_format=api_format, path=expected_path):
                    with self.assertRaises(protocol.ProtocolRequestError) as raised:
                        protocol.prepare_request(payload, api_format)
                    self.assertEqual(raised.exception.code, expected_code)
                    self.assertEqual(raised.exception.path, expected_path)

    def test_system_block_metadata_is_preserved_in_ir_and_visibly_degraded(self) -> None:
        payload = {
            "model": "fixture-model",
            "system": [
                {
                    "type": "text",
                    "text": "cached context",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }
            ],
            "messages": [{"role": "user", "content": "hello"}],
        }

        chat = protocol.prepare_request(payload, "openai_chat")
        responses = protocol.prepare_request(payload, "openai_responses")

        self.assertEqual(chat.payload["messages"][0]["content"], "cached context")
        self.assertEqual(responses.payload["instructions"], "cached context")
        for prepared in (chat, responses):
            self.assertIn(
                "HUB_DEGRADE_SYSTEM_METADATA_DROPPED",
                prepared.plan.warning_codes,
            )

        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolRequestError) as raised:
                    protocol.prepare_request(
                        payload,
                        api_format,
                        compatibility_mode="strict",
                    )
                self.assertEqual(
                    raised.exception.code, "HUB_UNSUPPORTED_SYSTEM_METADATA"
                )
                self.assertEqual(raised.exception.path, "$.system[0].cache_control")

    def test_nested_tool_result_and_is_error_use_a_lossless_visible_envelope(self) -> None:
        nested = [
            {"type": "text", "text": "failed"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "ZmFrZQ==",
                },
            },
            {
                "type": "document",
                "source": {"type": "text", "media_type": "text/plain", "data": "details"},
            },
        ]
        payload = {
            "model": "fixture-model",
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
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "content": nested,
                            "is_error": True,
                        }
                    ],
                },
            ],
        }

        chat = protocol.prepare_request(payload, "openai_chat")
        responses = protocol.prepare_request(payload, "openai_responses")

        chat_envelope = json.loads(
            next(message for message in chat.payload["messages"] if message["role"] == "tool")[
                "content"
            ]
        )
        responses_envelope = json.loads(
            next(
                item
                for item in responses.payload["input"]
                if item.get("type") == "function_call_output"
            )["output"]
        )
        expected = {
            "type": "anthropic_tool_result",
            "is_error": True,
            "content": nested,
        }
        self.assertEqual(chat_envelope, expected)
        self.assertEqual(responses_envelope, expected)
        for prepared in (chat, responses):
            self.assertIn(
                "HUB_DEGRADE_TOOL_RESULT_CONTENT_ENVELOPED",
                prepared.plan.warning_codes,
            )
            self.assertIn(
                "HUB_DEGRADE_TOOL_RESULT_ERROR_ENVELOPED",
                prepared.plan.warning_codes,
            )

    def test_tool_result_content_requires_text_or_content_blocks(self) -> None:
        for invalid_content in ({"hidden": True}, 7, None, False):
            payload = {
                "model": "fixture-model",
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
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": invalid_content,
                            }
                        ],
                    },
                ],
            }
            for api_format in ("openai_chat", "openai_responses"):
                with self.subTest(
                    api_format=api_format,
                    invalid_content=invalid_content,
                ):
                    with self.assertRaises(protocol.ProtocolRequestError) as raised:
                        protocol.prepare_request(payload, api_format)
                    self.assertEqual(
                        raised.exception.code,
                        "HUB_INVALID_TOOL_RESULT_CONTENT",
                    )
                    self.assertEqual(
                        raised.exception.path,
                        "$.messages[1].content[0].content",
                    )

    def test_document_search_result_and_citations_degrade_to_provenance_text(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "title": "Guide",
                            "source": {
                                "type": "text",
                                "media_type": "text/plain",
                                "data": "document body",
                            },
                            "citations": {"enabled": True},
                        },
                        {
                            "type": "document",
                            "title": "Scanned PDF",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": "JVBERg==",
                            },
                        },
                        {
                            "type": "search_result",
                            "title": "Result title",
                            "source": "https://example.test/source",
                            "content": [{"type": "text", "text": "search body"}],
                        },
                        {
                            "type": "text",
                            "text": "quoted claim",
                            "citations": [{"type": "future_citation", "opaque": "real-upstream-value"}],
                        },
                    ],
                }
            ],
        }

        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                prepared = protocol.prepare_request(payload, api_format)
                rendered = json.dumps(prepared.payload, ensure_ascii=False)
                self.assertIn("[document title=Guide media_type=text/plain]", rendered)
                self.assertIn("document body", rendered)
                self.assertIn("[document placeholder title=Scanned PDF media_type=application/pdf source=base64]", rendered)
                self.assertIn(
                    "[search_result title=Result title source=https://example.test/source]",
                    rendered,
                )
                self.assertIn("search body", rendered)
                self.assertIn("quoted claim", rendered)
                self.assertNotIn("real-upstream-value", rendered)
                self.assertTrue(
                    {
                        "HUB_DEGRADE_DOCUMENT_TEXT_EXTRACTED",
                        "HUB_DEGRADE_DOCUMENT_PLACEHOLDER",
                        "HUB_DEGRADE_SEARCH_RESULT_TEXTIFIED",
                        "HUB_DEGRADE_CITATION_METADATA_DROPPED",
                    }.issubset(set(prepared.plan.warning_codes))
                )

    def test_nested_document_and_search_text_metadata_is_observably_classified(self) -> None:
        for block in (
            {
                "type": "document",
                "source": {
                    "type": "content",
                    "content": [
                        {
                            "type": "text",
                            "text": "document body",
                            "citations": [{"type": "fixture"}],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
            },
            {
                "type": "search_result",
                "source": "fixture",
                "title": "result",
                "content": [
                    {
                        "type": "text",
                        "text": "search body",
                        "citations": [{"type": "fixture"}],
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
        ):
            payload = {
                "model": "fixture-model",
                "messages": [{"role": "user", "content": [block]}],
            }
            for api_format in ("openai_chat", "openai_responses"):
                with self.subTest(api_format=api_format, block=block["type"]):
                    prepared = protocol.prepare_request(payload, api_format)
                    self.assertIn(
                        "HUB_DEGRADE_CITATION_METADATA_DROPPED",
                        prepared.plan.warning_codes,
                    )
                    self.assertIn(
                        "HUB_DEGRADE_CONTENT_METADATA_DROPPED",
                        prepared.plan.warning_codes,
                    )
                    with self.assertRaises(protocol.ProtocolRequestError):
                        protocol.prepare_request(
                            payload,
                            api_format,
                            compatibility_mode="strict",
                        )

    def test_server_mcp_tool_search_and_code_execution_tools_are_capability_gated(self) -> None:
        cases = {
            "web_search_20250305": "HUB_UNSUPPORTED_SERVER_TOOL",
            "code_execution_20250825": "HUB_UNSUPPORTED_SERVER_TOOL",
            "mcp_toolset": "HUB_UNSUPPORTED_MCP",
            "future_tool_kind": "HUB_UNSUPPORTED_TOOL_TYPE",
        }
        for tool_type, expected_code in cases.items():
            payload = {
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [
                    {
                        "type": tool_type,
                        "name": "server_side",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
            for api_format in ("openai_chat", "openai_responses"):
                with self.subTest(tool_type=tool_type, api_format=api_format):
                    with self.assertRaises(protocol.ProtocolRequestError) as raised:
                        protocol.prepare_request(payload, api_format)
                    self.assertEqual(raised.exception.code, expected_code)
                    self.assertEqual(raised.exception.path, "$.tools[0]")

        native_payload = {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }
        self.assertEqual(
            protocol.prepare_request(native_payload, "anthropic").payload,
            native_payload,
        )

    def test_deferred_tools_are_eagerly_loaded_and_native_search_control_is_omitted(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "name": "deferred_fixture",
                    "description": "fixture tool",
                    "input_schema": {"type": "object"},
                    "defer_loading": True,
                },
                {
                    "type": "tool_search_tool_regex_20251119",
                    "name": "tool_search",
                },
            ],
        }
        expected_warnings = {
            "HUB_DEGRADE_DEFERRED_TOOL_EAGERLY_LOADED",
            "HUB_DEGRADE_TOOL_SEARCH_OMITTED",
        }
        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                prepared = protocol.prepare_request(payload, api_format)
                self.assertTrue(
                    expected_warnings.issubset(set(prepared.plan.warning_codes))
                )
                self.assertEqual(len(prepared.payload["tools"]), 1)
                self.assertNotIn(
                    "defer_loading",
                    json.dumps(prepared.payload["tools"]),
                )
                self.assertIn("deferred_fixture", json.dumps(prepared.payload["tools"]))
                with self.assertRaises(protocol.ProtocolRequestError) as raised:
                    protocol.prepare_request(
                        payload,
                        api_format,
                        compatibility_mode="strict",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UNSUPPORTED_TOOL_SEARCH",
                )
                self.assertEqual(
                    raised.exception.path,
                    "$.tools[0].defer_loading",
                )
        self.assertEqual(
            protocol.prepare_request(payload, "anthropic").payload,
            payload,
        )

    def test_request_controls_are_mapped_or_visibly_degraded(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"request_group": "fixture"},
            "service_tier": "auto",
            "cache_control": {"type": "ephemeral"},
            "top_k": 40,
            "output_config": {
                "effort": "high",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "format": "uri"}
                        },
                    },
                },
            },
        }

        chat = protocol.prepare_request(payload, "openai_chat")
        responses = protocol.prepare_request(payload, "openai_responses")

        self.assertNotIn("metadata", chat.payload)
        self.assertIn("HUB_DEGRADE_METADATA_DROPPED", chat.plan.warning_codes)
        self.assertEqual(chat.payload["service_tier"], "auto")
        self.assertEqual(chat.payload["response_format"]["type"], "json_schema")
        self.assertNotIn(
            "format",
            chat.payload["response_format"]["json_schema"]["schema"]["properties"]["url"],
        )
        self.assertEqual(responses.payload["metadata"], payload["metadata"])
        self.assertEqual(responses.payload["service_tier"], "auto")
        self.assertEqual(responses.payload["text"]["format"]["type"], "json_schema")
        for prepared in (chat, responses):
            self.assertNotIn("top_k", prepared.payload)
            self.assertNotIn("cache_control", prepared.payload)
            self.assertTrue(
                {
                    "HUB_DEGRADE_CACHE_CONTROL_DROPPED",
                    "HUB_DEGRADE_TOP_K_DROPPED",
                    "HUB_DEGRADE_SCHEMA_NORMALIZED",
                }.issubset(set(prepared.plan.warning_codes))
            )

        with self.assertRaises(protocol.ProtocolRequestError) as raised:
            protocol.prepare_request(
                {
                    "model": "fixture-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    "metadata": payload["metadata"],
                },
                "openai_chat",
                compatibility_mode="strict",
            )
        self.assertEqual(raised.exception.code, "HUB_UNSUPPORTED_METADATA")
        self.assertEqual(raised.exception.path, "$.metadata")

    def test_tool_strict_parallel_choice_and_thinking_budget_are_explicit(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "name": "lookup",
                    "description": "Lookup a value",
                    "input_schema": {"type": "object"},
                    "strict": True,
                }
            ],
            "tool_choice": {
                "type": "any",
                "disable_parallel_tool_use": True,
            },
            "thinking": {"type": "enabled", "budget_tokens": 8_000},
        }

        chat = protocol.prepare_request(payload, "openai_chat")
        responses = protocol.prepare_request(payload, "openai_responses")

        self.assertIs(chat.payload["tools"][0]["function"]["strict"], True)
        self.assertIs(responses.payload["tools"][0]["strict"], True)
        self.assertEqual(chat.payload["reasoning_effort"], "medium")
        self.assertEqual(responses.payload["reasoning"]["effort"], "medium")
        for prepared in (chat, responses):
            self.assertIs(prepared.payload["parallel_tool_calls"], False)
            self.assertIn(
                "HUB_DEGRADE_THINKING_BUDGET_TO_EFFORT",
                prepared.plan.warning_codes,
            )

    def test_responses_stop_sequences_and_document_context_are_observable(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "title": "Guide",
                            "context": "Only use the release section.",
                            "source": {
                                "type": "text",
                                "media_type": "text/plain",
                                "data": "release body",
                            },
                        }
                    ],
                }
            ],
            "stop_sequences": ["END"],
        }

        chat = protocol.prepare_request(payload, "openai_chat")
        responses = protocol.prepare_request(payload, "openai_responses")
        self.assertEqual(chat.payload["stop"], ["END"])
        self.assertIn("Only use the release section.", json.dumps(chat.payload))
        self.assertNotIn("stop", responses.payload)
        self.assertIn("Only use the release section.", json.dumps(responses.payload))
        self.assertIn(
            "HUB_DEGRADE_DOCUMENT_CONTEXT_TEXTIFIED",
            chat.plan.warning_codes,
        )
        self.assertTrue(
            {
                "HUB_DEGRADE_DOCUMENT_CONTEXT_TEXTIFIED",
                "HUB_DEGRADE_STOP_SEQUENCES_DROPPED",
            }.issubset(set(responses.plan.warning_codes))
        )

    def test_unknown_top_level_request_fields_degrade_by_default_and_strict_rejects(self) -> None:
        cases = {
            "future_request_field": (
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_UNSUPPORTED_REQUEST_FIELD",
            ),
            "context_management": (
                "HUB_DEGRADE_CONTEXT_MANAGEMENT_DROPPED",
                "HUB_UNSUPPORTED_CONTEXT_MANAGEMENT",
            ),
        }
        for field, (warning_code, reject_code) in cases.items():
            payload = {
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "hello"}],
                field: {"opaque": True},
            }
            for api_format in ("openai_chat", "openai_responses"):
                with self.subTest(field=field, api_format=api_format):
                    prepared = protocol.prepare_request(payload, api_format)
                    self.assertIn(warning_code, prepared.plan.warning_codes)
                    self.assertIn(
                        f"{warning_code}@$.{field}",
                        prepared.plan.warning_details,
                    )
                    self.assertNotIn(field, prepared.payload)
                    with self.assertRaises(protocol.ProtocolRequestError) as raised:
                        protocol.prepare_request(
                            payload,
                            api_format,
                            compatibility_mode="strict",
                        )
                    self.assertEqual(raised.exception.code, reject_code)
                    self.assertEqual(raised.exception.path, f"$.{field}")
            self.assertEqual(
                protocol.prepare_request(payload, "anthropic").payload,
                payload,
            )

    def test_execution_request_fields_fail_closed_cross_protocol(self) -> None:
        cases = {
            "container": "HUB_UNSUPPORTED_CONTAINER",
            "inference_geo": "HUB_UNSUPPORTED_INFERENCE_GEO",
            "mcp_servers": "HUB_UNSUPPORTED_MCP",
        }
        for field, expected_code in cases.items():
            payload = {
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "hello"}],
                field: {"opaque": True},
            }
            for api_format in ("openai_chat", "openai_responses"):
                for compatibility_mode in ("visible_lossy", "strict"):
                    with self.subTest(
                        field=field,
                        api_format=api_format,
                        compatibility_mode=compatibility_mode,
                    ):
                        with self.assertRaises(protocol.ProtocolRequestError) as raised:
                            protocol.prepare_request(
                                payload,
                                api_format,
                                compatibility_mode=compatibility_mode,
                            )
                        self.assertEqual(raised.exception.code, expected_code)
                        self.assertEqual(raised.exception.path, f"$.{field}")
            self.assertEqual(
                protocol.prepare_request(payload, "anthropic").payload,
                payload,
            )

    def test_thinking_text_degrades_but_anthropic_signature_is_never_fabricated_or_forwarded(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "private reasoning",
                            "signature": "real-anthropic-signature",
                        }
                    ],
                }
            ],
        }

        chat = protocol.prepare_request(payload, "openai_chat")
        responses = protocol.prepare_request(payload, "openai_responses")

        self.assertEqual(chat.payload["messages"][0]["reasoning_content"], "private reasoning")
        self.assertNotIn("real-anthropic-signature", json.dumps(chat.payload))
        reasoning_item = next(
            item for item in responses.payload["input"] if item.get("type") == "reasoning"
        )
        self.assertEqual(
            reasoning_item["summary"],
            [{"type": "summary_text", "text": "private reasoning"}],
        )
        self.assertNotIn("real-anthropic-signature", json.dumps(responses.payload))
        for prepared in (chat, responses):
            self.assertTrue(
                {
                    "HUB_DEGRADE_THINKING_TO_REASONING",
                    "HUB_DEGRADE_THINKING_SIGNATURE_DROPPED",
                }.issubset(set(prepared.plan.warning_codes))
            )

        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolRequestError) as raised:
                    protocol.prepare_request(
                        payload,
                        api_format,
                        compatibility_mode="strict",
                    )
                self.assertEqual(
                    raised.exception.code, "HUB_UNSUPPORTED_THINKING_SIGNATURE"
                )

    def test_empty_thinking_signature_placeholder_is_ignored_but_non_string_rejects(self) -> None:
        payload = {
            "model": "fixture-model",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "unsigned reasoning",
                            "signature": "",
                        }
                    ],
                }
            ],
        }
        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format):
                prepared = protocol.prepare_request(payload, api_format)
                self.assertIn(
                    "HUB_DEGRADE_EMPTY_THINKING_SIGNATURE_IGNORED",
                    prepared.plan.warning_codes,
                )
                self.assertIn(
                    "HUB_DEGRADE_THINKING_TO_REASONING",
                    prepared.plan.warning_codes,
                )
                self.assertNotIn(
                    "HUB_DEGRADE_THINKING_SIGNATURE_DROPPED",
                    prepared.plan.warning_codes,
                )
                self.assertNotIn('"signature"', json.dumps(prepared.payload))
                with self.assertRaises(protocol.ProtocolRequestError) as raised:
                    protocol.prepare_request(
                        payload,
                        api_format,
                        compatibility_mode="strict",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UNSUPPORTED_THINKING_SIGNATURE",
                )
                self.assertEqual(
                    raised.exception.path,
                    "$.messages[0].content[0].signature",
                )

        malformed = copy.deepcopy(payload)
        malformed["messages"][0]["content"][0]["signature"] = None
        for api_format in ("openai_chat", "openai_responses"):
            with self.subTest(api_format=api_format, signature="non-string"):
                with self.assertRaises(protocol.ProtocolRequestError) as raised:
                    protocol.prepare_request(malformed, api_format)
                self.assertEqual(raised.exception.code, "HUB_INVALID_CONTENT_BLOCK")
                self.assertEqual(
                    raised.exception.path,
                    "$.messages[0].content[0].signature",
                )

    def test_redacted_thinking_roundtrips_only_with_responses_provenance(self) -> None:
        anthropic = protocol.transform_response(
            {
                "id": "resp_opaque",
                "model": "responses-model",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "encrypted_content": "real-responses-opaque-value",
                        "summary": [],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            "openai_responses",
        )
        redacted = anthropic["content"][0]
        self.assertEqual(redacted["type"], "redacted_thinking")
        self.assertNotEqual(redacted["data"], "real-responses-opaque-value")

        replay = {
            "model": "responses-model",
            "messages": [{"role": "assistant", "content": [redacted]}],
        }
        prepared = protocol.prepare_request(replay, "openai_responses")
        reasoning = next(
            item for item in prepared.payload["input"] if item.get("type") == "reasoning"
        )
        self.assertEqual(
            reasoning["encrypted_content"], "real-responses-opaque-value"
        )

        with self.assertRaises(protocol.ProtocolRequestError) as chat_error:
            protocol.prepare_request(replay, "openai_chat")
        self.assertEqual(
            chat_error.exception.code, "HUB_UNSUPPORTED_REDACTED_THINKING"
        )

        unproven = {
            "model": "responses-model",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "unproven-opaque-value"}
                    ],
                }
            ],
        }
        with self.assertRaises(protocol.ProtocolRequestError) as provenance_error:
            protocol.prepare_request(unproven, "openai_responses")
        self.assertEqual(
            provenance_error.exception.code,
            "HUB_UNSUPPORTED_REDACTED_THINKING",
        )


class ResponseCapabilityContractTests(unittest.TestCase):
    def test_responses_output_item_ids_require_nonempty_text(self) -> None:
        item_templates = (
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": "{}",
                },
                "HUB_UPSTREAM_TOOL_CALL_INVALID",
            ),
            (
                {"type": "reasoning", "summary": []},
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
        )
        for template, expected_code in item_templates:
            for invalid_id in (None, "", 7, False):
                item = {**template, "id": invalid_id}
                with self.subTest(item_type=item["type"], invalid_id=invalid_id):
                    with self.assertRaises(protocol.ProtocolTransformError) as raised:
                        protocol.prepare_response(
                            {"status": "completed", "output": [item]},
                            "openai_responses",
                        )
                    self.assertEqual(raised.exception.code, expected_code)
                    self.assertEqual(raised.exception.path, "$.output[0].id")

    def test_responses_function_call_explicit_call_id_requires_nonempty_text(
        self,
    ) -> None:
        for invalid_call_id in (None, "", 7, False):
            with self.subTest(call_id=invalid_call_id):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "function_call",
                                    "id": "function_item_1",
                                    "call_id": invalid_call_id,
                                    "name": "lookup",
                                    "arguments": "{}",
                                }
                            ],
                        },
                        "openai_responses",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_TOOL_CALL_INVALID",
                )
                self.assertEqual(raised.exception.path, "$.output[0].call_id")

    def test_responses_output_item_ids_are_observably_dropped(self) -> None:
        prepared = protocol.prepare_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "message_item_1",
                        "role": "assistant",
                        "content": [],
                    },
                    {
                        "type": "function_call",
                        "id": "function_item_1",
                        "call_id": "call_1",
                        "name": "lookup",
                        "arguments": "{}",
                    },
                    {
                        "type": "reasoning",
                        "id": "reasoning_item_1",
                        "summary": [],
                    },
                ],
            },
            "openai_responses",
        )

        metadata_paths = {
            decision.path
            for decision in prepared.plan.decisions
            if decision.code
            == "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED"
        }
        self.assertEqual(
            metadata_paths,
            {
                "$.model",
                "$.output[0].id",
                "$.output[1].id",
                "$.output[2].id",
            },
        )

        id_only_call = protocol.prepare_response(
            {
                "status": "completed",
                "model": "responses-model",
                "output": [
                    {
                        "type": "function_call",
                        "id": "call_from_item_id",
                        "name": "lookup",
                        "arguments": "{}",
                    }
                ],
            },
            "openai_responses",
        )
        self.assertEqual(
            id_only_call.payload["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call_from_item_id",
                    "name": "lookup",
                    "input": {},
                }
            ],
        )
        self.assertNotIn(
            "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
            id_only_call.plan.warning_codes,
        )

    def test_response_wrappers_reject_unknown_or_malformed_identity_fields(self) -> None:
        chat_cases = (
            {
                "future_response_field": True,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
            },
            {
                "id": {"hidden": True},
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
            },
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                        "future_choice_field": True,
                    }
                ],
            },
        )
        for body in chat_cases:
            with self.subTest(api_format="openai_chat", body=body):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, "openai_chat")
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_RESPONSE_INVALID",
                )

        for body in (
            {"status": "completed", "output": [], "future_response_field": True},
            {"id": {"hidden": True}, "status": "completed", "output": []},
            {"model": ["hidden"], "status": "completed", "output": []},
        ):
            with self.subTest(api_format="openai_responses", body=body):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, "openai_responses")
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_RESPONSE_INVALID",
                )

        degraded = protocol.prepare_response(
            {
                "object": "chat.completion",
                "created": 1,
                "service_tier": "default",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                        "logprobs": None,
                    }
                ],
            },
            "openai_chat",
        )
        self.assertIn(
            "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
            degraded.plan.warning_codes,
        )

    def test_chat_message_role_and_signatures_fail_closed(self) -> None:
        malformed_messages = (
            {"role": "user", "content": "wrong role"},
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_signature": "opaque-signature",
            },
            {
                "role": "assistant",
                "content": "answer",
                "signature": "opaque-signature",
            },
        )
        for message in malformed_messages:
            with self.subTest(message=message):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "choices": [
                                {"message": message, "finish_reason": "stop"}
                            ]
                        },
                        "openai_chat",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_chat_content_parts_reject_unknown_fields(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "visible",
                                        "future_content": "hidden",
                                    }
                                ],
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
                "openai_chat",
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )

    def test_chat_content_parts_reject_conflicting_text_carriers(self) -> None:
        for part_type in (None, "text", "refusal"):
            part = {"text": "visible", "refusal": "hidden refusal"}
            if part_type is not None:
                part["type"] = part_type
            with self.subTest(part=part):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": [part],
                                    },
                                    "finish_reason": "content_filter",
                                }
                            ]
                        },
                        "openai_chat",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_chat_message_and_tool_call_fields_use_complete_registries(self) -> None:
        base_tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        malformed_messages = (
            {
                "role": "assistant",
                "content": "answer",
                "future_message_field": "hidden",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{**base_tool_call, "future_call_field": "hidden"}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{**base_tool_call, "type": "future_tool"}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        **base_tool_call,
                        "function": {
                            **base_tool_call["function"],
                            "future_function_field": "hidden",
                        },
                    }
                ],
            },
        )
        for message in malformed_messages:
            with self.subTest(message=message):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "choices": [
                                {
                                    "message": message,
                                    "finish_reason": "tool_calls",
                                }
                            ]
                        },
                        "openai_chat",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_completed_tool_calls_require_valid_indices_arguments_and_status(self) -> None:
        for call in (
            {
                "id": "call_1",
                "type": "function",
                "index": True,
                "function": {"name": "lookup", "arguments": "{}"},
            },
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup"},
            },
        ):
            with self.subTest(call=call):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": [call],
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

        for item in (
            {
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "status": "in_progress",
                "arguments": "{}",
            },
            {
                "type": "reasoning",
                "status": "in_progress",
                "summary": [],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "status": "completed",
            },
        ):
            with self.subTest(item=item):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {"status": "completed", "output": [item]},
                        "openai_responses",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_chat_requires_a_nonempty_explicit_finish_reason(self) -> None:
        malformed_choices = (
            {"message": {"role": "assistant", "content": "answer"}},
            {
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": None,
            },
            {
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": "",
            },
            {
                "message": {"role": "assistant", "content": "answer"},
                "finish_reason": False,
            },
        )
        for choice in malformed_choices:
            with self.subTest(choice=choice):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {"choices": [choice]},
                        "openai_chat",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
                )

    def test_chat_reasoning_and_content_bearing_fields_are_observable(self) -> None:
        prepared = protocol.prepare_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": "answer",
                            "reasoning_content": "unsigned reasoning",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
            "openai_chat",
        )
        self.assertEqual(
            prepared.payload["content"][0],
            {"type": "thinking", "thinking": "unsigned reasoning"},
        )
        self.assertIn(
            "HUB_DEGRADE_UNSIGNED_THINKING",
            prepared.plan.warning_codes,
        )

        malformed_messages = (
            {"content": {"type": "text", "text": "hidden"}},
            {"content": None, "reasoning_content": {"text": "hidden"}},
            {"content": None, "refusal": {"text": "hidden"}},
            {
                "content": None,
                "function_call": {"name": "legacy", "arguments": "{}"},
            },
            {"content": None, "audio": {"transcript": "hidden"}},
        )
        for message in malformed_messages:
            with self.subTest(message=message):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "choices": [
                                {"message": message, "finish_reason": "stop"}
                            ]
                        },
                        "openai_chat",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_responses_message_role_and_content_shape_fail_closed(self) -> None:
        malformed_messages = (
            {"type": "message", "role": "user", "content": []},
            {"type": "message", "role": "assistant"},
            {"type": "message", "role": "assistant", "content": None},
            {
                "type": "message",
                "role": "assistant",
                "content": "hidden text",
            },
        )
        for message in malformed_messages:
            with self.subTest(message=message):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "status": "completed",
                            "output": [message],
                        },
                        "openai_responses",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_responses_message_item_fields_use_a_complete_registry(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_1",
                            "role": "assistant",
                            "status": "completed",
                            "content": [],
                            "future_message_field": "hidden",
                        }
                    ],
                },
                "openai_responses",
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )

    def test_responses_text_parts_use_narrow_validated_shapes(self) -> None:
        malformed_parts = (
            {"type": "output_text", "text": "visible", "future_content": "hidden"},
            {"type": "output_text"},
            {"type": "output_text", "text": {"hidden": True}},
            {"type": "refusal", "refusal": "no", "annotations": []},
            {"type": "refusal"},
            {"type": "refusal", "refusal": ["hidden"]},
        )
        for part in malformed_parts:
            with self.subTest(part=part):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [part],
                                }
                            ],
                        },
                        "openai_responses",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_responses_reasoning_and_function_call_content_fail_closed(self) -> None:
        malformed_items = (
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "hidden"}],
            },
            {"type": "reasoning", "encrypted_content": {"hidden": True}},
            {
                "type": "reasoning",
                "summary": [
                    {
                        "type": "summary_text",
                        "text": "visible",
                        "future_content": "hidden",
                    }
                ],
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "{}",
                "future_content": "hidden",
            },
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": "{}",
                "input": {"hidden": True},
            },
        )
        for item in malformed_items:
            with self.subTest(item=item):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {"status": "completed", "output": [item]},
                        "openai_responses",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_responses_require_an_explicit_mappable_terminal_reason(self) -> None:
        malformed_terminal_fields = (
            {},
            {"status": None},
            {"status": ""},
            {"status": "future_status"},
            {"status": "incomplete"},
            {"status": "incomplete", "incomplete_details": {}},
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "future_reason"},
            },
            {
                "status": "incomplete",
                "incomplete_details": {
                    "reason": "max_output_tokens",
                    "future_terminal_field": "hidden",
                },
            },
            {
                "status": "completed",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            {"status": "completed", "incomplete_details": "malformed"},
        )
        for terminal_fields in malformed_terminal_fields:
            with self.subTest(terminal_fields=terminal_fields):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {"output": [], **terminal_fields},
                        "openai_responses",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
                )

        incomplete = protocol.prepare_response(
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
            "openai_responses",
        )
        self.assertEqual(incomplete.payload["stop_reason"], "max_tokens")

        reason_only = protocol.prepare_response(
            {
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
            "openai_responses",
        )
        self.assertEqual(reason_only.payload["stop_reason"], "max_tokens")

    def test_malformed_upstream_choices_tools_and_output_fail_closed(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "", "tool_calls": [None]},
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
                "HUB_UPSTREAM_TOOL_CALL_INVALID",
            ),
            (
                "openai_chat",
                {
                    "choices": [
                        {"message": {"content": "one"}, "finish_reason": "stop"},
                        {"message": {"content": "two"}, "finish_reason": "stop"},
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
                "HUB_UPSTREAM_MULTI_CHOICE_UNSUPPORTED",
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
                "HUB_UPSTREAM_RESPONSE_INVALID",
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [None],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
                "HUB_UPSTREAM_RESPONSE_INVALID",
            ),
        )
        for api_format, body, expected_code in cases:
            with self.subTest(api_format=api_format, expected_code=expected_code):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(raised.exception.code, expected_code)

    def test_missing_base_usage_is_an_observable_degradation_not_fake_cache(self) -> None:
        prepared = protocol.prepare_response(
            {
                "choices": [
                    {"message": {"content": "answer"}, "finish_reason": "stop"}
                ]
            },
            "openai_chat",
        )
        self.assertIn("HUB_USAGE_PROVENANCE_UNAVAILABLE", prepared.plan.warning_codes)
        self.assertNotIn("cache_read_input_tokens", prepared.payload["usage"])
        self.assertNotIn("cache_creation_input_tokens", prepared.payload["usage"])
        self.assertEqual(
            [
                (decision.feature, decision.path, decision.code)
                for decision in prepared.plan.decisions
                if decision.code == "HUB_USAGE_PROVENANCE_UNAVAILABLE"
            ],
            [
                (
                    "input_usage",
                    "$.usage.prompt_tokens",
                    "HUB_USAGE_PROVENANCE_UNAVAILABLE",
                ),
                (
                    "output_usage",
                    "$.usage.completion_tokens",
                    "HUB_USAGE_PROVENANCE_UNAVAILABLE",
                ),
            ],
        )

        partial = protocol.prepare_response(
            {
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 3, "total_tokens": 4},
            },
            "openai_responses",
        )
        self.assertEqual(partial.payload["usage"]["output_tokens"], 0)
        self.assertEqual(
            [
                (decision.feature, decision.path)
                for decision in partial.plan.decisions
                if decision.code == "HUB_USAGE_PROVENANCE_UNAVAILABLE"
            ],
            [("output_usage", "$.usage.output_tokens")],
        )

    def test_refusal_and_content_filter_map_to_refusal_stop_semantics(self) -> None:
        chat = protocol.transform_response(
            {
                "id": "chat_refusal",
                "model": "chat-model",
                "choices": [
                    {
                        "message": {"content": None, "refusal": "cannot comply"},
                        "finish_reason": "content_filter",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
            "openai_chat",
        )
        responses = protocol.transform_response(
            {
                "id": "resp_refusal",
                "model": "responses-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "cannot comply"}],
                    }
                ],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
            "openai_responses",
        )

        for body in (chat, responses):
            self.assertEqual(body["stop_reason"], "refusal")
            self.assertEqual(body["content"], [{"type": "text", "text": "cannot comply"}])

    def test_unknown_upstream_stop_reason_is_rejected_instead_of_becoming_end_turn(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.transform_response(
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "future_finish_reason",
                        }
                    ]
                },
                "openai_chat",
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
        )

    def test_usage_preserves_only_upstream_counters_and_cache_server_detail(self) -> None:
        prepared = protocol.prepare_response(
            {
                "id": "chat_usage",
                "model": "chat-model",
                "choices": [
                    {"message": {"content": "answer"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 40},
                    "cache_creation_input_tokens": 12,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 7,
                        "ephemeral_1h_input_tokens": 5,
                    },
                    "server_tool_use": {"web_search_requests": 2},
                },
            },
            "openai_chat",
        )
        self.assertEqual(
            prepared.payload["usage"],
            {
                "input_tokens": 60,
                "output_tokens": 20,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 12,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 7,
                    "ephemeral_1h_input_tokens": 5,
                },
                "server_tool_use": {"web_search_requests": 2},
            },
        )
        self.assertEqual(prepared.plan.adapter, "openai_chat")

        without_cache = protocol.prepare_response(
            {
                "choices": [
                    {"message": {"content": "answer"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            "openai_chat",
        ).payload["usage"]
        self.assertNotIn("cache_read_input_tokens", without_cache)
        self.assertNotIn("cache_creation_input_tokens", without_cache)
        self.assertNotIn("cache_creation", without_cache)
        self.assertNotIn("server_tool_use", without_cache)

    def test_usage_top_level_registry_drops_unknown_fields_with_warning(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "future_usage_counter": 4,
                        "reasoning_tokens": 0,
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "future_usage_counter": 4,
                    },
                },
            ),
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 1},
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": 1},
                    },
                },
            ),
        )
        for api_format, body in cases:
            with self.subTest(api_format=api_format, usage=body["usage"]):
                expected_dropped = sorted(set(body["usage"]) - {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"})[0]
                prepared = protocol.prepare_response(body, api_format)
                self.assertIn(
                    "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                    prepared.plan.warning_codes,
                )
                self.assertIn(
                    f"HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED@$.usage.{expected_dropped}",
                    prepared.plan.warning_details,
                )
                usage = prepared.payload["usage"]
                self.assertNotIn("future_usage_counter", usage)
                self.assertNotIn("reasoning_tokens", usage)
                self.assertNotIn("input_tokens_details", usage)
                self.assertNotIn("prompt_tokens_details", usage)

    def test_usage_total_tokens_must_be_a_non_negative_counter(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": False,
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "total_tokens": -1,
                    },
                },
            ),
        )
        for api_format, body in cases:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_usage_total_tokens_must_match_complete_base_counters(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 5,
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "total_tokens": 5,
                    },
                },
            ),
        )
        for api_format, body in cases:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_chat_cache_read_detail_rejects_a_malformed_wrapper(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": "malformed",
                    },
                },
                "openai_chat",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_responses_cache_read_detail_rejects_a_malformed_wrapper(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "input_tokens_details": ["malformed"],
                    },
                },
                "openai_responses",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_output_usage_detail_wrappers_must_be_objects(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "completion_tokens_details": "malformed",
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "output_tokens_details": ["malformed"],
                    },
                },
            ),
        )
        for api_format, body in cases:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_usage_detail_registries_drop_unknown_nested_fields(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"future_tokens": "opaque"},
                    },
                },
            ),
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "completion_tokens_details": {"future_tokens": "opaque"},
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "input_tokens_details": {"future_tokens": "opaque"},
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "output_tokens_details": {"future_tokens": "opaque"},
                    },
                },
            ),
        )
        for api_format, body in cases:
            with self.subTest(api_format=api_format, usage=body["usage"]):
                prepared = protocol.prepare_response(body, api_format)
                detail_key = next(
                    key
                    for key in body["usage"]
                    if key.endswith("_details")
                )
                self.assertIn(
                    "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED"
                    f"@$.usage.{detail_key}.future_tokens",
                    prepared.plan.warning_details,
                )
                self.assertNotIn(detail_key, prepared.payload["usage"])

    def test_standard_usage_details_require_non_negative_counters(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"audio_tokens": False},
                    },
                },
            ),
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "completion_tokens_details": {
                            "accepted_prediction_tokens": "malformed"
                        },
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "output_tokens_details": {"reasoning_tokens": -1},
                    },
                },
            ),
        )
        for api_format, body in cases:
            with self.subTest(api_format=api_format, usage=body["usage"]):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_standard_usage_details_degrade_observably_while_cache_is_exact(self) -> None:
        chat = protocol.prepare_response(
            {
                "choices": [
                    {"message": {"content": "answer"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "prompt_tokens_details": {
                        "cached_tokens": 3,
                        "audio_tokens": 2,
                    },
                    "completion_tokens_details": {
                        "reasoning_tokens": 2,
                        "accepted_prediction_tokens": 1,
                        "rejected_prediction_tokens": 1,
                        "audio_tokens": 0,
                    },
                },
            },
            "openai_chat",
        )
        responses = protocol.prepare_response(
            {
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                    "input_tokens_details": {"cached_tokens": 2},
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            },
            "openai_responses",
        )

        self.assertEqual(chat.payload["usage"]["cache_read_input_tokens"], 3)
        self.assertEqual(responses.payload["usage"]["cache_read_input_tokens"], 2)
        for prepared in (chat, responses):
            self.assertIn(
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                prepared.plan.warning_codes,
            )
        self.assertEqual(
            {
                decision.path
                for decision in chat.plan.decisions
                if decision.code
                == "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED"
            },
            {
                "$.model",
                "$.usage.prompt_tokens_details.audio_tokens",
                "$.usage.completion_tokens_details.reasoning_tokens",
                "$.usage.completion_tokens_details.accepted_prediction_tokens",
                "$.usage.completion_tokens_details.rejected_prediction_tokens",
                "$.usage.completion_tokens_details.audio_tokens",
            },
        )
        self.assertEqual(
            [
                decision.path
                for decision in responses.plan.decisions
                if decision.code
                == "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED"
            ],
            ["$.usage.output_tokens_details.reasoning_tokens", "$.model"],
        )

    def test_cache_read_detail_rejects_an_invalid_cached_counter(self) -> None:
        cases = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": False},
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": -1},
                    },
                },
            ),
        )
        for api_format, body in cases:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_direct_cache_carriers_reject_invalid_counters(self) -> None:
        cases = (
            ("cache_read_input_tokens", None),
            ("cache_read_tokens", []),
            ("cache_creation_input_tokens", False),
            ("cache_creation_tokens", -1),
        )
        for carrier, invalid_value in cases:
            with self.subTest(carrier=carrier):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(
                        {
                            "choices": [
                                {
                                    "message": {"content": "answer"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 3,
                                "completion_tokens": 1,
                                carrier: invalid_value,
                            },
                        },
                        "openai_chat",
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_later_cache_read_carrier_is_still_validated(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": 1},
                        "cache_read_input_tokens": "malformed",
                    },
                },
                "openai_chat",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_later_cache_write_carrier_is_still_validated(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 2,
                        "cache_creation_tokens": None,
                    },
                },
                "openai_responses",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_conflicting_cache_read_carriers_are_rejected(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "choices": [
                        {
                            "message": {"content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": 1},
                        "cache_read_input_tokens": 9,
                    },
                },
                "openai_chat",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_conflicting_cache_write_carriers_are_rejected(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 2,
                        "cache_creation_tokens": 7,
                    },
                },
                "openai_responses",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_cache_creation_total_must_match_split_detail(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 9,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 1,
                            "ephemeral_1h_input_tokens": 2,
                        },
                    },
                },
                "openai_responses",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_responses_conflicting_cache_read_carriers_are_rejected(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            protocol.prepare_response(
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 1},
                        "cache_read_tokens": 8,
                    },
                },
                "openai_responses",
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_matching_cache_carriers_are_coalesced_without_fabrication(self) -> None:
        prepared = protocol.prepare_response(
            {
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 1,
                    "input_tokens_details": {"cached_tokens": "4"},
                    "cache_read_input_tokens": 4,
                    "cache_read_tokens": "4",
                    "cache_creation_input_tokens": "2",
                    "cache_creation_tokens": 2,
                },
            },
            "openai_responses",
        )
        self.assertEqual(
            prepared.payload["usage"],
            {
                "input_tokens": 6,
                "output_tokens": 1,
                "cache_read_input_tokens": 4,
                "cache_creation_input_tokens": 2,
            },
        )

    def test_base_input_usage_excludes_cache_read_tokens(self) -> None:
        bodies = (
            (
                "openai_chat",
                {
                    "choices": [
                        {"message": {"content": "answer"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": 4},
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 4},
                    },
                },
            ),
        )
        for api_format, body in bodies:
            with self.subTest(api_format=api_format):
                usage = protocol.prepare_response(body, api_format).payload["usage"]
                self.assertEqual(usage["input_tokens"], 6)
                self.assertEqual(usage["cache_read_input_tokens"], 4)

    def test_cache_read_larger_than_base_input_usage_is_rejected(self) -> None:
        bodies = (
            (
                "openai_chat",
                {
                    "choices": [
                        {"message": {"content": "answer"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": 4},
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 4},
                    },
                },
            ),
        )
        for api_format, body in bodies:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID"
                )

    def test_duplicate_tool_call_ids_are_rejected_for_both_formats(self) -> None:
        bodies = (
            (
                "openai_chat",
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "a", "arguments": "{}"},
                                    },
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "b", "arguments": "{}"},
                                    },
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "a",
                            "arguments": "{}",
                        },
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "b",
                            "arguments": "{}",
                        },
                    ],
                },
            ),
        )
        for api_format, body in bodies:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code, "HUB_UPSTREAM_TOOL_CALL_INVALID"
                )

    def test_cache_creation_detail_without_total_is_rejected(self) -> None:
        bodies = (
            (
                "openai_chat",
                {
                    "choices": [
                        {"message": {"content": "answer"}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "cache_creation": {"ephemeral_5m_input_tokens": 2},
                    },
                },
            ),
            (
                "openai_responses",
                {
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "cache_creation": {"ephemeral_5m_input_tokens": 2},
                    },
                },
            ),
        )
        for api_format, body in bodies:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID"
                )

    def test_missing_upstream_model_is_an_observable_degradation(self) -> None:
        for api_format, body in (
            (
                "openai_chat",
                {
                    "choices": [
                        {"message": {"content": "answer"}, "finish_reason": "stop"}
                    ]
                },
            ),
            ("openai_responses", {"status": "completed", "output": []}),
        ):
            with self.subTest(api_format=api_format):
                prepared = protocol.prepare_response(body, api_format)
                self.assertEqual(prepared.payload["model"], "")
                self.assertIn(
                    "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                    prepared.plan.warning_codes,
                )
                self.assertIn(
                    "$.model",
                    {
                        decision.path
                        for decision in prepared.plan.decisions
                        if decision.code
                        == "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED"
                    },
                )

    def test_real_upstream_citations_are_not_fabricated_into_anthropic_locations(self) -> None:
        chat = protocol.prepare_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "cited answer",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url": "https://example.test/source",
                                            "start_index": 0,
                                            "end_index": 5,
                                        }
                                    ],
                                }
                            ]
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            "openai_chat",
        )
        responses = protocol.prepare_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "cited answer",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.test/source",
                                        "start_index": 0,
                                        "end_index": 5,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            "openai_responses",
        )

        for prepared in (chat, responses):
            self.assertEqual(
                prepared.payload["content"],
                [{"type": "text", "text": "cited answer"}],
            )
            self.assertNotIn("https://example.test/source", json.dumps(prepared.payload))
            self.assertIn(
                "HUB_DEGRADE_CITATION_METADATA_DROPPED",
                prepared.plan.warning_codes,
            )

    def test_unknown_upstream_output_blocks_fail_closed(self) -> None:
        cases = {
            "openai_chat": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "future_output_part", "opaque": True}
                            ]
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
            "openai_responses": {
                "status": "completed",
                "output": [{"type": "future_output_item", "opaque": True}],
            },
        }
        for api_format, body in cases.items():
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_invalid_upstream_tool_arguments_are_not_wrapped_as_fake_objects(self) -> None:
        cases = {
            "openai_chat": {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_bad",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": "not-json",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "openai_responses": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_bad",
                        "name": "lookup",
                        "arguments": ["not", "an", "object"],
                    }
                ],
            },
        }
        for api_format, body in cases.items():
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    protocol.prepare_response(body, api_format)
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_TOOL_ARGUMENTS_INVALID",
                )

    def test_upstream_error_type_and_sensitive_message_are_not_forwarded(self) -> None:
        transformed = protocol.transform_error(
            {
                "error": {
                    "type": "vendor_quota_type",
                    "message": (
                        "Bearer fixture-secret-token at "
                        "https://private-upstream.invalid/account"
                    ),
                }
            },
            400,
        )
        self.assertEqual(transformed["error"]["type"], "invalid_request_error")
        serialized = json.dumps(transformed)
        self.assertNotIn("vendor_quota_type", serialized)
        self.assertNotIn("fixture-secret-token", serialized)
        self.assertNotIn("private-upstream", serialized)

    def test_numeric_upstream_error_code_is_preserved(self) -> None:
        # OpenAI-compatible providers 常用 JSON 数字 code 表示参数错误
        # 或限流。数字与字符串走同一条证据通道，
        # 不因类型差异折叠回 "upstream HTTP N"。
        transformed = protocol.transform_error(
            {"error": {"code": 1210, "message": "API 调用参数有误"}},
            400,
        )
        self.assertEqual(
            transformed["error"]["message"],
            "upstream HTTP 400 (1210): API 调用参数有误",
        )
        code, message = protocol.upstream_error_evidence(
            {"error": {"code": 1302, "message": "账户达到速率限制"}}
        )
        self.assertEqual(code, "1302")
        self.assertEqual(message, "账户达到速率限制")

    def test_upstream_error_message_survives_without_code(self) -> None:
        # 无 code 的上游错误同样保留脱敏后的 message：证据通道不依赖
        # code 存在，只依赖脱敏规则。
        transformed = protocol.transform_error(
            {"error": {"message": "usage window exhausted; retry in 5h"}},
            429,
        )
        self.assertEqual(
            transformed["error"]["message"],
            "upstream HTTP 429: usage window exhausted; retry in 5h",
        )

    def test_double_encoded_upstream_error_preserves_safe_code_and_message(self) -> None:
        transformed = protocol.transform_error(
            {
                "error": {
                    "code": "",
                    "message": json.dumps(
                        {
                            "error": {
                                "code": "1308",
                                "message": "usage window exhausted; retry after 19:16:38",
                            }
                        }
                    ),
                }
            },
            429,
        )

        self.assertEqual(transformed["error"]["type"], "rate_limit_error")
        self.assertEqual(
            transformed["error"]["message"],
            "upstream HTTP 429 (1308): usage window exhausted; retry after 19:16:38",
        )

    def test_safe_upstream_error_message_is_redacted_before_forwarding(self) -> None:
        transformed = protocol.transform_error(
            {
                "error": {
                    "code": "rate_limit",
                    "message": (
                        "Bearer fixture-secret-token at "
                        "https://private-upstream.invalid/account"
                    ),
                }
            },
            429,
        )

        message = transformed["error"]["message"]
        self.assertIn("rate_limit", message)
        self.assertIn("[redacted-token]", message)
        self.assertIn("[redacted-url]", message)
        self.assertNotIn("fixture-secret-token", message)
        self.assertNotIn("private-upstream", message)

    def test_relay_error_shapes_without_the_canonical_envelope(self) -> None:
        # 中转站常省掉 {"error": {...}} 外壳。证据通道对这些形状一律通用，
        # 不为某个渠道写专用分支。
        cases = [
            ({"error": "quota exhausted"}, (None, "quota exhausted")),
            ({"message": "5 小时限额已到", "code": 429}, ("429", "5 小时限额已到")),
            ({"detail": "insufficient balance"}, (None, "insufficient balance")),
            ({"msg": "concurrency limit", "code": "1305"}, ("1305", "concurrency limit")),
            ('{"detail": "gateway rejected"}', (None, "gateway rejected")),
            # canonical 外壳存在时优先，不被顶层字段抢走
            (
                {"error": {"message": "envelope wins"}, "message": "ignored"},
                (None, "envelope wins"),
            ),
            # 无从取证时 fail-closed 回落 None，而不是转发整个响应体
            ({"unexpected": {"nested": "shape"}}, (None, None)),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(protocol.upstream_error_evidence(body), expected)

    def test_sanitizer_redacts_assignment_and_key_shapes(self) -> None:
        # 原来只脱敏 Bearer 与 URL，token=xxx 这类赋值形状会漏。流中错误
        # 详情要透传，前提就是清洗器先覆盖这些形状。
        # sk- 形状用拼接构造:仓库里不留完整 key 字面量,否则 secret_guard 会拦。
        key_shape = "sk-" + "fixtureSECRET0123456789"
        redacted = protocol.sanitize_error_text(
            "auth failed token=fixture-secret api_key: fixture-secret "
            f"key=fixture-secret {key_shape} "
            "Bearer fixture-secret https://vendor.invalid/x"
        )
        self.assertNotIn("fixture-secret", redacted)
        self.assertNotIn("fixtureSECRET", redacted)
        self.assertNotIn("vendor.invalid", redacted)
        self.assertIn("auth failed", redacted)
        self.assertIn("[redacted-key]", redacted)
        self.assertIn("[redacted-token]", redacted)
        self.assertIn("[redacted-url]", redacted)

        # 真实原因本身不含凭证形状时必须原样存活，中文不受影响。
        self.assertEqual(
            protocol.sanitize_error_text("您的账户已达到速率限制，请控制请求频率"),
            "您的账户已达到速率限制，请控制请求频率",
        )
        # 长度有界、控制字符剥离、空文本回落 None。
        self.assertEqual(len(protocol.sanitize_error_text("x" * 900)), 512)
        self.assertEqual(protocol.sanitize_error_text("a\r\nb"), "a b")
        self.assertIsNone(protocol.sanitize_error_text("   "))

    def test_chat_stream_error_frame_terminates_with_the_real_reason(self) -> None:
        # OpenAI 兼容网关在流中报限额时发裸 {"error": ...} 帧。以前它会撞上
        # 字段白名单炸成 ProtocolTransformError（下游只看到裸断连），
        # 现在必须变成一个携带真实原因的终态 error 事件。
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        rendered = b"".join(
            bridge.feed(
                "message",
                json.dumps(
                    {
                        "error": {
                            "code": "1302",
                            "message": "5 小时限额已用完 token=fixture-secret",
                        }
                    }
                ),
            )
        )

        self.assertIn(b"event: error", rendered)
        payload = json.loads(rendered.split(b"data: ", 1)[1].decode("utf-8"))
        self.assertEqual(payload["error"]["type"], "api_error")
        self.assertIn("(1302)", payload["error"]["message"])
        self.assertIn("5 小时限额已用完", payload["error"]["message"])
        self.assertNotIn("fixture-secret", payload["error"]["message"])
        self.assertTrue(bridge.upstream_terminal)
        self.assertNotIn(
            "HUB_DEGRADE_UPSTREAM_ERROR_DETAIL_DROPPED",
            bridge.warning_codes,
        )

    def test_chat_stream_string_error_frame_is_accepted_and_objects_validated(
        self,
    ) -> None:
        string_frame = protocol.AnthropicStreamBridge("openai_chat")
        rendered = b"".join(
            string_frame.feed("message", json.dumps({"error": "upstream overloaded"}))
        )
        self.assertIn(b"upstream overloaded", rendered)

        # 非 object/string 的 error 仍然 fail closed，不猜形状。
        invalid = protocol.AnthropicStreamBridge("openai_chat")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            invalid.feed("message", json.dumps({"error": [1, 2, 3]}))
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_RESPONSE_INVALID")


if __name__ == "__main__":
    unittest.main()
