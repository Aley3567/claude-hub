from __future__ import annotations

import copy
import unittest
from pathlib import Path

import claude_hub_catalog as catalog


class HubCatalogTests(unittest.TestCase):
    def test_normalize_catalog_preserves_order_and_detaches_entries(self) -> None:
        raw = {
            "version": 1,
            "default_hub": "work",
            "order": ["work", "research"],
            "hubs": {
                "work": {
                    "name": "工作 Hub",
                    "state": "ready",
                    "config": "hubs/work.json",
                    "log": "logs/work.log",
                    "usage": "logs/work-usage.jsonl",
                },
                "research": {
                    "name": "Research-Hub",
                    "state": "ready",
                    "config": "hubs/research.json",
                    "log": "logs/research.log",
                    "usage": "logs/research-usage.jsonl",
                },
            },
        }

        normalized = catalog.normalize_hub_catalog(raw)

        self.assertEqual(normalized, raw)
        self.assertEqual(list(normalized["hubs"]), ["work", "research"])
        self.assertIsNot(normalized, raw)
        self.assertIsNot(normalized["hubs"]["work"], raw["hubs"]["work"])

    def test_load_catalog_accepts_an_already_parsed_input_dict(self) -> None:
        raw = {
            "version": 1,
            "default_hub": "claude-hub",
            "order": ["claude-hub"],
            "hubs": {"claude-hub": catalog.legacy_hub_entry()},
        }

        loaded = catalog.load_hub_catalog(raw)

        self.assertEqual(loaded, raw)
        self.assertIsNot(loaded, raw)

    def test_display_names_allow_human_labels_but_reject_unsafe_text(self) -> None:
        self.assertEqual(catalog.validate_display_name("  研究 Hub-一号  "), "研究 Hub-一号")

        for invalid in ("", "\u0301", "bad\nname", "bad\tname", "x" * 49):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    catalog.validate_display_name(invalid)

    def test_resolve_hub_accepts_default_id_or_unique_display_name(self) -> None:
        normalized = catalog.normalize_hub_catalog(
            {
                "version": 1,
                "default_hub": "work",
                "order": ["work", "research"],
                "hubs": {
                    "work": {
                        "name": "工作 Hub",
                        "config": "work.json",
                        "log": "logs/work.log",
                        "usage": "logs/work.jsonl",
                    },
                    "research": {
                        "name": "Research Hub",
                        "config": "research.json",
                        "log": "logs/research.log",
                        "usage": "logs/research.jsonl",
                    },
                },
            }
        )

        self.assertEqual(catalog.resolve_hub_id(normalized), "work")
        self.assertEqual(catalog.resolve_hub_id(normalized, "research"), "research")
        self.assertEqual(catalog.resolve_hub_id(normalized, "RESEARCH HUB"), "research")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            duplicate = {
                **normalized,
                "hubs": {
                    **normalized["hubs"],
                    "research": {
                        **normalized["hubs"]["research"],
                        "name": "工作 Hub",
                    },
                },
            }
            catalog.resolve_hub_id(duplicate, "工作 hub")
        with self.assertRaisesRegex(ValueError, "not found"):
            catalog.resolve_hub_id(normalized, "missing")

    def test_unique_hub_id_slugifies_names_and_uses_numeric_suffixes(self) -> None:
        self.assertEqual(catalog.unique_hub_id("Research Hub", []), "research-hub")
        self.assertEqual(
            catalog.unique_hub_id("Research Hub", ["research-hub", "research-hub-2"]),
            "research-hub-3",
        )
        self.assertEqual(catalog.unique_hub_id("中文研究", []), "hub")
        self.assertEqual(catalog.unique_hub_id("123 Lab", []), "hub-123-lab")

    def test_legacy_entry_points_at_the_existing_single_hub_files(self) -> None:
        self.assertEqual(catalog.LEGACY_HUB_ID, "claude-hub")
        self.assertEqual(
            catalog.legacy_hub_entry(),
            {
                "name": "Claude-Hub",
                "state": "ready",
                "config": "claude-hub.json",
                "log": "logs/claude-hub.log",
                "usage": "logs/claude-hub-usage.jsonl",
            },
        )

    def test_catalog_normalizes_lifecycle_state_and_setup_draft(self) -> None:
        raw = {
            "version": 1,
            "default_hub": "work",
            "order": ["work", "research"],
            "hubs": {
                "work": {
                    "name": "Work Hub",
                    "config": "hubs/work.json",
                    "log": "logs/work.log",
                    "usage": "logs/work.jsonl",
                },
                "research": {
                    "name": "Research Hub",
                    "state": "setup",
                    "config": "hubs/research.json",
                    "log": "logs/research.log",
                    "usage": "logs/research.jsonl",
                    "draft": "drafts/research.json",
                },
            },
        }

        normalized = catalog.normalize_hub_catalog(raw)

        self.assertEqual(normalized["hubs"]["work"]["state"], "ready")
        self.assertNotIn("draft", normalized["hubs"]["work"])
        self.assertEqual(normalized["hubs"]["research"]["state"], "setup")
        self.assertEqual(
            normalized["hubs"]["research"]["draft"], "drafts/research.json"
        )

    def test_catalog_rejects_invalid_lifecycle_entries(self) -> None:
        base = {
            "version": 1,
            "default_hub": "work",
            "order": ["work"],
            "hubs": {
                "work": {
                    "name": "Work Hub",
                    "config": "hubs/work.json",
                    "log": "logs/work.log",
                    "usage": "logs/work.jsonl",
                }
            },
        }
        invalid_entries = (
            {"state": "paused"},
            {"state": "setup"},
            {"state": "setup", "draft": "../draft.json"},
            {"state": "setup", "draft": "/tmp/draft.json"},
        )

        for changes in invalid_entries:
            candidate = copy.deepcopy(base)
            candidate["hubs"]["work"].update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    catalog.normalize_hub_catalog(candidate)

    def test_catalog_rejects_paths_reused_across_hubs(self) -> None:
        base = {
            "version": 1,
            "default_hub": "work",
            "order": ["work", "research"],
            "hubs": {
                "work": {
                    "name": "Work Hub",
                    "config": "hubs/work.json",
                    "log": "logs/work.log",
                    "usage": "logs/work.jsonl",
                    "draft": "drafts/work.json",
                },
                "research": {
                    "name": "Research Hub",
                    "state": "setup",
                    "config": "hubs/research.json",
                    "log": "logs/research.log",
                    "usage": "logs/research.jsonl",
                    "draft": "drafts/research.json",
                },
            },
        }

        for field in ("config", "log", "usage", "draft"):
            candidate = copy.deepcopy(base)
            candidate["hubs"]["research"][field] = candidate["hubs"]["work"][field]
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "path.*unique"):
                    catalog.normalize_hub_catalog(candidate)

    def test_resolve_catalog_path_accepts_only_safe_relative_paths(self) -> None:
        catalog_path = Path("/private/state/claude-hubs.json")
        self.assertEqual(
            catalog.resolve_catalog_path(catalog_path, "hubs/work.json"),
            Path("/private/state/hubs/work.json"),
        )

        for unsafe in ("/tmp/work.json", "../work.json", "hubs/../../work.json", "C:\\tmp\\work.json"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    catalog.resolve_catalog_path(catalog_path, unsafe)

    def test_validate_unique_hub_ports_reports_conflicting_hubs(self) -> None:
        configs = {
            "work": {"port": 18787},
            "research": {"port": "18788"},
        }
        self.assertEqual(
            catalog.validate_unique_hub_ports(configs),
            {"work": 18787, "research": 18788},
        )

        with self.assertRaisesRegex(ValueError, "work.*research.*18787"):
            catalog.validate_unique_hub_ports(
                {"work": {"port": 18787}, "research": {"port": 18787}}
            )
        for invalid in (True, 0, 65536, "not-a-port"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    catalog.validate_unique_hub_ports({"work": {"port": invalid}})

    def test_catalog_rejects_invalid_identity_order_and_entry_paths(self) -> None:
        valid = {
            "version": 1,
            "default_hub": "work",
            "order": ["work"],
            "hubs": {
                "work": {
                    "name": "Work Hub",
                    "config": "hubs/work.json",
                    "log": "logs/work.log",
                    "usage": "logs/work.jsonl",
                }
            },
        }
        invalid_catalogs = []
        for path, value in (
            (("version",), True),
            (("default_hub",), "missing"),
            (("order",), []),
            (("hubs", "work", "config"), "../work.json"),
        ):
            candidate = copy.deepcopy(valid)
            target = candidate
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid_catalogs.append(candidate)
        invalid_catalogs.append(
            {
                **valid,
                "default_hub": "Work Hub",
                "order": ["Work Hub"],
                "hubs": {"Work Hub": valid["hubs"]["work"]},
            }
        )

        for candidate in invalid_catalogs:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    catalog.normalize_hub_catalog(candidate)

    def test_catalog_rejects_paths_shared_by_fields_or_hubs(self) -> None:
        base = {
            "version": 1,
            "default_hub": "work",
            "order": ["work", "research"],
            "hubs": {
                "work": {
                    "name": "Work",
                    "config": "hubs/work.json",
                    "log": "logs/work.log",
                    "usage": "logs/work.jsonl",
                },
                "research": {
                    "name": "Research",
                    "config": "hubs/research.json",
                    "log": "logs/research.log",
                    "usage": "logs/research.jsonl",
                },
            },
        }
        same_entry = copy.deepcopy(base)
        same_entry["hubs"]["work"]["log"] = "hubs/work.json"
        cross_hub = copy.deepcopy(base)
        cross_hub["hubs"]["research"]["usage"] = "logs/work.log"

        for candidate in (same_entry, cross_hub):
            with self.assertRaisesRegex(ValueError, "path is shared"):
                catalog.normalize_hub_catalog(candidate)

    def test_catalog_rejects_casefold_duplicate_display_names(self) -> None:
        raw = {
            "version": 1,
            "default_hub": "work",
            "order": ["work", "research"],
            "hubs": {
                "work": {
                    "name": "Research Hub",
                    "config": "work.json",
                    "log": "logs/work.log",
                    "usage": "logs/work.jsonl",
                },
                "research": {
                    "name": "RESEARCH HUB",
                    "config": "research.json",
                    "log": "logs/research.log",
                    "usage": "logs/research.jsonl",
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "display name.*unique"):
            catalog.normalize_hub_catalog(raw)


if __name__ == "__main__":
    unittest.main()
