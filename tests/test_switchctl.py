from __future__ import annotations

import io
import json
import pathlib
import sys
import unittest
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.domain import StoreCapability  # noqa: E402
from claude_hub.service import ProviderApplicationService  # noqa: E402
from claude_hub.switchctl import (  # noqa: E402
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    EXIT_USAGE,
    main,
)
from claude_hub.testing import InMemoryProviderStore  # noqa: E402
from claude_hub.update_check import (  # noqa: E402
    HttpResponse,
    UpdateCheckSettings,
    UpdateChecker,
)


SUCCESS_GOLDEN = (
    '{"schemaVersion":1,"ok":true,'
    '"data":{"capability":"compatible"},"error":null}\n'
)
FAILURE_GOLDEN = (
    '{"schemaVersion":1,"ok":false,"data":null,'
    '"error":{"code":"runtime_error","message":"detect failed"}}\n'
)
USAGE_GOLDEN = (
    '{"schemaVersion":1,"ok":false,"data":null,'
    '"error":{"code":"usage_error","message":"usage: switchctl detect | '
    'switchctl check-update [--disabled]"}}\n'
)
DEFAULT_GOLDEN = (
    '{"schemaVersion":1,"ok":true,'
    '"data":{"capability":"absent"},"error":null}\n'
)


class _ExplodingStore:
    def __init__(self, private_detail: str) -> None:
        self._private_detail = private_detail

    def detect(self) -> StoreCapability:
        raise RuntimeError(self._private_detail)

    def list(self):
        return ()

    def inspect(self, reference):
        raise RuntimeError(self._private_detail)


class SwitchctlDetectTests(unittest.TestCase):
    def _run(
        self,
        argv: list[str],
        *,
        service: ProviderApplicationService | None = None,
        update_checker: UpdateChecker | None = None,
        update_settings: UpdateCheckSettings | None = None,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = main(
            argv,
            service=service,
            update_checker=update_checker,
            update_settings=update_settings,
            stdout=stdout,
            stderr=stderr,
        )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_detect_success_matches_single_line_golden(self) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(capability=StoreCapability.COMPATIBLE)
        )

        exit_code, stdout, stderr = self._run(["detect"], service=service)

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout, SUCCESS_GOLDEN)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("\n"), 1)
        self.assertEqual(
            list(json.loads(stdout)),
            ["schemaVersion", "ok", "data", "error"],
        )

    def test_default_detect_is_explicit_absent_fake_boundary(self) -> None:
        exit_code, stdout, stderr = self._run(["detect"])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout, DEFAULT_GOLDEN)
        self.assertEqual(stderr, "")

    def test_runtime_failure_matches_golden_and_redacts_exception(self) -> None:
        sensitive = "fixture-" + "secret" + "-material"
        private_path = str(pathlib.Path("/private") / "fixture" / "store.db")
        exception_detail = f"{private_path}: {sensitive}"
        service = ProviderApplicationService(_ExplodingStore(exception_detail))

        exit_code, stdout, stderr = self._run(["detect"], service=service)

        self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(stdout, FAILURE_GOLDEN)
        self.assertEqual(stderr, "switchctl: runtime_error\n")
        combined = stdout + stderr
        self.assertNotIn(sensitive, combined)
        self.assertNotIn(private_path, combined)
        self.assertNotIn("RuntimeError", combined)
        self.assertNotIn("ExplodingStore", combined)

    def test_default_service_construction_failure_uses_runtime_envelope(self) -> None:
        sensitive = "fixture-" + "secret" + "-construction"
        private_path = str(pathlib.Path("/private") / "fixture" / "store.db")
        with mock.patch(
            "claude_hub.switchctl.build_default_service",
            side_effect=RuntimeError(f"{private_path}: {sensitive}"),
        ):
            exit_code, stdout, stderr = self._run(["detect"])

        self.assertEqual(exit_code, EXIT_RUNTIME_ERROR)
        self.assertEqual(stdout, FAILURE_GOLDEN)
        self.assertEqual(stderr, "switchctl: runtime_error\n")
        combined = stdout + stderr
        self.assertNotIn(sensitive, combined)
        self.assertNotIn(private_path, combined)
        self.assertNotIn("RuntimeError", combined)

    def test_usage_error_is_stable_and_does_not_echo_argv(self) -> None:
        sensitive_argument = "fixture-" + "secret" + "-argument"

        exit_code, stdout, stderr = self._run(
            ["detect", sensitive_argument]
        )

        self.assertEqual(exit_code, EXIT_USAGE)
        self.assertEqual(stdout, USAGE_GOLDEN)
        self.assertEqual(stderr, "switchctl: usage_error\n")
        self.assertNotIn(sensitive_argument, stdout + stderr)

    def test_injected_argv_does_not_consult_process_argv(self) -> None:
        service = ProviderApplicationService(
            InMemoryProviderStore(capability=StoreCapability.COMPATIBLE)
        )
        with mock.patch.object(
            sys,
            "argv",
            ["/private/fixture/switchctl", "unexpected-command"],
        ):
            exit_code, stdout, stderr = self._run(
                ["detect"],
                service=service,
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stdout, SUCCESS_GOLDEN)
        self.assertEqual(stderr, "")
        self.assertNotIn("/private/fixture", stdout)

    def test_help_is_also_a_single_json_document(self) -> None:
        exit_code, stdout, stderr = self._run(["--help"])

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "schemaVersion": 1,
                "ok": True,
                "data": {
                    "usage": (
                        "switchctl detect | "
                        "switchctl check-update [--disabled]"
                    )
                },
                "error": None,
            },
        )
        self.assertEqual(stdout.count("\n"), 1)

    def test_check_update_preserves_envelope_and_stable_success_exit(self) -> None:
        class _Client:
            def send(self, request):
                return HttpResponse(
                    status=200,
                    body=(
                        b'[{"tag_name":"v0.2.0","prerelease":false,'
                        b'"draft":false}]'
                    ),
                )

        checker = UpdateChecker(
            http_client=_Client(),
            settings=UpdateCheckSettings(cache_enabled=False),
        )

        exit_code, stdout, stderr = self._run(
            ["check-update"],
            update_checker=checker,
        )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count("\n"), 1)
        payload = json.loads(stdout)
        self.assertEqual(
            list(payload),
            ["schemaVersion", "ok", "data", "error"],
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["update"]["status"], "available")
        self.assertEqual(
            payload["data"]["update"]["latestVersion"],
            "0.2.0",
        )

    def test_disabled_check_update_makes_no_request_and_still_succeeds(self) -> None:
        with (
            mock.patch(
                "claude_hub.switchctl.build_default_checker",
                side_effect=AssertionError("checker construction used"),
            ),
            mock.patch(
                "claude_hub.update_check.urllib.request.urlopen",
                side_effect=AssertionError("network used"),
            ),
        ):
            exit_code, stdout, stderr = self._run(
                ["check-update", "--disabled"],
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout)["data"]["update"]["status"],
            "disabled",
        )

    def test_injected_disabled_setting_is_also_a_zero_request_contract(
        self,
    ) -> None:
        with mock.patch(
            "claude_hub.update_check.urllib.request.urlopen",
            side_effect=AssertionError("network used"),
        ):
            exit_code, stdout, stderr = self._run(
                ["check-update"],
                update_settings=UpdateCheckSettings(enabled=False),
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout)["data"]["update"]["status"],
            "disabled",
        )

    def test_offline_check_is_unavailable_without_changing_exit_code(self) -> None:
        class _OfflineClient:
            def send(self, request):
                raise OSError("private network detail")

        checker = UpdateChecker(
            http_client=_OfflineClient(),
            settings=UpdateCheckSettings(cache_enabled=False),
        )

        exit_code, stdout, stderr = self._run(
            ["check-update"],
            update_checker=checker,
        )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["data"]["update"]["status"],
            "unavailable",
        )


if __name__ == "__main__":
    unittest.main()
