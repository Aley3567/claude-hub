from __future__ import annotations

import json
import random
import time
import unittest
from pathlib import Path

import claude1_protocol as protocol


SSE_FIXTURE_ROOT = (
    Path(__file__).parent / "fixtures" / "anthropic_protocol" / "sse"
)


def _payloads(chunks: list[bytes]) -> list[dict]:
    payloads: list[dict] = []
    for chunk in chunks:
        data = chunk.decode("utf-8").split("\ndata: ", 1)[1]
        payloads.append(json.loads(data))
    return payloads


def _assert_anthropic_stream_invariants(test: unittest.TestCase, rendered: bytes) -> None:
    events = []
    for frame in rendered.split(b"\n\n"):
        if not frame.strip():
            continue
        lines = frame.decode("utf-8").splitlines()
        test.assertTrue(lines[0].startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append(json.loads(data))
    test.assertEqual(sum(event["type"] == "message_start" for event in events), 1)
    starts: list[int] = []
    opened: set[int] = set()
    stopped: set[int] = set()
    terminal_seen = False
    for event in events:
        kind = event["type"]
        if terminal_seen:
            test.fail(f"event {kind} appeared after message_stop")
        if kind == "content_block_start":
            index = event["index"]
            test.assertNotIn(index, opened)
            test.assertNotIn(index, stopped)
            starts.append(index)
            opened.add(index)
        elif kind == "content_block_delta":
            test.assertIn(event["index"], opened)
        elif kind == "content_block_stop":
            index = event["index"]
            test.assertIn(index, opened)
            opened.remove(index)
            stopped.add(index)
        elif kind == "message_stop":
            terminal_seen = True
    test.assertEqual(starts, list(range(len(starts))))
    test.assertFalse(opened)
    test.assertTrue(terminal_seen)


class SSEParserContractTests(unittest.TestCase):
    def test_fixture_backed_golden_streams(self) -> None:
        cases = (
            ("chat_utf8_crlf", "openai_chat", True),
            ("responses_tool_partial", "openai_responses", False),
            ("responses_refusal", "openai_responses", False),
        )
        for name, api_format, use_crlf in cases:
            with self.subTest(name=name, api_format=api_format):
                wire = (SSE_FIXTURE_ROOT / f"{name}.input.sse").read_bytes()
                if use_crlf:
                    wire = wire.replace(b"\n", b"\r\n")
                rendered = b"".join(
                    protocol.translate_sse_chunks(api_format, [wire])
                )
                expected = (
                    SSE_FIXTURE_ROOT / f"{name}.anthropic.golden.sse"
                ).read_bytes()
                self.assertEqual(rendered, expected)
                _assert_anthropic_stream_invariants(self, rendered)

    def test_parser_obeys_one_space_rule_across_every_utf8_chunk_boundary(self) -> None:
        wire = (
            ": keepalive\r\n"
            "event: message\r\n"
            "data:  你好🙂\r\n"
            "data: second line\r\n"
            "\r\n"
        ).encode("utf-8")
        expected = [("message", " 你好🙂\nsecond line")]

        self.assertEqual(protocol.SSEParser().feed(wire), expected)
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                parser = protocol.SSEParser()
                events = parser.feed(wire[:split])
                events.extend(parser.feed(wire[split:]))
                parser.finish()
                self.assertEqual(events, expected)

    def test_chat_and_responses_translation_is_invariant_to_all_and_fuzzed_chunks(self) -> None:
        chat_payloads = [
            {
                "id": "chat_fixture",
                "model": "fixture-model",
                "choices": [
                    {"delta": {"content": "你好🙂"}, "finish_reason": None}
                ],
            },
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        ]
        chat_wire = b"".join(
            (
                "data: "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\r\n\r\n"
            ).encode("utf-8")
            for payload in chat_payloads
        ) + b"data: [DONE]\r\n\r\n"

        response_events = [
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {"id": "resp_fixture", "model": "fixture-model"},
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "你好🙂",
                },
            ),
            (
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "你好🙂",
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                },
            ),
        ]
        responses_wire = b"".join(
            (
                f"event: {event}\r\n"
                "data: "
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                + "\r\n\r\n"
            ).encode("utf-8")
            for event, payload in response_events
        )

        for api_format, wire in (
            ("openai_chat", chat_wire),
            ("openai_responses", responses_wire),
        ):
            reference = b"".join(protocol.translate_sse_chunks(api_format, [wire]))
            _assert_anthropic_stream_invariants(self, reference)
            for split in range(len(wire) + 1):
                with self.subTest(api_format=api_format, split=split):
                    rendered = b"".join(
                        protocol.translate_sse_chunks(
                            api_format,
                            [wire[:split], wire[split:]],
                        )
                    )
                    self.assertEqual(rendered, reference)
            self.assertEqual(
                b"".join(
                    protocol.translate_sse_chunks(
                        api_format,
                        [bytes([byte]) for byte in wire],
                    )
                ),
                reference,
            )
            rng = random.Random(20260810)
            for case in range(64):
                cuts = sorted(
                    rng.sample(
                        range(1, len(wire)),
                        k=min(len(wire) - 1, rng.randint(1, 16)),
                    )
                )
                chunks = [
                    wire[start:end]
                    for start, end in zip(
                        [0, *cuts],
                        [*cuts, len(wire)],
                        strict=True,
                    )
                ]
                with self.subTest(api_format=api_format, fuzz_case=case):
                    self.assertEqual(
                        b"".join(protocol.translate_sse_chunks(api_format, chunks)),
                        reference,
                    )

    def test_parser_accepts_mixed_line_ending_boundaries(self) -> None:
        wire = b"data: one\n\r\ndata: two\r\rdata: three\r\n\r\ndata: four\n\n"
        self.assertEqual(
            protocol.SSEParser().feed(wire),
            [
                ("message", "one"),
                ("message", "two"),
                ("message", "three"),
                ("message", "four"),
            ],
        )

    def test_parser_strips_a_single_leading_utf8_bom(self) -> None:
        self.assertEqual(
            protocol.SSEParser().feed(b"\xef\xbb\xbfdata: hello\n\n"),
            [("message", "hello")],
        )
        parser = protocol.SSEParser()
        events = parser.feed(b"\xef\xbb\xbfdata: he")
        events.extend(parser.feed(b"llo\n\n"))
        parser.finish()
        self.assertEqual(events, [("message", "hello")])
        # A BOM after the first feed is event content, not framing noise.
        parser = protocol.SSEParser()
        events = parser.feed(b"data: x")
        events.extend(parser.feed(b"\xef\xbb\xbf\n\n"))
        self.assertEqual(events, [("message", "x\ufeff")])

    def test_parser_stays_linear_for_single_byte_chunks(self) -> None:
        parser = protocol.SSEParser()
        payload = b"data: " + b"a" * (200 * 1024) + b"\n\n"
        started = time.monotonic()
        events = []
        for index in range(len(payload)):
            events.extend(parser.feed(payload[index : index + 1]))
        elapsed = time.monotonic() - started
        self.assertEqual(events, [("message", "a" * (200 * 1024))])
        self.assertLess(elapsed, 10)

    def test_chat_text_thinking_text_interleave_closes_each_block(self) -> None:
        payloads = [
            {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
            {
                "choices": [
                    {"delta": {"reasoning_content": "think"}, "finish_reason": None}
                ]
            },
            {"choices": [{"delta": {"content": "world"}, "finish_reason": None}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        ]
        wire = b"".join(
            b"data: " + json.dumps(payload).encode() + b"\n\n" for payload in payloads
        ) + b"data: [DONE]\n\n"

        rendered = b"".join(protocol.translate_sse_chunks("openai_chat", [wire]))
        _assert_anthropic_stream_invariants(self, rendered)

        parser = protocol.SSEParser()
        block_events = []
        for event, data in parser.feed(rendered):
            body = json.loads(data)
            if body["type"].startswith("content_block_"):
                block_events.append(
                    (
                        body["type"],
                        body["index"],
                        body.get("content_block", {}).get("type"),
                    )
                )
        self.assertEqual(
            block_events,
            [
                ("content_block_start", 0, "text"),
                ("content_block_delta", 0, None),
                ("content_block_stop", 0, None),
                ("content_block_start", 1, "thinking"),
                ("content_block_delta", 1, None),
                ("content_block_stop", 1, None),
                ("content_block_start", 2, "text"),
                ("content_block_delta", 2, None),
                ("content_block_stop", 2, None),
            ],
        )

    def test_responses_terminal_snapshot_closes_parts_without_part_done(self) -> None:
        events = [
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {"id": "resp_1", "model": "responses-model"},
                },
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            ),
            (
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "hel",
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "model": "responses-model",
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "id": "msg_1",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {"type": "output_text", "text": "hello"}
                                ],
                            }
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 1},
                    },
                },
            ),
        ]
        wire = b"".join(
            (
                f"event: {event}\n"
                "data: "
                + json.dumps(payload, separators=(",", ":"))
                + "\n\n"
            ).encode()
            for event, payload in events
        )

        rendered = b"".join(protocol.translate_sse_chunks("openai_responses", [wire]))
        _assert_anthropic_stream_invariants(self, rendered)

        parser = protocol.SSEParser()
        text = []
        final_usage = None
        for _, data in parser.feed(rendered):
            body = json.loads(data)
            if body["type"] == "content_block_delta":
                text.append(body["delta"].get("text", ""))
            elif body["type"] == "message_delta":
                final_usage = body["usage"]
        self.assertEqual("".join(text), "hello")
        self.assertIsNotNone(final_usage)

    @staticmethod
    def _message_usages(chunks: list[bytes]) -> tuple[dict, dict]:
        parser = protocol.SSEParser()
        start_usage = delta_usage = None
        for _, data in parser.feed(b"".join(chunks)):
            body = json.loads(data)
            if body["type"] == "message_start":
                start_usage = body["message"]["usage"]
            elif body["type"] == "message_delta":
                delta_usage = body.get("usage")
        return start_usage, delta_usage

    def test_unobserved_stream_usage_is_omitted_not_zeroed(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        chunks = bridge.feed(
            "message",
            '{"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}',
        )
        chunks.extend(bridge.feed("message", "[DONE]"))
        chunks.extend(bridge.finish())
        self.assertIn("HUB_USAGE_PROVENANCE_UNAVAILABLE", bridge.warning_codes)

        start_usage, delta_usage = self._message_usages(chunks)
        self.assertEqual(start_usage, {})
        # 一个计数器都没观测到时，终态 message_delta 省略整个 usage 键，
        # 而不是发一个读作“观测过但为空”的对象。
        self.assertIsNone(delta_usage)

    def test_empty_upstream_usage_object_stays_unobserved(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        chunks = bridge.feed(
            "message",
            '{"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}],'
            '"usage":{}}',
        )
        chunks.extend(bridge.feed("message", "[DONE]"))
        chunks.extend(bridge.finish())
        # 上游显式发空 usage={} 同样不含任何观测证据：终态事件省略 usage
        # 键，记账视图也为空。
        self.assertEqual(bridge.usage_for_accounting(), {})

        start_usage, delta_usage = self._message_usages(chunks)
        self.assertEqual(start_usage, {})
        self.assertIsNone(delta_usage)

    def test_late_input_usage_is_carried_by_message_delta_observably(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        chunks = bridge.feed(
            "message",
            '{"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
        )
        chunks.extend(
            bridge.feed(
                "message",
                '{"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":5,"completion_tokens":3}}',
            )
        )
        chunks.extend(bridge.feed("message", "[DONE]"))
        chunks.extend(bridge.finish())
        self.assertIn("HUB_DEGRADE_LATE_INPUT_USAGE", bridge.warning_codes)

        start_usage, delta_usage = self._message_usages(chunks)
        self.assertEqual(start_usage, {})
        self.assertEqual(delta_usage, {"output_tokens": 3, "input_tokens": 5})

    def test_early_input_usage_stays_in_message_start(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        bridge.feed(
            "message",
            '{"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3}}',
        )
        chunks = bridge.feed(
            "message",
            '{"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}',
        )
        chunks.extend(bridge.feed("message", "[DONE]"))
        chunks.extend(bridge.finish())
        self.assertNotIn("HUB_DEGRADE_LATE_INPUT_USAGE", bridge.warning_codes)

        start_usage, delta_usage = self._message_usages(chunks)
        self.assertEqual(
            start_usage, {"input_tokens": 5, "output_tokens": 3}
        )
        self.assertEqual(delta_usage, {"output_tokens": 3})

    def test_invalid_utf8_partial_event_and_invalid_json_have_stable_errors(self) -> None:
        with self.assertRaises(protocol.ProtocolTransformError) as utf8:
            protocol.SSEParser().feed(b"data: \xff\n\n")
        self.assertEqual(utf8.exception.code, "HUB_SSE_UTF8_INVALID")

        parser = protocol.SSEParser()
        parser.feed(b'data: {"partial":')
        with self.assertRaises(protocol.ProtocolTransformError) as partial:
            parser.finish()
        self.assertEqual(partial.exception.code, "HUB_SSE_INCOMPLETE_EVENT")

        with self.assertRaises(protocol.ProtocolTransformError) as invalid_json:
            list(
                protocol.translate_sse_chunks(
                    "openai_chat",
                    [b"data: {not-json}\n\n"],
                )
            )
        self.assertEqual(invalid_json.exception.code, "HUB_SSE_JSON_INVALID")

    def test_translation_without_semantic_terminal_has_stable_error(self) -> None:
        cases = (
            (
                "openai_chat",
                b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
            ),
            (
                "openai_responses",
                b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_partial"}}\n\n',
            ),
        )
        for api_format, wire in cases:
            with self.subTest(api_format=api_format):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    list(protocol.translate_sse_chunks(api_format, [wire]))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_SSE_MISSING_TERMINAL",
                )


class StreamStateMachineContractTests(unittest.TestCase):
    def test_stream_resource_cap_rejects_too_many_blocks(self) -> None:
        fsm = protocol.StreamStateMachine()
        for _ in range(protocol.MAX_STREAM_BLOCKS):
            block = fsm.open_block("text", None)
            fsm.close_block(block.index)

        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            fsm.open_block("text", None)
        self.assertEqual(raised.exception.code, "HUB_SSE_TOO_MANY_BLOCKS")

        original_limit = protocol.MAX_STREAM_BLOCKS
        protocol.MAX_STREAM_BLOCKS = 2
        try:
            structural = protocol.AnthropicStreamBridge("openai_responses")
            for content_index in range(2):
                structural.feed(
                    "response.content_part.added",
                    json.dumps(
                        {
                            "type": "response.content_part.added",
                            "item_id": f"msg_{content_index}",
                            "output_index": content_index,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": ""},
                        }
                    ),
                )
            with self.assertRaises(protocol.ProtocolTransformError) as raised:
                structural.feed(
                    "response.reasoning_summary_part.added",
                    json.dumps(
                        {
                            "type": "response.reasoning_summary_part.added",
                            "item_id": "rs_overflow",
                            "output_index": 2,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": ""},
                        }
                    ),
                )
            self.assertEqual(raised.exception.code, "HUB_SSE_TOO_MANY_BLOCKS")

            empty_keys = protocol.AnthropicStreamBridge("openai_responses")
            empty_keys.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "",
                    }
                ),
            )
            empty_keys.feed(
                "response.reasoning_summary_text.delta",
                json.dumps(
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "output_index": 1,
                        "summary_index": 0,
                        "delta": "",
                    }
                ),
            )
            with self.assertRaises(protocol.ProtocolTransformError) as raised:
                empty_keys.feed(
                    "response.output_text.delta",
                    json.dumps(
                        {
                            "type": "response.output_text.delta",
                            "output_index": 2,
                            "content_index": 0,
                            "delta": "",
                        }
                    ),
                )
            self.assertEqual(raised.exception.code, "HUB_SSE_TOO_MANY_BLOCKS")
        finally:
            protocol.MAX_STREAM_BLOCKS = original_limit

    def test_stream_text_caps_count_utf8_bytes_for_deltas_and_snapshots(self) -> None:
        original_limit = protocol.MAX_STREAM_TEXT_BYTES
        original_total_limit = protocol.MAX_STREAM_TEXT_TOTAL_BYTES
        protocol.MAX_STREAM_TEXT_BYTES = 4
        protocol.MAX_STREAM_TEXT_TOTAL_BYTES = 7
        try:
            delta = protocol.AnthropicStreamBridge("openai_responses")
            delta.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "你",
                    }
                ),
            )
            with self.assertRaises(protocol.ProtocolTransformError) as raised:
                delta.feed(
                    "response.output_text.delta",
                    json.dumps(
                        {
                            "type": "response.output_text.delta",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": "好",
                        }
                    ),
                )
            self.assertEqual(raised.exception.code, "HUB_SSE_TEXT_TOO_LARGE")

            snapshot = protocol.AnthropicStreamBridge("openai_responses")
            with self.assertRaises(protocol.ProtocolTransformError) as raised:
                snapshot.feed(
                    "response.output_text.done",
                    json.dumps(
                        {
                            "type": "response.output_text.done",
                            "output_index": 0,
                            "content_index": 0,
                            "text": "hello",
                        }
                    ),
                )
            self.assertEqual(raised.exception.code, "HUB_SSE_TEXT_TOO_LARGE")

            structural = protocol.AnthropicStreamBridge("openai_responses")
            with self.assertRaises(protocol.ProtocolTransformError) as raised:
                structural.feed(
                    "response.reasoning_summary_part.added",
                    json.dumps(
                        {
                            "type": "response.reasoning_summary_part.added",
                            "item_id": "rs_large",
                            "output_index": 0,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": "hello"},
                        }
                    ),
                )
            self.assertEqual(raised.exception.code, "HUB_SSE_TEXT_TOO_LARGE")

            aggregate = protocol.AnthropicStreamBridge("openai_responses")
            aggregate.feed(
                "response.output_text.done",
                json.dumps(
                    {
                        "type": "response.output_text.done",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "1234",
                    }
                ),
            )
            with self.assertRaises(protocol.ProtocolTransformError) as raised:
                aggregate.feed(
                    "response.output_text.done",
                    json.dumps(
                        {
                            "type": "response.output_text.done",
                            "output_index": 0,
                            "content_index": 1,
                            "text": "5678",
                        }
                    ),
                )
            self.assertEqual(raised.exception.code, "HUB_SSE_TEXT_TOO_LARGE")
            self.assertEqual(aggregate.response_text_total_bytes, 4)
        finally:
            protocol.MAX_STREAM_TEXT_BYTES = original_limit
            protocol.MAX_STREAM_TEXT_TOTAL_BYTES = original_total_limit

    def test_stream_resource_caps_reject_aggregate_tool_arguments(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_large",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"value":"',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
        )
        half = "x" * (protocol.MAX_STREAM_TOOL_ARGUMENT_BYTES // 2)
        bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": half},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            bridge.feed(
                "message",
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": half},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_SSE_TOOL_ARGUMENTS_TOO_LARGE",
        )

    def test_responses_text_snapshots_repair_suffix_or_reject_conflicts(self) -> None:
        repaired = protocol.AnthropicStreamBridge("openai_responses")
        chunks = repaired.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "hel",
                }
            ),
        )
        chunks.extend(
            repaired.feed(
                "response.output_text.done",
                json.dumps(
                    {
                        "type": "response.output_text.done",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "hello",
                    }
                ),
            )
        )
        self.assertIn(b'"text":"lo"', b"".join(chunks))

        conflict = protocol.AnthropicStreamBridge("openai_responses")
        conflict.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "alpha",
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            conflict.feed(
                "response.output_text.done",
                json.dumps(
                    {
                        "type": "response.output_text.done",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "beta",
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

    def test_responses_message_snapshot_is_not_silently_discarded(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.done",
            json.dumps(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "snapshot answer"}
                        ],
                    },
                }
            ),
        )
        self.assertIn(b'"text":"snapshot answer"', b"".join(chunks))

    def test_stream_error_redacts_vendor_message_and_malformed_calls_fail(self) -> None:
        responses = protocol.AnthropicStreamBridge("openai_responses")
        rendered = b"".join(
            responses.feed(
                "response.failed",
                json.dumps(
                    {
                        "type": "response.failed",
                        "response": {
                            "error": {
                                "message": (
                                    "quota exhausted token=secret "
                                    "https://private.invalid/path"
                                )
                            }
                        },
                    }
                ),
            )
        )
        self.assertNotIn(b"secret", rendered)
        self.assertNotIn(b"private.invalid", rendered)
        # Sanitized detail is forwarded rather than dropped, so the reason the
        # upstream gave survives while the credential shapes do not.
        self.assertIn(b"quota exhausted", rendered)
        self.assertIn(b"token=[redacted]", rendered)
        self.assertIn(b"[redacted-url]", rendered)
        self.assertNotIn(
            "HUB_DEGRADE_UPSTREAM_ERROR_DETAIL_DROPPED",
            responses.warning_codes,
        )

        # The degradation code is still the honest signal when an error is
        # present but yields no forwardable evidence at all.
        opaque = protocol.AnthropicStreamBridge("openai_responses")
        opaque.feed(
            "response.failed",
            json.dumps(
                {
                    "type": "response.failed",
                    "response": {"error": {"message": "   "}},
                }
            ),
        )
        self.assertIn(
            "HUB_DEGRADE_UPSTREAM_ERROR_DETAIL_DROPPED",
            opaque.warning_codes,
        )

        for response in (
            "malformed",
            {"status": "completed", "error": {"message": "wrong status"}},
            {"status": "failed", "error": "malformed"},
            {"status": "failed", "future_response_field": True},
            {
                "status": "failed",
                "error": {"message": "boom"},
                "output": [{"type": "message", "content": []}],
            },
        ):
            with self.subTest(response=response):
                malformed_failure = protocol.AnthropicStreamBridge(
                    "openai_responses"
                )
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    malformed_failure.feed(
                        "response.failed",
                        json.dumps(
                            {"type": "response.failed", "response": response}
                        ),
                    )
                self.assertIn(
                    raised.exception.code,
                    {
                        "HUB_UPSTREAM_RESPONSE_INVALID",
                        "HUB_SSE_DUPLICATE_CONFLICT",
                        "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                    },
                )

        malformed_error = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            malformed_error.feed(
                "response.failed",
                json.dumps(
                    {"type": "response.failed", "error": "malformed"}
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_RESPONSE_INVALID",
        )

        completed_with_error = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            completed_with_error.feed(
                "response.completed",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "error": {"message": "conflict"},
                        },
                    }
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_RESPONSE_INVALID",
        )

        malformed = protocol.AnthropicStreamBridge("openai_chat")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            malformed.feed(
                "message",
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {"tool_calls": [None]},
                                "finish_reason": None,
                            }
                        ]
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_CALL_INVALID")

    def test_thinking_signature_is_emitted_only_when_upstream_supplies_it(self) -> None:
        signed = protocol.AnthropicStreamBridge("openai_chat")
        signed_chunks = signed.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "think",
                                "reasoning_signature": "real-upstream-signature",
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
        )
        signed.feed(
            "message",
            json.dumps(
                {"choices": [{"delta": {}, "finish_reason": "stop"}]}
            ),
        )
        signed_chunks.extend(signed.finish())
        signed_events = _payloads(signed_chunks)
        thinking_start = next(
            event
            for event in signed_events
            if event["type"] == "content_block_start"
        )
        self.assertNotIn("signature", thinking_start["content_block"])
        self.assertEqual(
            [
                event["delta"]
                for event in signed_events
                if event["type"] == "content_block_delta"
            ],
            [
                {"type": "thinking_delta", "thinking": "think"},
                {
                    "type": "signature_delta",
                    "signature": "real-upstream-signature",
                },
            ],
        )
        self.assertNotIn("HUB_DEGRADE_UNSIGNED_THINKING", signed.warning_codes)

        unsigned = protocol.AnthropicStreamBridge("openai_chat")
        unsigned_chunks = unsigned.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"reasoning_content": "think"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ),
        )
        unsigned_chunks.extend(unsigned.finish())
        rendered = b"".join(unsigned_chunks)
        self.assertNotIn(b'"signature":""', rendered)
        self.assertNotIn(b'"type":"signature_delta"', rendered)
        self.assertIn("HUB_DEGRADE_UNSIGNED_THINKING", unsigned.warning_codes)

    def test_tool_json_snapshot_repairs_partial_delta_and_invalid_final_json_fails(self) -> None:
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
        bridge.feed(
            "response.completed",
            json.dumps(
                {"type": "response.completed", "response": {"status": "completed"}}
            ),
        )
        chunks.extend(bridge.finish())
        events = _payloads(chunks)
        fragments = [
            event["delta"]["partial_json"]
            for event in events
            if event["type"] == "content_block_delta"
            and event["delta"]["type"] == "input_json_delta"
        ]
        self.assertEqual(fragments, ['{"q":', '"x"}'])
        self.assertEqual(json.loads("".join(fragments)), {"q": "x"})

        invalid = protocol.AnthropicStreamBridge("openai_chat")
        invalid.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_bad",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"q":',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            invalid.finish()
        self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_ARGUMENTS_INVALID")

    def test_streamed_tool_calls_require_an_explicit_json_object(self) -> None:
        chat = protocol.AnthropicStreamBridge("openai_chat")
        chat.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_empty",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": "",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            chat.finish()
        self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_ARGUMENTS_INVALID")

        responses = protocol.AnthropicStreamBridge("openai_responses")
        responses.feed(
            "response.output_item.added",
            json.dumps(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "id": "fc_empty",
                        "call_id": "call_empty",
                        "name": "lookup",
                    },
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            responses.feed(
                "response.function_call_arguments.done",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 0,
                        "arguments": "",
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_ARGUMENTS_INVALID")

        terminal_only = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            terminal_only.feed(
                "response.completed",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "function_call",
                                    "id": "fc_final_empty",
                                    "call_id": "call_final_empty",
                                    "name": "lookup",
                                    "status": "completed",
                                    "arguments": "",
                                }
                            ],
                        },
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_ARGUMENTS_INVALID")

    def test_responses_unmatched_tool_argument_events_fail_closed(self) -> None:
        cases = (
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "delta": '{"q":"x"}',
                },
            ),
            (
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "arguments": '{"q":"x"}',
                },
            ),
            (
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "call_id": "unknown_call",
                    "arguments": "",
                },
            ),
        )
        for event, payload in cases:
            with self.subTest(event=event, payload=payload):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_SSE_ORDER_VIOLATION",
                )

        ambiguous = protocol.AnthropicStreamBridge("openai_responses")
        for name in ("first", "second"):
            ambiguous.feed(
                "response.output_item.added",
                json.dumps(
                    {
                        "type": "response.output_item.added",
                        "item": {"type": "function_call", "name": name},
                    }
                ),
            )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            ambiguous.feed(
                "response.function_call_arguments.delta",
                json.dumps(
                    {
                        "type": "response.function_call_arguments.delta",
                        "delta": '{"ambiguous":true}',
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

    def test_responses_tool_argument_delta_requires_a_string_fragment(self) -> None:
        for payload in (
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_1",
                "delta": 123,
            },
            {
                "type": "response.function_call_arguments.delta",
                "call_id": "call_1",
            },
        ):
            with self.subTest(payload=payload):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                bridge.feed(
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
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.function_call_arguments.delta",
                        json.dumps(payload),
                    )
                self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_CALL_INVALID")

    def test_responses_completed_tool_snapshots_are_idempotent_across_done_and_terminal(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.added",
            json.dumps(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
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
                        "output_index": 0,
                        "call_id": "call_1",
                        "arguments": '{"q":"x"}',
                    }
                ),
            )
        )
        self.assertEqual(
            bridge.feed(
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "lookup",
                            "status": "completed",
                            "arguments": '{"q":"x"}',
                        },
                    }
                ),
            ),
            [],
        )
        self.assertEqual(
            bridge.feed(
                "response.completed",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "output": [
                                {
                                    "type": "function_call",
                                    "id": "fc_1",
                                    "call_id": "call_1",
                                    "name": "lookup",
                                    "status": "completed",
                                    "arguments": '{"q":"x"}',
                                }
                            ],
                        },
                    }
                ),
            ),
            [],
        )
        chunks.extend(bridge.finish())
        self.assertEqual(
            sum(
                event["type"] == "content_block_start"
                and event["content_block"]["type"] == "tool_use"
                for event in _payloads(chunks)
            ),
            1,
        )

    def test_responses_terminal_only_function_call_snapshot_is_reconstructed(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.completed",
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {
                                "type": "function_call",
                                "id": "fc_final",
                                "call_id": "call_final",
                                "name": "lookup",
                                "status": "completed",
                                "arguments": '{"q":"x"}',
                            }
                        ],
                    },
                }
            ),
        )
        chunks.extend(bridge.finish())
        events = _payloads(chunks)
        tool = next(
            event["content_block"]
            for event in events
            if event["type"] == "content_block_start"
        )
        self.assertEqual(tool["id"], "call_final")
        self.assertEqual(tool["name"], "lookup")
        self.assertEqual(
            "".join(
                event["delta"]["partial_json"]
                for event in events
                if event["type"] == "content_block_delta"
                and event["delta"]["type"] == "input_json_delta"
            ),
            '{"q":"x"}',
        )

    def test_responses_terminal_only_output_index_tool_id_is_observable(self) -> None:
        payload = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "lookup",
                        "status": "completed",
                        "arguments": '{"q":"x"}',
                    }
                ],
            },
        }
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        events = _payloads(
            bridge.feed("response.completed", json.dumps(payload))
        )
        tool = next(
            event["content_block"]
            for event in events
            if event["type"] == "content_block_start"
        )
        self.assertEqual(tool["id"], "0")
        self.assertIn("HUB_DEGRADE_SYNTHETIC_TOOL_ID", bridge.warning_codes)

        strict = protocol.AnthropicStreamBridge(
            "openai_responses",
            compatibility_mode="strict",
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            strict.feed("response.completed", json.dumps(payload))
        self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_CALL_INVALID")

    def test_responses_function_call_done_requires_a_matching_open_call(self) -> None:
        for item in (
            {"type": "function_call"},
            {
                "type": "function_call",
                "id": "fc_orphan",
                "call_id": "call_orphan",
                "arguments": "{}",
            },
        ):
            with self.subTest(item=item):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.output_item.done",
                        json.dumps(
                            {
                                "type": "response.output_item.done",
                                "item": item,
                            }
                        ),
                    )
                self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

    def test_responses_stream_function_call_shapes_fail_closed(self) -> None:
        invalid_items = (
            {"type": "function_call", "id": 1, "call_id": "call_1", "name": "lookup"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": 3},
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": 123,
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup",
                "future_field": True,
            },
        )
        for item in invalid_items:
            with self.subTest(item=item):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.output_item.added",
                        json.dumps(
                            {"type": "response.output_item.added", "item": item}
                        ),
                    )
                self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_CALL_INVALID")

    def test_responses_tool_event_identities_require_typed_consistent_aliases(self) -> None:
        for payload, expected_code in (
            (
                {
                    "type": "response.function_call_arguments.delta",
                    "call_id": 7,
                    "delta": "{}",
                },
                "HUB_SSE_TOOL_CALL_INVALID",
            ),
            (
                {
                    "type": "response.function_call_arguments.delta",
                    "call_id": "call_1",
                    "output_index": True,
                    "delta": "{}",
                },
                "HUB_SSE_TOOL_CALL_INVALID",
            ),
            (
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "call_id": "other_call",
                    "delta": "{}",
                },
                "HUB_SSE_ORDER_VIOLATION",
            ),
        ):
            with self.subTest(payload=payload):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                bridge.feed(
                    "response.output_item.added",
                    json.dumps(
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "type": "function_call",
                                "id": "fc_1",
                                "call_id": "call_1",
                                "name": "lookup",
                            },
                        }
                    ),
                )
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.function_call_arguments.delta",
                        json.dumps(payload),
                    )
                self.assertEqual(raised.exception.code, expected_code)

        distinct = protocol.AnthropicStreamBridge("openai_responses")
        chunks: list[bytes] = []
        for output_index, item_id, call_id, name in (
            (0, "1", "call_1", "first"),
            (1, "2", "call_2", "second"),
        ):
            chunks.extend(
                distinct.feed(
                    "response.output_item.added",
                    json.dumps(
                        {
                            "type": "response.output_item.added",
                            "output_index": output_index,
                            "item": {
                                "type": "function_call",
                                "id": item_id,
                                "call_id": call_id,
                                "name": name,
                            },
                        }
                    ),
                )
            )
        self.assertEqual(
            [
                event["content_block"]["name"]
                for event in _payloads(chunks)
                if event["type"] == "content_block_start"
            ],
            ["first", "second"],
        )

        for event, field_name in (
            ("response.function_call_arguments.done", "arguments"),
            ("response.output_item.done", "item"),
        ):
            with self.subTest(event=event):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                bridge.feed(
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
                payload = {
                    "type": event,
                    "call_id": "call_1",
                    "arguments": 123,
                }
                if field_name == "item":
                    payload = {
                        "type": event,
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "arguments": 123,
                        },
                    }
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_CALL_INVALID")

    def test_terminal_late_duplicate_and_usage_regression_fail_closed(self) -> None:
        completed = protocol.AnthropicStreamBridge("openai_responses")
        terminal = json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
            }
        )
        completed.feed("response.completed", terminal)

        with self.assertRaises(protocol.ProtocolTransformError) as late:
            completed.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "late",
                    }
                ),
            )
        self.assertEqual(late.exception.code, "HUB_SSE_LATE_EVENT")

        with self.assertRaises(protocol.ProtocolTransformError) as duplicate:
            completed.feed("response.completed", terminal)
        self.assertEqual(duplicate.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

        regressing = protocol.AnthropicStreamBridge("openai_chat")
        regressing.feed(
            "message",
            json.dumps(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as usage:
            regressing.feed(
                "message",
                json.dumps(
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 9, "completion_tokens": 4},
                    }
                ),
            )
        self.assertEqual(usage.exception.code, "HUB_SSE_USAGE_REGRESSION")

    def test_stream_usage_rejects_malformed_shapes_instead_of_marking_unavailable(self) -> None:
        cases = (
            (
                protocol.AnthropicStreamBridge("openai_chat"),
                "message",
                {
                    "usage": "bad",
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                },
            ),
            (
                protocol.AnthropicStreamBridge("openai_responses"),
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "usage": "bad"},
                },
            ),
        )
        for bridge, event, payload in cases:
            with self.subTest(event=event):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_responses_terminal_requires_an_explicit_consistent_response_status(self) -> None:
        invalid_cases = (
            ("response.completed", {"type": "response.completed"}, "HUB_UPSTREAM_RESPONSE_INVALID"),
            (
                "response.completed",
                {"type": "response.completed", "response": "bad"},
                "HUB_UPSTREAM_RESPONSE_INVALID",
            ),
            (
                "response.completed",
                {"type": "response.completed", "response": {}},
                "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
                },
                "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            ),
            (
                "response.incomplete",
                {"type": "response.incomplete", "response": {"status": "completed"}},
                "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            ),
            (
                "response.incomplete",
                {"type": "response.incomplete", "response": {"status": "incomplete"}},
                "HUB_UPSTREAM_STOP_REASON_UNMAPPABLE",
            ),
        )
        for event, payload, expected_code in invalid_cases:
            with self.subTest(event=event, payload=payload):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(raised.exception.code, expected_code)

        incomplete = protocol.AnthropicStreamBridge("openai_responses")
        incomplete.feed(
            "response.incomplete",
            json.dumps(
                {
                    "type": "response.incomplete",
                    "response": {
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                    },
                }
            ),
        )
        terminal = next(
            event
            for event in _payloads(incomplete.finish())
            if event["type"] == "message_delta"
        )
        self.assertEqual(terminal["delta"]["stop_reason"], "max_tokens")

    def test_responses_terminal_snapshot_cannot_hide_output_or_unknown_fields(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.completed",
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_final",
                        "model": "responses-model",
                        "status": "completed",
                        "output": [
                            {
                                "id": "msg_final",
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {"type": "output_text", "text": "final-only"}
                                ],
                            }
                        ],
                    },
                }
            ),
        )
        chunks.extend(bridge.finish())
        self.assertEqual(
            "".join(
                event["delta"]["text"]
                for event in _payloads(chunks)
                if event["type"] == "content_block_delta"
                and event["delta"]["type"] == "text_delta"
            ),
            "final-only",
        )

        unknown = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            unknown.feed(
                "response.completed",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "future_response_field": True,
                        },
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_RESPONSE_INVALID")

    def test_streamed_refusal_uses_refusal_stop_reason_for_both_adapters(self) -> None:
        chat = protocol.AnthropicStreamBridge("openai_chat")
        chat_chunks = chat.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"refusal": "cannot comply"},
                            "finish_reason": "content_filter",
                        }
                    ]
                }
            ),
        )
        chat_chunks.extend(chat.finish())

        responses = protocol.AnthropicStreamBridge("openai_responses")
        response_chunks = responses.feed(
            "response.refusal.delta",
            json.dumps(
                {
                    "type": "response.refusal.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "cannot comply",
                }
            ),
        )
        responses.feed(
            "response.completed",
            json.dumps(
                {"type": "response.completed", "response": {"status": "completed"}}
            ),
        )
        response_chunks.extend(responses.finish())

        for chunks in (chat_chunks, response_chunks):
            events = _payloads(chunks)
            text = "".join(
                event["delta"]["text"]
                for event in events
                if event["type"] == "content_block_delta"
                and event["delta"]["type"] == "text_delta"
            )
            terminal = next(
                event for event in events if event["type"] == "message_delta"
            )
            self.assertEqual(text, "cannot comply")
            self.assertEqual(terminal["delta"]["stop_reason"], "refusal")

    def test_responses_encrypted_reasoning_streams_as_provenance_tagged_redaction(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.done",
            json.dumps(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "reasoning",
                        "encrypted_content": "real-stream-opaque-value",
                        "summary": [],
                    },
                }
            ),
        )
        bridge.feed(
            "response.completed",
            json.dumps(
                {"type": "response.completed", "response": {"status": "completed"}}
            ),
        )
        chunks.extend(bridge.finish())
        events = _payloads(chunks)
        block = next(
            event["content_block"]
            for event in events
            if event["type"] == "content_block_start"
        )
        self.assertEqual(block["type"], "redacted_thinking")
        self.assertNotEqual(block["data"], "real-stream-opaque-value")

        replay = protocol.prepare_request(
            {
                "model": "responses-model",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [block],
                    }
                ],
            },
            "openai_responses",
        )
        reasoning = next(
            item for item in replay.payload["input"] if item.get("type") == "reasoning"
        )
        self.assertEqual(
            reasoning["encrypted_content"], "real-stream-opaque-value"
        )

    def test_responses_final_reasoning_snapshot_preserves_summary_and_redaction(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        chunks = bridge.feed(
            "response.output_item.done",
            json.dumps(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "rs_final",
                        "type": "reasoning",
                        "encrypted_content": "real-final-opaque-value",
                        "summary": [
                            {"type": "summary_text", "text": "final reason"}
                        ],
                    },
                }
            ),
        )
        bridge.feed(
            "response.completed",
            json.dumps(
                {"type": "response.completed", "response": {"status": "completed"}}
            ),
        )
        chunks.extend(bridge.finish())
        events = _payloads(chunks)
        block_types = [
            event["content_block"]["type"]
            for event in events
            if event["type"] == "content_block_start"
        ]
        self.assertEqual(block_types, ["redacted_thinking", "thinking"])
        self.assertEqual(
            "".join(
                event["delta"]["thinking"]
                for event in events
                if event["type"] == "content_block_delta"
                and event["delta"]["type"] == "thinking_delta"
            ),
            "final reason",
        )

    def test_responses_redacted_reasoning_snapshot_is_idempotent_at_terminal(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        item = {
            "id": "rs_1",
            "type": "reasoning",
            "status": "completed",
            "encrypted_content": "real-opaque-value",
            "summary": [],
        }
        chunks = bridge.feed(
            "response.output_item.done",
            json.dumps(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": item,
                }
            ),
        )
        chunks.extend(
            bridge.feed(
                "response.completed",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {"status": "completed", "output": [item]},
                    }
                ),
            )
        )
        chunks.extend(bridge.finish())
        self.assertEqual(
            sum(
                event["type"] == "content_block_start"
                and event["content_block"]["type"] == "redacted_thinking"
                for event in _payloads(chunks)
            ),
            1,
        )

    def test_responses_redacted_reasoning_identity_enrichment_is_idempotent(self) -> None:
        for initial_id, terminal_id in ((None, "rs_enriched"), ("rs_known", None)):
            with self.subTest(initial_id=initial_id, terminal_id=terminal_id):
                initial_item = {
                    "type": "reasoning",
                    "status": "completed",
                    "encrypted_content": "same-opaque-value",
                    "summary": [],
                }
                terminal_item = dict(initial_item)
                if initial_id is not None:
                    initial_item["id"] = initial_id
                if terminal_id is not None:
                    terminal_item["id"] = terminal_id
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                chunks = bridge.feed(
                    "response.output_item.done",
                    json.dumps(
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": initial_item,
                        }
                    ),
                )
                chunks.extend(
                    bridge.feed(
                        "response.completed",
                        json.dumps(
                            {
                                "type": "response.completed",
                                "response": {
                                    "status": "completed",
                                    "output": [terminal_item],
                                },
                            }
                        ),
                    )
                )
                self.assertEqual(
                    sum(
                        event["type"] == "content_block_start"
                        and event["content_block"]["type"]
                        == "redacted_thinking"
                        for event in _payloads(chunks)
                    ),
                    1,
                )

    def test_responses_terminal_output_item_identity_and_type_conflicts(self) -> None:
        cases = (
            (
                {
                    "type": "message",
                    "id": "msg_a",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
                {
                    "type": "reasoning",
                    "id": "rs_b",
                    "status": "completed",
                    "encrypted_content": "opaque",
                    "summary": [],
                },
            ),
            (
                {
                    "type": "reasoning",
                    "id": "rs_a",
                    "status": "completed",
                    "encrypted_content": "opaque",
                    "summary": [],
                },
                {
                    "type": "message",
                    "id": "msg_b",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            ),
            (
                {
                    "type": "message",
                    "id": "msg_a",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
                {
                    "type": "message",
                    "id": "msg_b",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            ),
        )
        for initial_item, terminal_item in cases:
            with self.subTest(
                initial_type=initial_item["type"],
                terminal_type=terminal_item["type"],
            ):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                bridge.feed(
                    "response.output_item.done",
                    json.dumps(
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": initial_item,
                        }
                    ),
                )
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.completed",
                        json.dumps(
                            {
                                "type": "response.completed",
                                "response": {
                                    "status": "completed",
                                    "output": [terminal_item],
                                },
                            }
                        ),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_SSE_DUPLICATE_CONFLICT",
                )

    def test_stream_response_identity_is_locked_at_first_observation(self) -> None:
        chat = protocol.AnthropicStreamBridge("openai_chat")
        chat.feed(
            "message",
            json.dumps(
                {
                    "id": "chat_a",
                    "model": "model_a",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "a"},
                            "finish_reason": None,
                        }
                    ],
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            chat.feed(
                "message",
                json.dumps(
                    {
                        "id": "chat_b",
                        "model": "model_b",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "b"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

        responses = protocol.AnthropicStreamBridge("openai_responses")
        responses.feed(
            "response.created",
            json.dumps(
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp_a",
                        "model": "model_a",
                        "status": "in_progress",
                    },
                }
            ),
        )
        responses.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "answer",
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            responses.feed(
                "response.completed",
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_b",
                            "model": "model_b",
                            "status": "completed",
                        },
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

    def test_responses_final_reasoning_snapshot_rejects_unclassified_fields(self) -> None:
        invalid_items = (
            (
                {"type": "reasoning", "encrypted_content": 7, "summary": []},
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {"type": "reasoning", "content": [{"type": "reasoning_text"}]},
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "hidden", "future": True}
                    ],
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {"type": "reasoning", "summary": [], "future": True},
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {"type": "reasoning", "summary": [], "status": "in_progress"},
                "HUB_SSE_DUPLICATE_CONFLICT",
            ),
        )
        for item, expected_code in invalid_items:
            with self.subTest(item=item):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.output_item.done",
                        json.dumps(
                            {
                                "type": "response.output_item.done",
                                "output_index": 0,
                                "item": item,
                            }
                        ),
                    )
                self.assertEqual(raised.exception.code, expected_code)

    def test_responses_final_message_snapshot_enforces_role_status_and_allowlists(self) -> None:
        invalid_items = (
            (
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "output_text", "text": "hidden"}],
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "hidden", "logprobs": []}
                    ],
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hidden"}],
                    "future_field": True,
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [{"type": "output_text", "text": "hidden"}],
                },
                "HUB_SSE_DUPLICATE_CONFLICT",
            ),
        )
        for item, expected_code in invalid_items:
            with self.subTest(item=item):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.output_item.done",
                        json.dumps(
                            {
                                "type": "response.output_item.done",
                                "output_index": 0,
                                "item": item,
                            }
                        ),
                    )
                self.assertEqual(raised.exception.code, expected_code)

        identity = protocol.AnthropicStreamBridge("openai_responses")
        identity.feed(
            "response.content_part.added",
            json.dumps(
                {
                    "type": "response.content_part.added",
                    "item_id": "msg_a",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            identity.feed(
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": "msg_b",
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "hidden"}
                            ],
                        },
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

    def test_responses_lifecycle_event_statuses_cannot_conflict_with_snapshots(self) -> None:
        cases = (
            (
                "response.created",
                {"type": "response.created", "response": "bad"},
                "HUB_UPSTREAM_RESPONSE_INVALID",
            ),
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {"id": "resp_1", "status": "failed"},
                },
                "HUB_SSE_DUPLICATE_CONFLICT",
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [],
                    },
                },
                "HUB_SSE_DUPLICATE_CONFLICT",
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [{"type": "output_text", "text": "premature"}],
                    },
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
        )
        for event, payload, expected_code in cases:
            with self.subTest(event=event, payload=payload):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(raised.exception.code, expected_code)

    def test_unknown_stream_events_and_output_items_fail_closed(self) -> None:
        cases = [
            (
                protocol.AnthropicStreamBridge("openai_responses"),
                "response.future_event",
                {"type": "response.future_event", "opaque": True},
                "HUB_SSE_UNKNOWN_EVENT",
            ),
            (
                protocol.AnthropicStreamBridge("openai_responses"),
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "item": {"type": "code_interpreter_call", "id": "server_1"},
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
            (
                protocol.AnthropicStreamBridge("openai_chat"),
                "message",
                {
                    "choices": [
                        {
                            "delta": {"audio": {"data": "opaque"}},
                            "finish_reason": None,
                        }
                    ]
                },
                "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
            ),
        ]
        for bridge, event, payload, expected_code in cases:
            with self.subTest(event=event, expected_code=expected_code):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(raised.exception.code, expected_code)

    def test_responses_stream_payload_type_must_be_a_string_when_present(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            bridge.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": 7,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "hidden",
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_UNKNOWN_EVENT")

    def test_responses_text_and_reasoning_events_require_valid_coordinates(self) -> None:
        cases = (
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "hidden"},
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "output_index": -1,
                    "content_index": 0,
                    "delta": "hidden",
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": True,
                    "delta": "hidden",
                },
            ),
            (
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "output_index": 0,
                    "delta": "hidden",
                },
            ),
        )
        for event, payload in cases:
            with self.subTest(event=event, payload=payload):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

    def test_responses_event_wrappers_reject_unknown_fields_and_track_metadata_loss(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            bridge.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "hidden",
                        "future_field": True,
                    }
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )

        malformed_sequence = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            malformed_sequence.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": True,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "hidden",
                    }
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )

        degraded = protocol.AnthropicStreamBridge("openai_responses")
        degraded.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "visible",
                }
            ),
        )
        self.assertIn(
            "HUB_DEGRADE_STREAM_SEQUENCE_METADATA_DROPPED",
            degraded.warning_codes,
        )

    def test_chat_stream_text_carriers_and_role_reject_malformed_values(self) -> None:
        invalid_deltas = (
            {"content": {"text": "hidden"}},
            {"reasoning_content": 7},
            {"refusal": ["hidden"]},
            {"role": "user"},
            {"role": 3},
        )
        for delta in invalid_deltas:
            with self.subTest(delta=delta):
                bridge = protocol.AnthropicStreamBridge("openai_chat")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "message",
                        json.dumps(
                            {
                                "choices": [
                                    {"delta": delta, "finish_reason": "stop"}
                                ]
                            }
                        ),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_chat_stream_signature_aliases_cannot_conflict_or_hide_each_other(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            bridge.feed(
                "message",
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "reasoning_content": "think",
                                    "reasoning_signature": "sig-a",
                                    "signature": "sig-b",
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

        hidden = protocol.AnthropicStreamBridge("openai_chat")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            hidden.feed(
                "message",
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "reasoning_content": "think",
                                    "reasoning_signature": None,
                                    "signature": "real-signature",
                                },
                                "finish_reason": "stop",
                            }
                        ]
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_SIGNATURE_ORDER")

    def test_chat_stream_tool_discriminator_and_index_fail_closed(self) -> None:
        invalid_calls = (
            {
                "index": 0,
                "id": "call_1",
                "type": "server_tool",
                "function": {"name": "lookup", "arguments": "{}"},
            },
            {
                "index": -1,
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            },
            {
                "index": True,
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            },
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                bridge = protocol.AnthropicStreamBridge("openai_chat")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "message",
                        json.dumps(
                            {
                                "choices": [
                                    {
                                        "delta": {"tool_calls": [call]},
                                        "finish_reason": "tool_calls",
                                    }
                                ]
                            }
                        ),
                    )
                self.assertEqual(raised.exception.code, "HUB_SSE_TOOL_CALL_INVALID")

    def test_chat_stream_wrapper_fields_are_rejected_or_observably_degraded(self) -> None:
        invalid_payloads = (
            {
                "future_top_level": True,
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": 7,
                "choices": [{"delta": {}, "finish_reason": "stop"}],
            },
            {
                "choices": [
                    {"delta": {}, "finish_reason": "stop", "future_choice": True}
                ],
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                bridge = protocol.AnthropicStreamBridge("openai_chat")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed("message", json.dumps(payload))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

        degraded = protocol.AnthropicStreamBridge("openai_chat")
        degraded.feed(
            "message",
            json.dumps(
                {
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "service_tier": "default",
                    "system_fingerprint": "fp_fixture",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "logprobs": [],
                            "finish_reason": "stop",
                        }
                    ],
                }
            ),
        )
        self.assertIn(
            "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
            degraded.warning_codes,
        )

    def test_responses_structural_parts_preserve_exact_snapshots(self) -> None:
        text = protocol.AnthropicStreamBridge("openai_responses")
        text_chunks: list[bytes] = []
        text_events = (
            (
                "response.content_part.added",
                {
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "text": "",
                        "annotations": [],
                    },
                },
            ),
            (
                "response.output_text.delta",
                {
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "hel",
                },
            ),
            (
                "response.output_text.done",
                {
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "hello",
                },
            ),
            (
                "response.content_part.done",
                {
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "text": "hello",
                        "annotations": [],
                    },
                },
            ),
        )
        for event, payload in text_events:
            text_chunks.extend(
                text.feed(event, json.dumps({"type": event, **payload}))
            )
        self.assertEqual(
            "".join(
                event["delta"]["text"]
                for event in _payloads(text_chunks)
                if event["type"] == "content_block_delta"
            ),
            "hello",
        )
        self.assertFalse(text.response_text_fragments)
        self.assertEqual(
            text.feed(
                "response.content_part.done",
                json.dumps(
                    {
                        "type": "response.content_part.done",
                        **text_events[-1][1],
                    }
                ),
            ),
            [],
        )
        text_chunks.extend(
            text.feed(
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": "msg_1",
                            "type": "message",
                            "content": [text_events[-1][1]["part"]],
                        },
                    }
                ),
            )
        )
        text.feed(
            "response.completed",
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                }
            ),
        )
        text_chunks.extend(text.finish())
        text_payloads = _payloads(text_chunks)
        self.assertEqual(
            "".join(
                event["delta"]["text"]
                for event in text_payloads
                if event["type"] == "content_block_delta"
            ),
            "hello",
        )
        self.assertEqual(
            sum(event["type"] == "message_stop" for event in text_payloads),
            1,
        )

        refusal = protocol.AnthropicStreamBridge("openai_responses")
        refusal_chunks: list[bytes] = []
        for event, payload in (
            (
                "response.content_part.added",
                {
                    "item_id": "msg_refusal",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "refusal", "refusal": "can"},
                },
            ),
            (
                "response.refusal.delta",
                {
                    "item_id": "msg_refusal",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "not",
                },
            ),
            (
                "response.refusal.done",
                {
                    "item_id": "msg_refusal",
                    "output_index": 0,
                    "content_index": 0,
                    "refusal": "cannot",
                },
            ),
            (
                "response.content_part.done",
                {
                    "item_id": "msg_refusal",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "refusal", "refusal": "cannot"},
                },
            ),
        ):
            refusal_chunks.extend(
                refusal.feed(event, json.dumps({"type": event, **payload}))
            )
        self.assertEqual(
            "".join(
                event["delta"]["text"]
                for event in _payloads(refusal_chunks)
                if event["type"] == "content_block_delta"
            ),
            "cannot",
        )
        self.assertTrue(refusal.refused)

        summary = protocol.AnthropicStreamBridge("openai_responses")
        summary_chunks: list[bytes] = []
        summary_done = {
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": "reason"},
        }
        for event, payload in (
            (
                "response.reasoning_summary_part.added",
                {
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": "rea"},
                },
            ),
            (
                "response.reasoning_summary_text.delta",
                {
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": "son",
                },
            ),
            (
                "response.reasoning_summary_text.done",
                {
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "text": "reason",
                },
            ),
            ("response.reasoning_summary_part.done", summary_done),
        ):
            summary_chunks.extend(
                summary.feed(event, json.dumps({"type": event, **payload}))
            )
        self.assertEqual(
            "".join(
                event["delta"]["thinking"]
                for event in _payloads(summary_chunks)
                if event["type"] == "content_block_delta"
            ),
            "reason",
        )
        self.assertFalse(summary.response_reasoning_fragments)
        self.assertEqual(
            summary.feed(
                "response.reasoning_summary_part.done",
                json.dumps(
                    {
                        "type": "response.reasoning_summary_part.done",
                        **summary_done,
                    }
                ),
            ),
            [],
        )
        summary_chunks.extend(
            summary.feed(
                "response.output_item.done",
                json.dumps(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": "rs_1",
                            "type": "reasoning",
                            "summary": [summary_done["part"]],
                        },
                    }
                ),
            )
        )
        summary.feed(
            "response.completed",
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                }
            ),
        )
        summary_chunks.extend(summary.finish())
        summary_payloads = _payloads(summary_chunks)
        self.assertEqual(
            "".join(
                event["delta"]["thinking"]
                for event in summary_payloads
                if event["type"] == "content_block_delta"
            ),
            "reason",
        )
        self.assertEqual(
            sum(event["type"] == "message_stop" for event in summary_payloads),
            1,
        )

    def test_responses_structural_part_conflicts_fail_closed(self) -> None:
        conflict = protocol.AnthropicStreamBridge("openai_responses")
        conflict.feed(
            "response.content_part.added",
            json.dumps(
                {
                    "type": "response.content_part.added",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                }
            ),
        )
        conflict.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "alpha",
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            conflict.feed(
                "response.content_part.done",
                json.dumps(
                    {
                        "type": "response.content_part.done",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "beta"},
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

        summary = protocol.AnthropicStreamBridge("openai_responses")
        summary.feed(
            "response.reasoning_summary_part.added",
            json.dumps(
                {
                    "type": "response.reasoning_summary_part.added",
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": "alpha"},
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            summary.feed(
                "response.reasoning_summary_text.done",
                json.dumps(
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "summary_index": 0,
                        "text": "beta",
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

    def test_responses_done_snapshots_repair_missing_delta_streams(self) -> None:
        content = protocol.AnthropicStreamBridge("openai_responses")
        content.feed(
            "response.content_part.added",
            json.dumps(
                {
                    "type": "response.content_part.added",
                    "item_id": "msg_snapshot",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                }
            ),
        )
        content_chunks = content.feed(
            "response.content_part.done",
            json.dumps(
                {
                    "type": "response.content_part.done",
                    "item_id": "msg_snapshot",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "snapshot text"},
                }
            ),
        )
        self.assertEqual(
            "".join(
                event["delta"]["text"]
                for event in _payloads(content_chunks)
                if event["type"] == "content_block_delta"
            ),
            "snapshot text",
        )

        summary = protocol.AnthropicStreamBridge("openai_responses")
        summary.feed(
            "response.reasoning_summary_part.added",
            json.dumps(
                {
                    "type": "response.reasoning_summary_part.added",
                    "item_id": "rs_snapshot",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                }
            ),
        )
        summary_chunks = summary.feed(
            "response.reasoning_summary_part.done",
            json.dumps(
                {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": "rs_snapshot",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": "summary snapshot"},
                }
            ),
        )
        self.assertEqual(
            "".join(
                event["delta"]["thinking"]
                for event in _payloads(summary_chunks)
                if event["type"] == "content_block_delta"
            ),
            "summary snapshot",
        )

        standalone = protocol.AnthropicStreamBridge("openai_responses")
        standalone_chunks = standalone.feed(
            "response.reasoning_summary_text.done",
            json.dumps(
                {
                    "type": "response.reasoning_summary_text.done",
                    "output_index": 0,
                    "summary_index": 0,
                    "text": "standalone snapshot",
                }
            ),
        )
        self.assertEqual(
            "".join(
                event["delta"]["thinking"]
                for event in _payloads(standalone_chunks)
                if event["type"] == "content_block_delta"
            ),
            "standalone snapshot",
        )

    def test_responses_structural_part_order_and_unknown_types_fail_closed(self) -> None:
        for event, payload in (
            (
                "response.content_part.done",
                {
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "orphan"},
                },
            ),
            (
                "response.reasoning_summary_part.done",
                {
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": "orphan"},
                },
            ),
        ):
            with self.subTest(event=event):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps({"type": event, **payload}))
                self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

        closed = protocol.AnthropicStreamBridge("openai_responses")
        for event in ("response.content_part.added", "response.content_part.done"):
            closed.feed(
                event,
                json.dumps(
                    {
                        "type": event,
                        "item_id": "msg_closed",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "done"},
                    }
                ),
            )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            closed.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "item_id": "msg_closed",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "late",
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

        mismatched = protocol.AnthropicStreamBridge("openai_responses")
        mismatched.feed(
            "response.reasoning_summary_part.added",
            json.dumps(
                {
                    "type": "response.reasoning_summary_part.added",
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            mismatched.feed(
                "response.reasoning_summary_text.delta",
                json.dumps(
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": "rs_1",
                        "output_index": 0,
                        "summary_index": 1,
                        "delta": "wrong index",
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

        unclosed = protocol.AnthropicStreamBridge("openai_responses")
        unclosed.feed(
            "response.content_part.added",
            json.dumps(
                {
                    "type": "response.content_part.added",
                    "item_id": "msg_unclosed",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "partial"},
                }
            ),
        )
        unclosed.feed(
            "response.completed",
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {"status": "completed"},
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            unclosed.finish()
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

        for event, payload in (
            (
                "response.content_part.added",
                {
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_audio", "audio": "opaque"},
                },
            ),
            (
                "response.reasoning_summary_part.added",
                {
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "future_summary", "text": "opaque"},
                },
            ),
            (
                "response.content_part.added",
                {
                    "item_id": "msg_extra",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "text": "opaque",
                        "logprobs": [],
                    },
                },
            ),
            (
                "response.content_part.added",
                {
                    "item_id": "msg_refusal_extra",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {
                        "type": "refusal",
                        "refusal": "opaque",
                        "annotations": [],
                    },
                },
            ),
            (
                "response.reasoning_summary_part.added",
                {
                    "item_id": "rs_extra",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {
                        "type": "summary_text",
                        "text": "opaque",
                        "status": "completed",
                    },
                },
            ),
        ):
            with self.subTest(event=event):
                bridge = protocol.AnthropicStreamBridge("openai_responses")
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps({"type": event, **payload}))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_stream_citation_metadata_requires_open_text_and_is_observable(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_responses")
        bridge.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "cited answer",
                }
            ),
        )
        self.assertEqual(
            bridge.feed(
                "response.output_text.annotation.added",
                json.dumps(
                    {
                        "type": "response.output_text.annotation.added",
                        "output_index": 0,
                        "content_index": 0,
                        "annotation": {
                            "type": "url_citation",
                            "url": "https://example.test/source",
                        },
                    }
                ),
            ),
            [],
        )
        self.assertIn(
            "HUB_DEGRADE_CITATION_METADATA_DROPPED",
            bridge.warning_codes,
        )

        out_of_order = protocol.AnthropicStreamBridge("openai_responses")
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            out_of_order.feed(
                "response.output_text.annotation.added",
                json.dumps(
                    {
                        "type": "response.output_text.annotation.added",
                        "annotation": {"type": "url_citation"},
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

        for payload in (
            {
                "type": "response.output_text.annotation.added",
                "output_index": 1,
                "content_index": 0,
                "annotation": {"type": "url_citation"},
            },
            {
                "type": "response.output_text.annotation.added",
                "output_index": 0,
                "content_index": -1,
                "annotation": {"type": "url_citation"},
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(
                        "response.output_text.annotation.added",
                        json.dumps(payload),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_SSE_ORDER_VIOLATION",
                )

        structural = protocol.AnthropicStreamBridge("openai_responses")
        structural.feed(
            "response.content_part.added",
            json.dumps(
                {
                    "type": "response.content_part.added",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "answer"},
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            structural.feed(
                "response.output_text.citation.added",
                json.dumps(
                    {
                        "type": "response.output_text.citation.added",
                        "item_id": "msg_other",
                        "output_index": 0,
                        "content_index": 0,
                        "citation_index": 0,
                        "citation": {"type": "url_citation"},
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_DUPLICATE_CONFLICT")

        structural.feed(
            "response.content_part.done",
            json.dumps(
                {
                    "type": "response.content_part.done",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "answer"},
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            structural.feed(
                "response.output_text.citation.added",
                json.dumps(
                    {
                        "type": "response.output_text.citation.added",
                        "item_id": "msg_1",
                        "output_index": 0,
                        "content_index": 0,
                        "citation_index": 0,
                        "citation": {"type": "url_citation"},
                    }
                ),
            )
        self.assertEqual(raised.exception.code, "HUB_SSE_ORDER_VIOLATION")

        for malformed_metadata in (
            {
                "type": "response.output_text.annotation.added",
                "output_index": 0,
                "content_index": 0,
            },
            {
                "type": "response.output_text.annotation.added",
                "output_index": 0,
                "content_index": 0,
                "annotation": "malformed",
            },
        ):
            malformed_bridge = protocol.AnthropicStreamBridge("openai_responses")
            malformed_bridge.feed(
                "response.output_text.delta",
                json.dumps(
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "answer",
                    }
                ),
            )
            with self.subTest(malformed_metadata=malformed_metadata):
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    malformed_bridge.feed(
                        "response.output_text.annotation.added",
                        json.dumps(malformed_metadata),
                    )
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

        strict = protocol.AnthropicStreamBridge(
            "openai_responses",
            compatibility_mode="strict",
        )
        strict.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "answer",
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            strict.feed(
                "response.output_text.annotation.added",
                json.dumps(
                    {
                        "type": "response.output_text.annotation.added",
                        "output_index": 0,
                        "content_index": 0,
                        "annotation": {"type": "url_citation"},
                    }
                ),
            )
        self.assertEqual(
            raised.exception.code,
            "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
        )

    def test_stream_usage_preserves_cache_split_and_server_tool_counters(self) -> None:
        bridge = protocol.AnthropicStreamBridge(
            "openai_chat",
            compatibility_mode="strict",
        )
        bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [],
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
                }
            ),
        )
        bridge.feed(
            "message",
            json.dumps(
                {"choices": [{"delta": {}, "finish_reason": "stop"}]}
            ),
        )
        events = _payloads(bridge.finish())
        final_usage = next(
            event["usage"] for event in events if event["type"] == "message_delta"
        )
        self.assertEqual(final_usage["cache_read_input_tokens"], 40)
        self.assertEqual(final_usage["cache_creation_input_tokens"], 12)
        self.assertEqual(
            final_usage["cache_creation"],
            {
                "ephemeral_5m_input_tokens": 7,
                "ephemeral_1h_input_tokens": 5,
            },
        )
        self.assertEqual(
            final_usage["server_tool_use"],
            {"web_search_requests": 2},
        )

    def test_stream_usage_registries_drop_unknown_fields_with_warning(self) -> None:
        cases = (
            (
                "openai_chat",
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "future_usage_counter": 4,
                    },
                },
            ),
            (
                "openai_responses",
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 1,
                            "future_usage_counter": 4,
                        },
                    },
                },
            ),
            (
                "openai_chat",
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 1},
                    },
                },
            ),
            (
                "openai_responses",
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 1,
                            "prompt_tokens_details": {"cached_tokens": 1},
                        },
                    },
                },
            ),
        )
        for api_format, event, payload in cases:
            with self.subTest(api_format=api_format):
                bridge = protocol.AnthropicStreamBridge(api_format)
                bridge.feed(event, json.dumps(payload))
                self.assertIn(
                    "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                    bridge.warning_codes,
                )

    def test_extra_usage_field_does_not_abort_finished_stream(self) -> None:
        # 兼容上游的真实序列：finish_reason 之后发一帧
        # choices=[] 的 usage 事件，其中 usage 带非标准顶层字段
        # reasoning_tokens。usage 只是统计回执，未知字段必须降级丢弃，
        # 不得把已经开始的下游流判成致命协议错误而掐断。
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        chunks = bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [
                        {"delta": {"content": "你好"}, "finish_reason": "stop"}
                    ]
                }
            ),
        )
        chunks.extend(
            bridge.feed(
                "message",
                json.dumps(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 16520,
                            "completion_tokens": 8,
                            "total_tokens": 16528,
                            "reasoning_tokens": 0,
                        },
                    },
                ),
            )
        )
        chunks.extend(bridge.feed("message", "[DONE]"))
        # 不抛异常、正常补齐 terminal 事件，是本回归测试的核心断言。
        chunks.extend(bridge.finish())
        events = [
            json.loads(data)
            for _, data in protocol.SSEParser().feed(b"".join(chunks))
        ]
        self.assertEqual(events[-1]["type"], "message_stop")
        self.assertIn(
            "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
            bridge.warning_codes,
        )

    def test_stream_usage_details_fail_closed_on_malformed_nested_shapes(self) -> None:
        cases = (
            (
                "openai_chat",
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "prompt_tokens_details": "malformed",
                    },
                },
            ),
            (
                "openai_chat",
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "completion_tokens_details": {"reasoning_tokens": False},
                    },
                },
            ),
            (
                "openai_responses",
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 1,
                            "output_tokens_details": ["malformed"],
                        },
                    },
                },
            ),
        )
        for api_format, event, payload in cases:
            with self.subTest(api_format=api_format, payload=payload):
                bridge = protocol.AnthropicStreamBridge(api_format)
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_stream_total_tokens_must_be_a_non_negative_counter(self) -> None:
        cases = (
            (
                "openai_chat",
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": False,
                    },
                },
            ),
            (
                "openai_responses",
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 1,
                            "total_tokens": -1,
                        },
                    },
                },
            ),
        )
        for api_format, event, payload in cases:
            with self.subTest(api_format=api_format):
                bridge = protocol.AnthropicStreamBridge(api_format)
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_stream_total_tokens_must_match_complete_snapshot_counters(self) -> None:
        cases = (
            (
                "openai_chat",
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "total_tokens": 5,
                    },
                },
            ),
            (
                "openai_responses",
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 3,
                            "output_tokens": 1,
                            "total_tokens": 5,
                        },
                    },
                },
            ),
        )
        for api_format, event, payload in cases:
            with self.subTest(api_format=api_format):
                bridge = protocol.AnthropicStreamBridge(api_format)
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    bridge.feed(event, json.dumps(payload))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_USAGE_INVALID",
                )

    def test_stream_standard_usage_details_are_observable_and_strictly_rejected(
        self,
    ) -> None:
        cases = (
            (
                "openai_chat",
                "message",
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                        "prompt_tokens_details": {
                            "cached_tokens": 3,
                            "audio_tokens": 2,
                        },
                        "completion_tokens_details": {"reasoning_tokens": 2},
                    },
                },
                3,
            ),
            (
                "openai_responses",
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 4,
                            "total_tokens": 14,
                            "input_tokens_details": {"cached_tokens": 2},
                            "output_tokens_details": {"reasoning_tokens": 3},
                        },
                    },
                },
                2,
            ),
        )
        for api_format, event, payload, cached_tokens in cases:
            with self.subTest(api_format=api_format, mode="visible_lossy"):
                bridge = protocol.AnthropicStreamBridge(api_format)
                bridge.feed(event, json.dumps(payload))
                self.assertEqual(bridge.cache_read, cached_tokens)
                self.assertIn(
                    "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
                    bridge.warning_codes,
                )

            with self.subTest(api_format=api_format, mode="strict"):
                strict = protocol.AnthropicStreamBridge(
                    api_format,
                    compatibility_mode="strict",
                )
                with self.assertRaises(protocol.ProtocolTransformError) as raised:
                    strict.feed(event, json.dumps(payload))
                self.assertEqual(
                    raised.exception.code,
                    "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED",
                )

    def test_stream_cache_creation_total_conflict_is_rejected_at_finish(self) -> None:
        bridge = protocol.AnthropicStreamBridge("openai_chat")
        bridge.feed(
            "message",
            json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                        "cache_creation_input_tokens": 9,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 1,
                            "ephemeral_1h_input_tokens": 2,
                        },
                    },
                }
            ),
        )
        with self.assertRaises(protocol.ProtocolTransformError) as raised:
            bridge.finish()
        self.assertEqual(raised.exception.code, "HUB_UPSTREAM_USAGE_INVALID")

    def test_stream_usage_provenance_tracks_late_and_missing_counters(self) -> None:
        late = protocol.AnthropicStreamBridge("openai_responses")
        late.feed(
            "response.output_text.delta",
            json.dumps(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "answer",
                }
            ),
        )
        late.feed(
            "response.completed",
            json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                    },
                }
            ),
        )
        late.finish()
        self.assertTrue(late.saw_input_usage)
        self.assertTrue(late.saw_output_usage)
        self.assertIn("HUB_DEGRADE_LATE_INPUT_USAGE", late.warning_codes)
        self.assertNotIn(
            "HUB_USAGE_PROVENANCE_UNAVAILABLE",
            late.warning_codes,
        )

        missing = protocol.AnthropicStreamBridge("openai_chat")
        missing.feed(
            "message",
            json.dumps(
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 4},
                }
            ),
        )
        missing.finish()
        self.assertFalse(missing.saw_input_usage)
        self.assertFalse(missing.saw_output_usage)
        self.assertIn(
            "HUB_USAGE_PROVENANCE_UNAVAILABLE",
            missing.warning_codes,
        )
        self.assertNotIn(
            "HUB_DEGRADE_LATE_INPUT_USAGE",
            missing.warning_codes,
        )


if __name__ == "__main__":
    unittest.main()
