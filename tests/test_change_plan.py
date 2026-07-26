from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import pathlib
import socket
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from uuid import UUID


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub import change_plan  # noqa: E402
from claude_hub.change_plan import (  # noqa: E402
    COMPANION_STORE_ID,
    PLAN_SCHEMA_VERSION,
    STANDALONE_STORE_ID,
    ChangePlan,
    EmptyChangePlanError,
    FieldChange,
    InvalidChangePlanError,
    PlanTarget,
    build_change_plan,
    canonical_change_plan_json,
    change_plan_digest,
    json_preview,
    tui_preview,
)
from claude_hub.domain import (  # noqa: E402
    ModelMapping,
    ProtocolAdapter,
    ProviderRef,
    RuntimeMode,
    StandaloneProfile,
)
from claude_hub.standalone import StandaloneProfileStore  # noqa: E402


FINGERPRINT = "a1" * 32
OTHER_FINGERPRINT = "b2" * 32
CREATED_AT = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)
STANDALONE_PROFILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def companion_plan(
    *,
    changes: object | None = None,
    target: object | None = None,
    fingerprint: object = FINGERPRINT,
    mode: object = RuntimeMode.COMPANION,
    schema_version: object = PLAN_SCHEMA_VERSION,
) -> ChangePlan:
    selected_changes = (
        {
            "models.default": (
                "model-default-old",
                "model-default-new",
            )
        }
        if changes is None
        else changes
    )
    selected_target = (
        ProviderRef(
            store=COMPANION_STORE_ID,
            provider_id="provider-public-id",
        )
        if target is None
        else target
    )
    return ChangePlan(
        mode=mode,  # type: ignore[arg-type]
        target=selected_target,  # type: ignore[arg-type]
        store_fingerprint=fingerprint,  # type: ignore[arg-type]
        changes=selected_changes,  # type: ignore[arg-type]
        schema_version=schema_version,  # type: ignore[arg-type]
    )


class ChangePlanContractTests(unittest.TestCase):
    def test_plan_is_immutable_and_discards_private_target_metadata(self) -> None:
        private_name = "Private local provider label"
        reference = ProviderRef(
            store=COMPANION_STORE_ID,
            provider_id="provider-public-id",
            is_current=True,
            display_name=private_name,
        )

        plan = companion_plan(target=reference)

        self.assertEqual(
            plan.target,
            PlanTarget(
                store=COMPANION_STORE_ID,
                provider_id="provider-public-id",
            ),
        )
        self.assertFalse(hasattr(plan.target, "display_name"))
        self.assertFalse(hasattr(plan.target, "is_current"))
        self.assertNotIn(private_name, repr(plan))
        self.assertNotIn(private_name, repr(plan.target))
        self.assertNotIn(private_name, canonical_change_plan_json(plan))
        self.assertNotIn(private_name, tui_preview(plan))

        for value, field_name, replacement in (
            (plan, "mode", RuntimeMode.STANDALONE),
            (plan.target, "store", STANDALONE_STORE_ID),
            (plan.changes[0], "new", "replacement"),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(value, field_name, replacement)

        for forbidden_operation in ("approve", "approved", "apply"):
            self.assertFalse(hasattr(plan, forbidden_operation))

    def test_constructor_normalizes_mapping_and_tag_order(self) -> None:
        target_a = ProviderRef(
            store=STANDALONE_STORE_ID,
            provider_id=STANDALONE_PROFILE_ID,
            is_current=False,
            display_name="First private label",
        )
        target_b = ProviderRef(
            store=STANDALONE_STORE_ID,
            provider_id=STANDALONE_PROFILE_ID,
            is_current=True,
            display_name="Second private label",
        )
        changes_a = {
            "purpose_tags": (
                ("coding", "primary", "coding"),
                ("review", "primary"),
            ),
            "models.fast": ("fast-old", "fast-new"),
            "models.default": ("default-old", "default-new"),
        }
        changes_b = {
            "models.default": ("default-old", "default-new"),
            "models.fast": ("fast-old", "fast-new"),
            "purposeTags": (
                ("primary", "coding"),
                ("primary", "review", "review"),
            ),
        }

        plan_a = build_change_plan(
            mode=RuntimeMode.STANDALONE,
            target=target_a,
            store_fingerprint=OTHER_FINGERPRINT,
            changes=changes_a,
        )
        plan_b = build_change_plan(
            mode="standalone",
            target=target_b,
            store_fingerprint=OTHER_FINGERPRINT,
            changes=changes_b,
        )
        plan_c = build_change_plan(
            mode=RuntimeMode.STANDALONE,
            target=target_a,
            store_fingerprint=OTHER_FINGERPRINT,
            changes=(
                FieldChange(
                    "purpose_tags",
                    ("primary", "coding"),
                    ("review", "primary"),
                ),
                FieldChange(
                    "models.fast",
                    "fast-old",
                    "fast-new",
                ),
                FieldChange(
                    "models.default",
                    "default-old",
                    "default-new",
                ),
            ),
        )

        self.assertEqual(plan_a, plan_b)
        self.assertEqual(plan_a, plan_c)
        self.assertEqual(
            tuple(change.field for change in plan_a.changes),
            (
                "models.default",
                "models.fast",
                "purpose_tags",
            ),
        )
        self.assertEqual(
            plan_a.changes[-1].old,
            ("coding", "primary"),
        )
        self.assertEqual(
            plan_a.changes[-1].new,
            ("primary", "review"),
        )
        self.assertEqual(
            canonical_change_plan_json(plan_a),
            canonical_change_plan_json(plan_b),
        )
        self.assertEqual(
            canonical_change_plan_json(plan_a),
            canonical_change_plan_json(plan_c),
        )
        self.assertEqual(
            canonical_change_plan_json(plan_a).encode("utf-8"),
            canonical_change_plan_json(plan_b).encode("utf-8"),
        )
        self.assertEqual(plan_a.digest, plan_b.digest)
        self.assertEqual(plan_a.digest, plan_c.digest)

    def test_canonical_json_digest_and_previews_have_fixed_contract(self) -> None:
        plan = build_change_plan(
            mode=RuntimeMode.COMPANION,
            target=ProviderRef(
                store=COMPANION_STORE_ID,
                provider_id="provider-public-id",
            ),
            store_fingerprint=FINGERPRINT,
            changes={
                "models.reasoning": (None, "reasoning-new"),
                "models.default": ("default-old", "default-new"),
            },
        )

        expected_json = (
            '{"changes":[{"field":"models.default","new":"default-new",'
            '"old":"default-old"},{"field":"models.reasoning",'
            '"new":"reasoning-new","old":null}],"mode":"companion",'
            '"schemaVersion":1,"storeFingerprint":"'
            + FINGERPRINT
            + '","target":{"providerId":"provider-public-id",'
            '"store":"cc-switch"},"unchanged":{"baseUrl":"unchanged",'
            '"credential":"unchanged","current":"unchanged",'
            '"proxyTakeover":"unchanged"}}'
        )
        expected_tui = "\n".join(
            (
                "Change plan v1",
                "Mode: companion",
                "Target: cc-switch/provider-public-id",
                f"Store fingerprint: {FINGERPRINT}",
                'models.default: "default-old" -> "default-new"',
                'models.reasoning: null -> "reasoning-new"',
                "baseUrl: unchanged",
                "credential: unchanged",
                "current: unchanged",
                "proxyTakeover: unchanged",
                (
                    "Digest: "
                    + hashlib.sha256(
                        expected_json.encode("utf-8")
                    ).hexdigest()
                ),
            )
        )

        self.assertEqual(canonical_change_plan_json(plan), expected_json)
        self.assertEqual(json_preview(plan), expected_json)
        self.assertEqual(tui_preview(plan), expected_tui)
        self.assertEqual(
            change_plan_digest(plan),
            hashlib.sha256(expected_json.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(plan.digest, change_plan_digest(plan))
        self.assertEqual(plan.to_canonical_json(), expected_json)

        payload = json.loads(expected_json)
        self.assertEqual(
            payload["unchanged"],
            {
                "baseUrl": "unchanged",
                "credential": "unchanged",
                "current": "unchanged",
                "proxyTakeover": "unchanged",
            },
        )

    def test_only_model_slots_and_standalone_purpose_tags_are_allowed(
        self,
    ) -> None:
        model_fields = (
            "models.default",
            "models.fast",
            "models.reasoning",
            "models.coding",
            "models.long_context",
            "models.fallback",
        )
        for field_name in model_fields:
            with self.subTest(field=field_name):
                plan = companion_plan(
                    changes={field_name: ("model-old", "model-new")},
                )
                self.assertEqual(plan.changes[0].field, field_name)

        forbidden_fields = (
            "baseUrl",
            "credential",
            "current",
            "proxyTakeover",
            "secretRef",
            "models",
            "models.unknown",
            "rawConfig",
        )
        for field_name in forbidden_fields:
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(
                    InvalidChangePlanError,
                    "^change field is not allowed$",
                ):
                    companion_plan(
                        changes={
                            field_name: (
                                "old-public-value",
                                "new-public-value",
                            )
                        }
                    )

        standalone = build_change_plan(
            mode=RuntimeMode.STANDALONE,
            target=PlanTarget(
                store=STANDALONE_STORE_ID,
                provider_id=STANDALONE_PROFILE_ID,
            ),
            store_fingerprint=FINGERPRINT,
            changes={
                "purpose_tags": (
                    ("primary",),
                    ("coding",),
                )
            },
        )
        self.assertEqual(
            standalone.changes,
            (
                FieldChange(
                    "purpose_tags",
                    ("primary",),
                    ("coding",),
                ),
            ),
        )
        with self.assertRaisesRegex(
            InvalidChangePlanError,
            "^purpose tags require standalone mode$",
        ):
            companion_plan(
                changes={
                    "purpose_tags": (
                        ("primary",),
                        ("coding",),
                    )
                }
            )

    def test_mode_and_canonical_store_id_must_match(self) -> None:
        valid_pairs = (
            (RuntimeMode.COMPANION, COMPANION_STORE_ID),
            (RuntimeMode.STANDALONE, STANDALONE_STORE_ID),
        )
        for mode, store_id in valid_pairs:
            with self.subTest(mode=mode.value):
                provider_id = (
                    STANDALONE_PROFILE_ID
                    if mode is RuntimeMode.STANDALONE
                    else "provider-public-id"
                )
                plan = build_change_plan(
                    mode=mode,
                    target=PlanTarget(
                        store=store_id,
                        provider_id=provider_id,
                    ),
                    store_fingerprint=FINGERPRINT,
                    changes={
                        "models.default": (
                            "model-old",
                            "model-new",
                        )
                    },
                )
                self.assertEqual(plan.target.store, store_id)
                if mode is RuntimeMode.STANDALONE:
                    self.assertEqual(
                        json.loads(canonical_change_plan_json(plan))[
                            "target"
                        ],
                        {
                            "providerId": STANDALONE_PROFILE_ID,
                            "store": STANDALONE_STORE_ID,
                        },
                    )
                    self.assertIn(
                        (
                            "Target: standalone/"
                            + STANDALONE_PROFILE_ID
                        ),
                        tui_preview(plan).splitlines(),
                    )

        invalid_pairs = (
            (RuntimeMode.COMPANION, STANDALONE_STORE_ID),
            (RuntimeMode.STANDALONE, COMPANION_STORE_ID),
            (RuntimeMode.COMPANION, "memory"),
            (RuntimeMode.STANDALONE, "ccswitch"),
        )
        for mode, store_id in invalid_pairs:
            with self.subTest(mode=mode.value, store=store_id):
                with self.assertRaisesRegex(
                    InvalidChangePlanError,
                    "^change plan target does not match runtime mode$",
                ):
                    build_change_plan(
                        mode=mode,
                        target=PlanTarget(
                            store=store_id,
                            provider_id="provider-public-id",
                        ),
                        store_fingerprint=FINGERPRINT,
                        changes={
                            "models.default": (
                                "model-old",
                                "model-new",
                            )
                        },
                    )

        invalid_standalone_ids = (
            "profile-public-id",
            STANDALONE_PROFILE_ID.upper(),
            STANDALONE_PROFILE_ID.replace("-", ""),
        )
        for provider_id in invalid_standalone_ids:
            with self.subTest(provider_id_style=provider_id[:8]):
                with self.assertRaisesRegex(
                    InvalidChangePlanError,
                    "^standalone target must use a canonical UUID$",
                ):
                    build_change_plan(
                        mode=RuntimeMode.STANDALONE,
                        target=PlanTarget(
                            store=STANDALONE_STORE_ID,
                            provider_id=provider_id,
                        ),
                        store_fingerprint=FINGERPRINT,
                        changes={
                            "models.default": (
                                "model-old",
                                "model-new",
                            )
                        },
                    )

    def test_invalid_binding_and_empty_diff_are_rejected(self) -> None:
        invalid_modes = (
            RuntimeMode.EMPTY,
            RuntimeMode.INCOMPATIBLE,
            "unknown-mode",
        )
        for mode in invalid_modes:
            with self.subTest(mode=str(mode)):
                with self.assertRaisesRegex(
                    InvalidChangePlanError,
                    "^change plan mode is unsupported$",
                ):
                    companion_plan(mode=mode)

        invalid_fingerprints = (
            "",
            "a" * 63,
            "A" * 64,
            "g" * 64,
            64,
        )
        for fingerprint in invalid_fingerprints:
            with self.subTest(fingerprint_type=type(fingerprint).__name__):
                with self.assertRaisesRegex(
                    InvalidChangePlanError,
                    "^store fingerprint must be a SHA-256 digest$",
                ):
                    companion_plan(fingerprint=fingerprint)

        for version in (0, 2, True, "1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    InvalidChangePlanError,
                    "^change plan schema version is unsupported$",
                ):
                    companion_plan(schema_version=version)

        with self.assertRaisesRegex(
            EmptyChangePlanError,
            "^change plan must not be empty$",
        ):
            companion_plan(changes={})
        with self.assertRaisesRegex(
            EmptyChangePlanError,
            "^change plan must not be empty$",
        ):
            companion_plan(
                changes={
                    "models.default": (
                        "same-model",
                        "same-model",
                    )
                }
            )
        with self.assertRaisesRegex(
            EmptyChangePlanError,
            "^change plan must not be empty$",
        ):
            build_change_plan(
                mode=RuntimeMode.STANDALONE,
                target=PlanTarget(
                    store=STANDALONE_STORE_ID,
                    provider_id=STANDALONE_PROFILE_ID,
                ),
                store_fingerprint=FINGERPRINT,
                changes={
                    "purpose_tags": (
                        ("coding", "primary"),
                        ("primary", "coding", "coding"),
                    )
                },
            )

    def test_canaries_never_enter_repr_json_tui_or_exceptions(self) -> None:
        private_name = "Private customer provider alpha"
        plaintext_key = "sk-live-fixture-canary-123456"
        full_url = "".join(
            (
                "https://",
                "private-user",
                ":",
                "private-password",
                "@",
                "private-fixture.invalid/v1?api_key=forbidden",
            )
        )
        raw_config = '{"models":{"default":"raw-config-canary"}}'
        target = ProviderRef(
            store=COMPANION_STORE_ID,
            provider_id="provider-public-id",
            display_name=private_name,
        )
        plan = companion_plan(target=target)
        surfaces = (
            repr(plan),
            repr(plan.target),
            repr(plan.changes),
            canonical_change_plan_json(plan),
            json_preview(plan),
            tui_preview(plan),
        )
        for surface in surfaces:
            for canary in (
                private_name,
                plaintext_key,
                full_url,
                raw_config,
                "private-password",
            ):
                self.assertNotIn(canary, surface)

        invalid_cases = (
            {
                f"private-field-{plaintext_key}": (
                    "model-old",
                    "model-new",
                )
            },
            {"models.default": ("model-old", plaintext_key)},
            {"models.default": ("model-old", full_url)},
            {"models.default": ("model-old", raw_config)},
            {"purpose_tags": (("primary",), (plaintext_key,))},
            {"purpose_tags": (("primary",), (raw_config,))},
        )
        for changes in invalid_cases:
            with self.subTest(field=next(iter(changes))):
                with self.assertRaises(InvalidChangePlanError) as captured:
                    build_change_plan(
                        mode=RuntimeMode.STANDALONE,
                        target=PlanTarget(
                            store=STANDALONE_STORE_ID,
                            provider_id=STANDALONE_PROFILE_ID,
                        ),
                        store_fingerprint=FINGERPRINT,
                        changes=changes,
                    )
                failure = f"{captured.exception!s} {captured.exception!r}"
                for canary in (
                    private_name,
                    plaintext_key,
                    full_url,
                    raw_config,
                    "private-password",
                ):
                    self.assertNotIn(canary, failure)

        invalid_bindings = (
            {
                "mode": private_name,
                "target": target,
                "store_fingerprint": FINGERPRINT,
            },
            {
                "mode": RuntimeMode.COMPANION,
                "target": PlanTarget(
                    store=COMPANION_STORE_ID,
                    provider_id="provider-public-id",
                ),
                "store_fingerprint": plaintext_key,
            },
        )
        for overrides in invalid_bindings:
            with self.subTest(binding=tuple(overrides)):
                with self.assertRaises(InvalidChangePlanError) as captured:
                    ChangePlan(
                        changes={
                            "models.default": (
                                "model-old",
                                "model-new",
                            )
                        },  # type: ignore[arg-type]
                        **overrides,  # type: ignore[arg-type]
                    )
                failure = f"{captured.exception!s} {captured.exception!r}"
                for canary in (
                    private_name,
                    plaintext_key,
                    full_url,
                    raw_config,
                ):
                    self.assertNotIn(canary, failure)

        with self.assertRaises(InvalidChangePlanError) as target_error:
            PlanTarget(
                store=COMPANION_STORE_ID,
                provider_id=full_url,
            )
        self.assertNotIn(full_url, str(target_error.exception))
        self.assertNotIn(full_url, repr(target_error.exception))


class ChangePlanIsolationTests(unittest.TestCase):
    def test_construct_and_preview_leave_fixture_stores_byte_identical(
        self,
    ) -> None:
        private_name = "Private fixture provider name"
        plaintext_key = "sk-live-store-fixture-canary-987654"
        full_url = "https://private-store-fixture.invalid/private/v1"
        raw_config = json.dumps(
            {
                "api_key": plaintext_key,
                "base_url": full_url,
                "privateMetadata": "raw-config-fixture-canary",
            },
            sort_keys=True,
        )

        with tempfile.TemporaryDirectory() as raw_directory:
            directory = pathlib.Path(raw_directory)
            database_path = directory / "fixture.db"
            connection = sqlite3.connect(database_path)
            try:
                connection.execute(
                    "CREATE TABLE providers (id TEXT, raw_config TEXT)"
                )
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?)",
                    ("provider-public-id", raw_config),
                )
                connection.commit()
            finally:
                connection.close()

            standalone_path = directory / "standalone.json"
            standalone_store = StandaloneProfileStore(standalone_path)
            standalone_store.create(
                StandaloneProfile(
                    profile_id=UUID(STANDALONE_PROFILE_ID),
                    name=private_name,
                    base_url=full_url,
                    adapter=ProtocolAdapter.ANTHROPIC,
                    secret_ref=UUID(
                        "66666666-7777-4888-8999-000000000000"
                    ),
                    created_at=CREATED_AT,
                    updated_at=CREATED_AT,
                    models=ModelMapping(default="model-default-old"),
                    purpose_tags=("primary",),
                )
            )
            database_before = database_path.read_bytes()
            standalone_before = standalone_path.read_bytes()

            with (
                mock.patch.object(
                    pathlib.Path,
                    "home",
                    side_effect=AssertionError("HOME access is forbidden"),
                ),
                mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError(
                        "network access is forbidden"
                    ),
                ),
                mock.patch.object(
                    sqlite3,
                    "connect",
                    side_effect=AssertionError(
                        "database access is forbidden"
                    ),
                ),
            ):
                companion = build_change_plan(
                    mode=RuntimeMode.COMPANION,
                    target=ProviderRef(
                        store=COMPANION_STORE_ID,
                        provider_id="provider-public-id",
                        display_name=private_name,
                    ),
                    store_fingerprint=hashlib.sha256(
                        database_before
                    ).hexdigest(),
                    changes={
                        "models.default": (
                            "model-default-old",
                            "model-default-new",
                        )
                    },
                )
                standalone = build_change_plan(
                    mode=RuntimeMode.STANDALONE,
                    target=ProviderRef(
                        store=STANDALONE_STORE_ID,
                        provider_id=STANDALONE_PROFILE_ID,
                        display_name=private_name,
                    ),
                    store_fingerprint=hashlib.sha256(
                        standalone_before
                    ).hexdigest(),
                    changes={
                        "models.default": (
                            "model-default-old",
                            "model-default-new",
                        ),
                        "purpose_tags": (
                            ("primary",),
                            ("coding", "primary"),
                        ),
                    },
                )
                rendered = (
                    repr(companion),
                    canonical_change_plan_json(companion),
                    tui_preview(companion),
                    repr(standalone),
                    canonical_change_plan_json(standalone),
                    tui_preview(standalone),
                )

            self.assertEqual(database_path.read_bytes(), database_before)
            self.assertEqual(standalone_path.read_bytes(), standalone_before)
            for surface in rendered:
                for canary in (
                    private_name,
                    plaintext_key,
                    full_url,
                    raw_config,
                    "raw-config-fixture-canary",
                ):
                    self.assertNotIn(canary, surface)

    def test_module_has_no_store_or_external_io_dependency(self) -> None:
        source_path = pathlib.Path(change_plan.__file__)
        parsed = ast.parse(source_path.read_text(encoding="utf-8"))
        direct_imports: set[str] = set()
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                direct_imports.update(
                    alias.name.split(".", maxsplit=1)[0]
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                direct_imports.add(node.module.split(".", maxsplit=1)[0])

        self.assertTrue(
            {
                "os",
                "pathlib",
                "socket",
                "sqlite3",
                "subprocess",
                "keyring",
                "standalone",
                "store",
            }.isdisjoint(direct_imports)
        )


if __name__ == "__main__":
    unittest.main()
