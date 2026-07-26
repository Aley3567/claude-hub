from __future__ import annotations

import pathlib
import sys
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.claude_models import ClaudeModelAdapter  # noqa: E402
from claude_hub.domain import ModelMapping  # noqa: E402


class ClaudeModelAdapterTests(unittest.TestCase):
    def test_project_and_patch_round_trip_preserves_unknown_fields(self) -> None:
        document = {
            "env": {
                "ANTHROPIC_MODEL": "fixture-default",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fixture-fast",
                "ANTHROPIC_REASONING_MODEL": "fixture-reasoning",
                "ANTHROPIC_AUTH_TOKEN": "fixture-private-value",
                "FIXTURE_EXTENSION": {"enabled": True},
            },
            "api_format": "fixture-format",
            "fixture_metadata": {"nested": ["kept", 7]},
        }
        adapter = ClaudeModelAdapter()

        projected = adapter.project(document)
        patched = adapter.patch(document, projected)

        self.assertEqual(
            projected.to_public_dict(),
            {
                "default": "fixture-default",
                "fast": "fixture-fast",
                "reasoning": "fixture-reasoning",
            },
        )
        self.assertEqual(patched, document)
        self.assertIsNot(patched, document)
        self.assertIsNot(patched["env"], document["env"])

    def test_missing_optional_roles_remain_missing_on_round_trip(self) -> None:
        document = {"fixture_metadata": {"preserved": True}}
        adapter = ClaudeModelAdapter()

        projected = adapter.project(document)

        self.assertEqual(projected.configured_slots, ())
        self.assertIsNone(projected.fast)
        self.assertIsNone(projected.reasoning)
        self.assertEqual(adapter.patch(document, projected), document)

    def test_unknown_fields_are_summarized_without_returning_values(self) -> None:
        document = {
            "env": {
                "ANTHROPIC_MODEL": "fixture-default",
                "ANTHROPIC_AUTH_TOKEN": "fixture-private-value",
                "FIXTURE_EXTENSION": {"enabled": True},
            },
            "api_format": "fixture-format",
            "fixture_metadata": {"nested": ["kept", 7]},
        }

        summary = ClaudeModelAdapter().summarize_unknown(document)

        self.assertEqual(summary.count, 4)
        self.assertEqual(
            summary.fingerprint,
            "ba7d88d491f982418e2f4d704c0360fdb193968ff2295261f2a7015130ab7353",
        )
        self.assertNotIn("fixture-private-value", repr(summary))
        self.assertNotIn("fixture-format", repr(summary))

    def test_patch_maps_every_generic_slot_without_touching_other_values(self) -> None:
        document = {
            "env": {"FIXTURE_EXTENSION": "unchanged"},
            "fixture_metadata": ["unchanged"],
        }

        patched = ClaudeModelAdapter().patch(
            document,
            ModelMapping(
                default="fixture-default",
                fast="fixture-fast",
                reasoning="fixture-reasoning",
                coding="fixture-coding",
                long_context="fixture-long",
                fallback="fixture-fallback",
            ),
        )

        self.assertEqual(
            patched,
            {
                "env": {
                    "FIXTURE_EXTENSION": "unchanged",
                    "ANTHROPIC_MODEL": "fixture-default",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fixture-fast",
                    "ANTHROPIC_REASONING_MODEL": "fixture-reasoning",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "fixture-coding",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": "fixture-long",
                    "ANTHROPIC_DEFAULT_FABLE_MODEL": "fixture-fallback",
                },
                "fixture_metadata": ["unchanged"],
            },
        )
        self.assertEqual(
            document,
            {
                "env": {"FIXTURE_EXTENSION": "unchanged"},
                "fixture_metadata": ["unchanged"],
            },
        )

    def test_legacy_small_fast_field_projects_without_changing_round_trip(
        self,
    ) -> None:
        document = {
            "env": {
                "ANTHROPIC_SMALL_FAST_MODEL": "fixture-fast",
                "FIXTURE_EXTENSION": "unchanged",
            }
        }
        adapter = ClaudeModelAdapter()

        projected = adapter.project(document)

        self.assertEqual(projected.fast, "fixture-fast")
        self.assertEqual(adapter.patch(document, projected), document)


if __name__ == "__main__":
    unittest.main()
