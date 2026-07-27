from __future__ import annotations

import dataclasses
import pathlib
import socket
import sqlite3
import sys
import unittest
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.domain import (  # noqa: E402
    ModelMapping,
    ProviderInspection,
    ProviderRef,
    RuntimeMode,
    StoreCapability,
)
from claude_hub.service import ProviderApplicationService  # noqa: E402
from claude_hub.store import ProviderNotFoundError, ProviderStore  # noqa: E402
from claude_hub.testing import InMemoryProviderStore  # noqa: E402


class CoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = ProviderRef(
            store="memory",
            provider_id="provider-01",
            is_current=True,
        )
        self.models = ModelMapping(
            default="model-default",
            fast="model-fast",
            reasoning="model-reasoning",
        )
        self.inspection = ProviderInspection(
            reference=self.reference,
            models=self.models,
            is_current=True,
        )
        self.store = InMemoryProviderStore(
            capability=StoreCapability.COMPATIBLE,
            providers=(self.reference,),
            inspections=(self.inspection,),
        )
        self.service = ProviderApplicationService(self.store)

    def test_shared_values_are_immutable(self) -> None:
        for value, field_name, replacement in (
            (self.reference, "provider_id", "another-provider"),
            (self.models, "default", "another-model"),
            (self.inspection, "is_current", False),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(value, field_name, replacement)

        self.assertEqual(RuntimeMode.COMPANION.value, "companion")
        self.assertEqual(StoreCapability.READ_ONLY.value, "read_only")

    def test_fake_store_satisfies_protocol_and_service_contract(self) -> None:
        self.assertIsInstance(self.store, ProviderStore)
        self.assertIs(
            self.service.detect(),
            StoreCapability.COMPATIBLE,
        )
        self.assertEqual(self.service.list(), (self.reference,))
        self.assertEqual(
            self.service.inspect(self.reference),
            self.inspection,
        )
        self.assertEqual(
            self.service.list_providers(),
            (self.reference,),
        )
        self.assertEqual(
            self.service.inspect_provider(self.reference),
            self.inspection,
        )

    def test_fake_contract_is_isolated_from_home_network_and_sqlite(self) -> None:
        with (
            mock.patch.object(
                pathlib.Path,
                "home",
                side_effect=AssertionError("HOME access is forbidden"),
            ),
            mock.patch.object(
                socket,
                "create_connection",
                side_effect=AssertionError("network access is forbidden"),
            ),
            mock.patch.object(
                sqlite3,
                "connect",
                side_effect=AssertionError("database access is forbidden"),
            ),
        ):
            self.assertIs(
                self.service.detect(),
                StoreCapability.COMPATIBLE,
            )
            self.assertEqual(self.service.list(), (self.reference,))
            self.assertEqual(
                self.service.inspect(self.reference),
                self.inspection,
            )

    def test_missing_provider_uses_stable_store_error(self) -> None:
        missing = ProviderRef(store="memory", provider_id="missing-provider")
        with self.assertRaisesRegex(
            ProviderNotFoundError,
            "^provider reference was not found$",
        ):
            self.service.inspect(missing)

    def test_public_dtos_have_no_secret_bearing_fields(self) -> None:
        forbidden_fragments = (
            "key",
            "token",
            "credential",
            "password",
            "url",
            "header",
            "config",
            "path",
        )
        for dto_type in (ProviderRef, ModelMapping, ProviderInspection):
            with self.subTest(dto=dto_type.__name__):
                field_names = {
                    field.name.casefold()
                    for field in dataclasses.fields(dto_type)
                }
                for field_name in field_names:
                    self.assertFalse(
                        any(fragment in field_name for fragment in forbidden_fragments)
                    )

    def test_secret_like_values_cannot_enter_public_dtos_or_repr(self) -> None:
        sensitive = "fixture-" + "secret" + "-material"

        with self.assertRaisesRegex(ValueError, "not a public identifier"):
            ProviderRef(store="memory", provider_id=sensitive)
        with self.assertRaisesRegex(ValueError, "not a public identifier"):
            ModelMapping(default=sensitive)

        representation = repr(self.inspection)
        self.assertNotIn("model-default", representation)
        self.assertNotIn("provider-01", representation)
        self.assertIn("<redacted>", representation)

    def test_absolute_paths_cannot_enter_public_identifiers(self) -> None:
        for private_path in (
            "/private/fixture/provider",
            r"C:\private\fixture\provider",
            r"\\fixture-host\private\provider",
        ):
            with self.subTest(style=private_path[:2]):
                with self.assertRaisesRegex(
                    ValueError,
                    "not a public identifier",
                ):
                    ProviderRef(
                        store="memory",
                        provider_id=private_path,
                    )
                with self.assertRaisesRegex(
                    ValueError,
                    "not a public identifier",
                ):
                    ModelMapping(default=private_path)

    def test_inspection_rejects_conflicting_current_markers(self) -> None:
        reference = ProviderRef(
            store="memory",
            provider_id="fixture-provider",
            is_current=False,
        )

        with self.assertRaisesRegex(
            ValueError,
            "^reference and inspection current markers must match$",
        ):
            ProviderInspection(
                reference=reference,
                is_current=True,
            )

    def test_capability_properties_do_not_grant_unknown_schema_write(self) -> None:
        for capability in (
            StoreCapability.ABSENT,
            StoreCapability.READ_ONLY,
            StoreCapability.INCOMPATIBLE,
            StoreCapability.CORRUPT,
        ):
            with self.subTest(capability=capability):
                self.assertFalse(capability.schema_allows_write)
        self.assertTrue(StoreCapability.COMPATIBLE.schema_allows_write)


if __name__ == "__main__":
    unittest.main()
