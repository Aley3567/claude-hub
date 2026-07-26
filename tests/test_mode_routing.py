from __future__ import annotations

import pathlib
import sys
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.domain import RuntimeMode, StoreCapability  # noqa: E402
from claude_hub.routing import FirstScreen, resolve_startup_route  # noqa: E402
from claude_hub.service import ProviderApplicationService  # noqa: E402
from claude_hub.testing import InMemoryProviderStore  # noqa: E402
from claude_hub.tui import resolve_tui_startup  # noqa: E402


class StartupRoutingTests(unittest.TestCase):
    def test_capabilities_and_standalone_presence_resolve_four_modes(self) -> None:
        cases = (
            (
                StoreCapability.COMPATIBLE,
                False,
                RuntimeMode.COMPANION,
                FirstScreen.PROVIDER_LIST,
            ),
            (
                StoreCapability.READ_ONLY,
                True,
                RuntimeMode.COMPANION,
                FirstScreen.PROVIDER_LIST,
            ),
            (
                StoreCapability.ABSENT,
                True,
                RuntimeMode.STANDALONE,
                FirstScreen.PROFILE_LIST,
            ),
            (
                StoreCapability.ABSENT,
                False,
                RuntimeMode.EMPTY,
                FirstScreen.QUICK_SETUP,
            ),
            (
                StoreCapability.INCOMPATIBLE,
                True,
                RuntimeMode.INCOMPATIBLE,
                FirstScreen.INCOMPATIBLE_ERROR,
            ),
            (
                StoreCapability.CORRUPT,
                True,
                RuntimeMode.INCOMPATIBLE,
                FirstScreen.INCOMPATIBLE_ERROR,
            ),
        )

        for capability, standalone_exists, mode, screen in cases:
            with self.subTest(capability=capability):
                route = resolve_startup_route(
                    capability,
                    standalone_exists=standalone_exists,
                )
                self.assertIs(route.mode, mode)
                self.assertIs(route.first_screen, screen)

    def test_explicit_standalone_is_the_only_fail_closed_override(self) -> None:
        existing = resolve_startup_route(
            StoreCapability.INCOMPATIBLE,
            standalone_exists=True,
            store_override="standalone",
        )
        new = resolve_startup_route(
            StoreCapability.CORRUPT,
            standalone_exists=False,
            store_override="standalone",
        )

        self.assertIs(existing.mode, RuntimeMode.STANDALONE)
        self.assertIs(existing.first_screen, FirstScreen.PROFILE_LIST)
        self.assertIs(new.mode, RuntimeMode.EMPTY)
        self.assertIs(new.first_screen, FirstScreen.QUICK_SETUP)
        with self.assertRaisesRegex(ValueError, "^unsupported store override$"):
            resolve_startup_route(
                StoreCapability.ABSENT,
                standalone_exists=False,
                store_override="fixture-unsupported",
            )

    def test_application_service_reuses_the_same_ui_resolver(self) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(
                capability=StoreCapability.INCOMPATIBLE,
            )
        )

        route = service.resolve_startup(
            standalone_exists=True,
            store_override="standalone",
        )

        self.assertEqual(
            route,
            resolve_startup_route(
                StoreCapability.INCOMPATIBLE,
                standalone_exists=True,
                store_override="standalone",
            ),
        )

    def test_tui_adapter_routes_every_mode_through_the_shared_service(
        self,
    ) -> None:
        cases = (
            (
                StoreCapability.COMPATIBLE,
                False,
                RuntimeMode.COMPANION,
                FirstScreen.PROVIDER_LIST,
            ),
            (
                StoreCapability.ABSENT,
                True,
                RuntimeMode.STANDALONE,
                FirstScreen.PROFILE_LIST,
            ),
            (
                StoreCapability.ABSENT,
                False,
                RuntimeMode.EMPTY,
                FirstScreen.QUICK_SETUP,
            ),
            (
                StoreCapability.INCOMPATIBLE,
                True,
                RuntimeMode.INCOMPATIBLE,
                FirstScreen.INCOMPATIBLE_ERROR,
            ),
        )

        for capability, standalone_exists, mode, screen in cases:
            with self.subTest(capability=capability):
                service = ProviderApplicationService(
                    InMemoryProviderStore(capability=capability)
                )

                route = resolve_tui_startup(
                    service,
                    standalone_exists=standalone_exists,
                )

                self.assertIs(route.mode, mode)
                self.assertIs(route.first_screen, screen)


if __name__ == "__main__":
    unittest.main()
