from __future__ import annotations

import ast
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
TESTS_ROOT = REPOSITORY_ROOT / "tests"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from claude_hub.desktop import (  # noqa: E402
    DESKTOP_INSTALL_COMMAND,
    DesktopDependencyError,
    PySide6QtFacade,
    build_desktop_install_command,
    run_desktop,
)
from claude_hub.domain import StoreCapability  # noqa: E402
from claude_hub.entrypoints import (  # noqa: E402
    EXIT_DESKTOP_DEPENDENCY,
    EXIT_OK,
    hub_main,
)
from claude_hub.service import ProviderApplicationService  # noqa: E402
from claude_hub.testing import InMemoryProviderStore  # noqa: E402
from desktop_smoke import ScheduledExecQtFacade  # noqa: E402


class _FakeService:
    def __init__(
        self,
        events: list[object],
        capability: StoreCapability = StoreCapability.COMPATIBLE,
    ) -> None:
        self.events = events
        self.capability = capability

    def detect(self) -> StoreCapability:
        self.events.append("service.detect")
        return self.capability


class _FakeApplication:
    def __init__(self, events: list[object], exit_code: int) -> None:
        self.events = events
        self.exit_code = exit_code

    def exec(self) -> int:
        self.events.append("application.exec")
        return self.exit_code

    def quit(self) -> None:
        self.events.append("application.quit")


class _FakeWindow:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def show(self) -> None:
        self.events.append("window.show")


class _FakeQtFacade:
    def __init__(self, events: list[object], exit_code: int = 0) -> None:
        self.events = events
        self.application = _FakeApplication(events, exit_code)
        self.window = _FakeWindow(events)

    def create_application(self, argv):
        self.events.append(("qt.create_application", tuple(argv)))
        return self.application

    def create_main_window(self, *, capability):
        self.events.append(("qt.create_main_window", capability))
        return self.window


class DesktopShellTests(unittest.TestCase):
    def test_install_command_quotes_the_active_python_and_desktop_extra(
        self,
    ) -> None:
        command = build_desktop_install_command(
            "/fixture/Python Runtime's/bin/python"
        )

        if os.name == "nt":
            self.assertIn(
                "/fixture/Python Runtime's/bin/python",
                command,
            )
        else:
            self.assertTrue(
                command.startswith(
                    "'/fixture/Python Runtime'\"'\"'s/bin/python'"
                )
            )
        self.assertIn(" -m pip install ", command)
        self.assertIn("claude-hub-kit[desktop]", command)

    def test_real_facade_uses_only_basic_qt_widgets_contract(self) -> None:
        widgets = mock.Mock()
        widgets.QApplication.instance.return_value = None
        application = widgets.QApplication.return_value
        window = widgets.QMainWindow.return_value
        central_widget = widgets.QWidget.return_value
        layout = widgets.QVBoxLayout.return_value
        labels = [mock.Mock(), mock.Mock(), mock.Mock()]
        widgets.QLabel.side_effect = labels
        facade = PySide6QtFacade(widgets)

        created_application = facade.create_application(("claude-hub",))
        created_window = facade.create_main_window(
            capability=StoreCapability.COMPATIBLE,
        )

        self.assertIs(created_application, application)
        widgets.QApplication.instance.assert_called_once_with()
        widgets.QApplication.assert_called_once_with(["claude-hub"])
        self.assertIs(created_window, window)
        window.setWindowTitle.assert_called_once_with("claude-hub")
        window.resize.assert_called_once_with(640, 360)
        widgets.QWidget.assert_called_once_with(window)
        widgets.QVBoxLayout.assert_called_once_with(central_widget)
        window.setCentralWidget.assert_called_once_with(central_widget)
        self.assertEqual(layout.addWidget.call_count, 3)
        layout.addStretch.assert_called_once_with()
        self.assertEqual(
            widgets.QLabel.call_args_list[-1],
            mock.call(
                "Provider store: compatible",
                central_widget,
            ),
        )

    def test_shell_calls_only_shared_detect_before_show_and_event_loop(
        self,
    ) -> None:
        events: list[object] = []
        service = _FakeService(events)
        qt = _FakeQtFacade(events, exit_code=17)

        exit_code = run_desktop(
            service,
            qt=qt,
            argv=("claude-hub", "--fixture"),
        )

        self.assertEqual(exit_code, 17)
        self.assertEqual(
            events,
            [
                (
                    "qt.create_application",
                    ("claude-hub", "--fixture"),
                ),
                "service.detect",
                (
                    "qt.create_main_window",
                    StoreCapability.COMPATIBLE,
                ),
                "window.show",
                "application.exec",
            ],
        )

    def test_smoke_schedules_quit_after_show_immediately_before_exec(
        self,
    ) -> None:
        events: list[object] = []
        scheduled_callbacks = []
        real_qt = _FakeQtFacade(events, exit_code=23)

        def single_shot(delay_ms, callback) -> None:
            events.append(("timer.single_shot", delay_ms))
            scheduled_callbacks.append(callback)

        smoke_qt = ScheduledExecQtFacade(real_qt, single_shot)

        exit_code = run_desktop(
            _FakeService(events),
            qt=smoke_qt,
            argv=("claude-hub-smoke",),
        )

        self.assertEqual(exit_code, 23)
        self.assertEqual(
            events,
            [
                (
                    "qt.create_application",
                    ("claude-hub-smoke",),
                ),
                "service.detect",
                (
                    "qt.create_main_window",
                    StoreCapability.COMPATIBLE,
                ),
                "window.show",
                ("timer.single_shot", 25),
                "application.exec",
            ],
        )
        self.assertEqual(len(scheduled_callbacks), 1)
        scheduled_callbacks[0]()
        self.assertEqual(events[-1], "application.quit")

    def test_cli_dispatches_gui_to_injected_service_and_qt(self) -> None:
        events: list[object] = []
        stderr = io.StringIO()
        service = ProviderApplicationService(
            InMemoryProviderStore(
                capability=StoreCapability.READ_ONLY,
            )
        )

        status = hub_main(
            ["gui"],
            service=service,
            qt=_FakeQtFacade(events),
            stderr=stderr,
        )

        self.assertEqual(status, EXIT_OK)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(
            ("qt.create_main_window", StoreCapability.READ_ONLY),
            events,
        )

    def test_cli_default_service_is_the_isolated_absent_boundary(self) -> None:
        events: list[object] = []

        status = hub_main(
            ["gui"],
            qt=_FakeQtFacade(events),
            stderr=io.StringIO(),
        )

        self.assertEqual(status, EXIT_OK)
        self.assertIn(
            ("qt.create_main_window", StoreCapability.ABSENT),
            events,
        )

    def test_missing_extra_has_stable_exit_and_exact_install_command(
        self,
    ) -> None:
        events: list[object] = []
        stderr = io.StringIO()
        with mock.patch(
            "claude_hub.desktop.load_default_qt_facade",
            side_effect=DesktopDependencyError("private loader detail"),
        ):
            status = hub_main(
                ["gui"],
                service=_FakeService(events),
                stderr=stderr,
            )

        self.assertEqual(status, EXIT_DESKTOP_DEPENDENCY)
        self.assertEqual(events, [])
        self.assertEqual(
            stderr.getvalue(),
            "claude-hub: desktop dependencies are not installed.\n"
            "claude-hub: install them with: "
            f"{DESKTOP_INSTALL_COMMAND}\n",
        )
        self.assertNotIn("private loader detail", stderr.getvalue())

    def test_startup_failure_is_nonzero_and_redacts_exception(self) -> None:
        events: list[object] = []
        stderr = io.StringIO()

        class _ExplodingQt(_FakeQtFacade):
            def create_application(self, argv):
                raise RuntimeError("private Qt platform detail")

        status = hub_main(
            ["gui"],
            service=_FakeService(events),
            qt=_ExplodingQt(events),
            stderr=stderr,
        )

        self.assertEqual(status, 1)
        self.assertEqual(
            stderr.getvalue(),
            "claude-hub: desktop failed to start.\n",
        )
        self.assertNotIn("private Qt platform detail", stderr.getvalue())

    def test_hub_help_advertises_gui_without_loading_qt(self) -> None:
        stdout = io.StringIO()
        with (
            mock.patch(
                "claude_hub.desktop.load_default_qt_facade",
                side_effect=AssertionError("Qt loader used"),
            ),
            mock.patch("sys.stdout", stdout),
        ):
            status = hub_main(["--help"])

        self.assertEqual(status, EXIT_OK)
        normalized = " ".join(stdout.getvalue().split())
        self.assertIn("usage: claude-hub", normalized)
        self.assertIn("gui", normalized)
        self.assertIn("Qt Widgets desktop shell", normalized)

    def test_desktop_module_has_no_direct_storage_or_secret_backend_import(
        self,
    ) -> None:
        source_path = SOURCE_ROOT / "claude_hub" / "desktop.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module = f"{'.' * node.level}{node.module}"
                imported_modules.add(module)
                imported_modules.update(
                    f"{module}.{alias.name}" for alias in node.names
                )

        forbidden = {
            "json",
            "keyring",
            "sqlite3",
            "PySide6.QtQml",
            "PySide6.QtSql",
            "PySide6.QtWebEngineWidgets",
        }
        self.assertFalse(
            any(
                module == forbidden_module
                or module.startswith(f"{forbidden_module}.")
                for module in imported_modules
                for forbidden_module in forbidden
            )
        )
        self.assertFalse(
            any(
                module == ".store" or module.startswith(".store.")
                for module in imported_modules
            )
        )
        self.assertIn(".service", imported_modules)

    @unittest.skipUnless(
        importlib.util.find_spec("PySide6") is not None,
        "PySide6 desktop extra is not installed",
    )
    def test_real_qt_widgets_offscreen_smoke_opens_and_closes(self) -> None:
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as raw_cwd:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(TESTS_ROOT / "desktop_smoke.py"),
                ],
                cwd=raw_cwd,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
                timeout=15,
            )

        self.assertEqual(
            completed.returncode,
            0,
            msg=completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
