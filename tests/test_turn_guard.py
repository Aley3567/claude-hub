from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import stat


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "claude1-turn-guard.py"


class TurnGuardTests(unittest.TestCase):
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
                "last_assistant_message": "",
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

    def test_explicit_connection_failure_is_recorded_as_drop(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            state_dir = home / "guard-state"
            env = {
                **os.environ,
                "HOME": str(home),
                "CLAUDE1_TURN_GUARD_STATE_DIR": str(state_dir),
            }

            result = subprocess.run(
                [sys.executable, str(GUARD), "failure"],
                input=json.dumps(
                    {
                        "error": "connection_failed",
                        "last_assistant_message": "fixture private message",
                    }
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            log = (state_dir / "watch.log").read_text(encoding="utf-8")
            self.assertIn("LIVE_DROP explicit API failure", log)
            self.assertNotIn("fixture private message", log)

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
                input=json.dumps({"error": "connection_failed"}),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(log_path.stat().st_size, 256 * 1024)
            self.assertIn(
                "LIVE_DROP explicit API failure",
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
