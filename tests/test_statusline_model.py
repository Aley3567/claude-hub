from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "statusline-model.py"
SPEC = importlib.util.spec_from_file_location("statusline_model", MODULE_PATH)
statusline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(statusline)


def iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


class StatuslineModelTests(unittest.TestCase):
    def test_turn_metadata_does_not_invalidate_latest_assistant_model(self) -> None:
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as raw:
            transcript = Path(raw) / "session.jsonl"
            rows = [
                {
                    "type": "assistant",
                    "timestamp": iso(now - 2),
                    "message": {"model": "upstream-flash"},
                },
                {
                    "type": "user",
                    "sourceToolAssistantUUID": "assistant-tool-call",
                    "toolUseResult": {"ok": True},
                    "timestamp": iso(now - 1),
                    "message": {"content": [{"type": "tool_result"}]},
                },
                {
                    "type": "attachment",
                    "timestamp": iso(now - 0.5),
                },
                {
                    "type": "system",
                    "subtype": "turn_duration",
                    "timestamp": iso(now - 0.25),
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )
            payload = {
                "model": {"id": "startup-pro", "display_name": "Pro"},
                "transcript_path": str(transcript),
            }

            self.assertEqual(
                statusline.resolve_model(payload, {}, now=now),
                "upstream-flash",
            )

    def test_real_unanswered_user_turn_invalidates_old_assistant(self) -> None:
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as raw:
            transcript = Path(raw) / "session.jsonl"
            rows = [
                {
                    "type": "assistant",
                    "timestamp": iso(now - 2),
                    "message": {"model": "old-model"},
                },
                {
                    "type": "user",
                    "timestamp": iso(now - 1),
                    "message": {"content": "new real prompt"},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(row) for row in rows),
                encoding="utf-8",
            )
            payload = {
                "model": {"id": "selected-model", "display_name": "Selected"},
                "transcript_path": str(transcript),
            }

            self.assertEqual(
                statusline.resolve_model(payload, {}, now=now),
                "selected-model",
            )

    def test_third_party_id_maps_by_exact_slot_value_not_tier_keyword(self) -> None:
        payload = {
            "model": {
                "id": "third-party-flash",
                "display_name": "Logical Opus",
            }
        }
        env = {
            "ANTHROPIC_MODEL": "startup-pro",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "third-party-flash",
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "Flash Friendly Name",
        }

        self.assertEqual(
            statusline.resolve_model(payload, env),
            "Flash Friendly Name",
        )

    def test_missing_stdin_id_falls_back_to_process_model(self) -> None:
        payload = {"model": {"display_name": "Logical tier"}}
        self.assertEqual(
            statusline.resolve_model(
                payload,
                {"ANTHROPIC_MODEL": "configured-startup-model"},
            ),
            "configured-startup-model",
        )

    def test_db_current_mapping_requires_exactly_one_current_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = Path(raw) / "cc-switch.db"
            connection = sqlite3.connect(db)
            connection.execute(
                "CREATE TABLE providers ("
                "id TEXT, app_type TEXT, settings_config TEXT, is_current INTEGER)"
            )
            connection.execute(
                "INSERT INTO providers VALUES (?, 'claude', ?, 1)",
                (
                    "current",
                    json.dumps(
                        {
                            "env": {
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "slot-id",
                                "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "DB Model",
                            }
                        }
                    ),
                ),
            )
            connection.commit()
            connection.close()
            payload = {"model": {"id": "slot-id", "display_name": "Haiku"}}
            env = {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:19091",
                "CLAUDE1_DB_PATH": str(db),
            }

            self.assertEqual(statusline.resolve_model(payload, env), "DB Model")


if __name__ == "__main__":
    unittest.main()
