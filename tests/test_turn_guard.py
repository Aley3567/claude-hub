from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "claude1-turn-guard.py"


class TurnGuardTests(unittest.TestCase):
    def _run_guard(
        self,
        home: Path,
        event: str,
        hook_input: dict,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        state_dir = home / "guard-state"
        env = {
            **os.environ,
            "HOME": str(home),
            "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
        }
        result = subprocess.run(
            [sys.executable, str(GUARD), event],
            input=json.dumps(hook_input),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        return result, state_dir

    def test_thinking_only_end_turn_requests_one_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            transcript = home / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "id": "msg_fixture_empty",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "thinking",
                                    "thinking": "fixture reasoning",
                                }
                            ],
                            "stop_reason": "end_turn",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            hook_input = {
                "transcript_path": str(transcript),
                "stop_hook_active": False,
                "last_assistant_message": " \t",
            }
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(home / "guard-state"),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "stop"],
                input=json.dumps(hook_input),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "decision": "block",
                    "reason": (
                        "上一轮响应只产生了 thinking，未返回正文或工具调用。"
                        "请从中断处继续完成原任务；已执行过的工具调用不要重复执行。"
                    ),
                },
            )

    def test_repeated_thinking_only_end_turn_trips_the_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            transcript = home / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "id": "msg_fixture_repeated",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "thinking",
                                    "thinking": "fixture reasoning",
                                }
                            ],
                            "stop_reason": "end_turn",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            hook_input = {
                "transcript_path": str(transcript),
                "stop_hook_active": True,
                "last_assistant_message": "",
            }
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "stop"],
                input=json.dumps(hook_input),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            # 熔断时必须放行（不再 block），但要向用户显示降级状态与恢复建议。
            self.assertNotIn("decision", payload)
            self.assertIn("systemMessage", payload)
            self.assertIn("已停止自动续跑", payload["systemMessage"])
            self.assertIn("/resume", payload["systemMessage"])
            # 提示不得携带 transcript 内容。
            self.assertNotIn("fixture", result.stdout)
            self.assertIn(
                "LIVE_BROKEN repeated thinking-only end_turn",
                (state_dir / "watch.log").read_text(encoding="utf-8"),
            )

    def test_text_response_is_released_and_recorded_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            transcript = home / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "id": "msg_fixture_text",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "thinking",
                                            "thinking": "fixture reasoning",
                                        }
                                    ],
                                    "stop_reason": None,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "id": "msg_fixture_text",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "fixture final answer",
                                        }
                                    ],
                                    "stop_reason": "end_turn",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            hook_input = {
                "transcript_path": str(transcript),
                "stop_hook_active": False,
                "last_assistant_message": "fixture final answer",
            }
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "stop"],
                input=json.dumps(hook_input),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn(
                "LIVE_OK usable assistant response",
                (state_dir / "watch.log").read_text(encoding="utf-8"),
            )

    def test_last_assistant_message_wins_over_stale_thinking_transcript(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            transcript = home / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "id": "msg_fixture_stale",
                            "content": [
                                {
                                    "type": "thinking",
                                    "thinking": "stale fixture reasoning",
                                }
                            ],
                            "stop_reason": "end_turn",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            result, state_dir = self._run_guard(
                home,
                "stop",
                {
                    "transcript_path": str(transcript),
                    "stop_hook_active": False,
                    "last_assistant_message": "new fixture final answer",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            log = (state_dir / "watch.log").read_text(encoding="utf-8")
            self.assertIn("LIVE_OK usable assistant response", log)
            self.assertNotIn("stale fixture reasoning", log)
            self.assertNotIn("new fixture final answer", log)

    def test_tool_use_end_turn_is_released_as_usable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            transcript = home / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "id": "msg_fixture_tool_use",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "thinking",
                                    "thinking": "fixture reasoning",
                                },
                                {
                                    "type": "tool_use",
                                    "id": "toolu_fixture",
                                    "name": "fixture_tool",
                                    "input": {},
                                },
                            ],
                            "stop_reason": "end_turn",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            hook_input = {
                "transcript_path": str(transcript),
                "stop_hook_active": False,
                "last_assistant_message": "",
            }
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "stop"],
                input=json.dumps(hook_input),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            # 含 tool_use 的 end_turn 是可用响应：不得 block、不得触发熔断。
            self.assertEqual(result.stdout, "")
            self.assertIn(
                "LIVE_OK usable assistant response",
                (state_dir / "watch.log").read_text(encoding="utf-8"),
            )

    def test_malformed_transcript_is_released_without_logging_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            transcript = home / "session.jsonl"
            private_marker = "fixture-private-transcript-content"
            transcript.write_text(
                f'{{"broken":"{private_marker}"\n',
                encoding="utf-8",
            )
            hook_input = {
                "transcript_path": str(transcript),
                "stop_hook_active": False,
                "last_assistant_message": "",
            }
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "stop"],
                input=json.dumps(hook_input),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            log = (state_dir / "watch.log").read_text(encoding="utf-8")
            self.assertIn("LIVE_UNK transcript unavailable", log)
            self.assertNotIn(private_marker, log)

    def test_corrupt_or_missing_tail_never_reuses_older_thinking_only(
        self,
    ) -> None:
        stale_event = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_fixture_stale_tail",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "old fixture reasoning",
                        }
                    ],
                    "stop_reason": "end_turn",
                },
            }
        )
        tails = (
            '{"type":"assistant","message":',
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": "new fixture request"},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg_fixture_incomplete",
                        "content": [{"type": "thinking", "thinking": "new"}],
                        "stop_reason": None,
                    },
                }
            ),
        )

        for index, tail in enumerate(tails):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as raw_home:
                home = Path(raw_home)
                transcript = home / "session.jsonl"
                transcript.write_text(
                    f"{stale_event}\n{tail}\n",
                    encoding="utf-8",
                )
                result, state_dir = self._run_guard(
                    home,
                    "stop",
                    {
                        "transcript_path": str(transcript),
                        "stop_hook_active": False,
                        "last_assistant_message": "",
                    },
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                log = (state_dir / "watch.log").read_text(encoding="utf-8")
                self.assertIn("LIVE_UNK transcript unavailable", log)
                self.assertNotIn("old fixture reasoning", log)

    def test_missing_transcript_is_released_safely(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            result, state_dir = self._run_guard(
                home,
                "stop",
                {
                    "transcript_path": str(home / "missing.jsonl"),
                    "stop_hook_active": False,
                    "last_assistant_message": "",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn(
                "LIVE_UNK transcript unavailable",
                (state_dir / "watch.log").read_text(encoding="utf-8"),
            )

    def test_bounded_tail_ignores_oversized_invalid_old_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            transcript = home / "session.jsonl"
            final_event = json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg_fixture_bounded_tail",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "fixture bounded reasoning",
                            }
                        ],
                        "stop_reason": "end_turn",
                    },
                }
            ).encode("utf-8")
            transcript.write_bytes(
                b"\xff" * (600 * 1024) + b"\n" + final_event + b"\n"
            )

            result, _state_dir = self._run_guard(
                home,
                "stop",
                {
                    "transcript_path": str(transcript),
                    "stop_hook_active": False,
                    "last_assistant_message": "",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "block")

    def test_oversized_unterminated_final_segment_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            transcript = home / "session.jsonl"
            old_event = json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg_fixture_old_large",
                        "content": [{"type": "thinking", "thinking": "old"}],
                        "stop_reason": "end_turn",
                    },
                }
            ).encode("utf-8")
            transcript.write_bytes(
                old_event
                + b"\n"
                + b'{"type":"assistant","message":{"id":"msg_fixture_large",'
                + b'"content":"'
                + b"x" * (600 * 1024)
            )

            result, state_dir = self._run_guard(
                home,
                "stop",
                {
                    "transcript_path": str(transcript),
                    "stop_hook_active": False,
                    "last_assistant_message": "",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn(
                "LIVE_UNK transcript unavailable",
                (state_dir / "watch.log").read_text(encoding="utf-8"),
            )

    def test_stop_failure_uses_official_observation_only_taxonomy(self) -> None:
        cases = {
            "rate_limit": "LIVE_BUSY rate limited",
            "overloaded": "LIVE_DOWN upstream unavailable",
            "authentication_failed": "LIVE_AUTH authentication failed",
            "oauth_org_not_allowed": "LIVE_AUTH authentication failed",
            "billing_error": "LIVE_BILLING billing unavailable",
            "invalid_request": "LIVE_REJECT request rejected",
            "model_not_found": "LIVE_REJECT model unavailable",
            "server_error": "LIVE_DOWN upstream unavailable",
            "max_output_tokens": "LIVE_LIMIT output limit reached",
            "unknown": "LIVE_UNK unclassified API failure",
        }
        private_marker = "fixture-private-failure-detail"

        for error_type, expected in cases.items():
            with (
                self.subTest(error_type=error_type),
                tempfile.TemporaryDirectory() as raw_home,
            ):
                home = Path(raw_home)
                result, state_dir = self._run_guard(
                    home,
                    "failure",
                    {
                        "error": error_type,
                        "error_details": {"message": private_marker},
                        "last_assistant_message": private_marker,
                    },
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
                log = (state_dir / "watch.log").read_text(encoding="utf-8")
                self.assertIn(expected, log)
                self.assertNotIn(private_marker, log)
                self.assertNotIn("续跑", log)
                self.assertNotIn("decision", log)

    def test_unofficial_failure_value_is_only_observed_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            result, state_dir = self._run_guard(
                home,
                "failure",
                {
                    "error": "connection_failed",
                    "error_details": "fixture private message",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            log = (state_dir / "watch.log").read_text(encoding="utf-8")
            self.assertIn("LIVE_UNK unclassified API failure", log)
            self.assertNotIn("fixture private message", log)

    def test_user_interrupt_has_no_guard_event_or_continuation_claim(self) -> None:
        # Claude does not invoke Stop for a user interrupt.  An unsupported
        # event must therefore be a complete no-op rather than inferred from
        # transcript content.
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            result, state_dir = self._run_guard(
                home,
                "user-interrupt",
                {
                    "transcript_path": str(home / "session.jsonl"),
                    "last_assistant_message": "fixture private message",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertFalse(state_dir.exists())

    def test_large_log_is_rotated_and_kept_private(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            state_dir.mkdir()
            log_path = state_dir / "watch.log"
            log_path.write_text("x" * (300 * 1024), encoding="utf-8")
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "failure"],
                input=json.dumps({"error": "server_error"}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(log_path.stat().st_size, 256 * 1024)
            self.assertIn(
                "LIVE_DOWN upstream unavailable",
                log_path.read_text(encoding="utf-8"),
            )
            rotated = state_dir / "watch.log.1"
            self.assertTrue(rotated.is_file())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(rotated.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX special-file safety")
    def test_fifo_transcript_is_released_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            transcript = home / "session.jsonl"
            os.mkfifo(transcript)
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "stop"],
                input=json.dumps(
                    {
                        "transcript_path": str(transcript),
                        "stop_hook_active": False,
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
                timeout=1,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn(
                "LIVE_UNK transcript unavailable",
                (state_dir / "watch.log").read_text(encoding="utf-8"),
            )

    def test_invalid_utf8_transcript_is_released_safely(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            transcript = home / "session.jsonl"
            transcript.write_bytes(b"\xff\xfe\x00broken")
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "stop"],
                input=json.dumps({"transcript_path": str(transcript)}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn(
                "LIVE_UNK transcript unavailable",
                (state_dir / "watch.log").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
