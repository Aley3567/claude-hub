from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "claude-provider-once.py"


@contextmanager
def loaded_launcher(env: dict[str, str]):
    """Load a fresh launcher module after applying an isolated runtime env."""
    with mock.patch.dict(os.environ, env, clear=False):
        name = f"claude1_launcher_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, LAUNCHER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Register before exec so dataclasses can resolve the module's string
        # annotations (the launcher uses ``from __future__ import annotations``).
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            yield module
        finally:
            sys.modules.pop(name, None)


def write_executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def isolated_env(home: Path, **overrides: str) -> dict[str, str]:
    state = home / "state"
    env = {
        "HOME": str(home),
        "CLAUDE1_HOME": str(home),
        "CLAUDE1_DB_PATH": str(state / "cc-switch.db"),
        "CLAUDE1_MRU_PATH": str(state / "mru.json"),
        "CLAUDE1_CONFIG_PATH": str(state / "config.json"),
        "CLAUDE1_BACKEND_STATE": str(state / "last-session.json"),
        "CLAUDE1_BACKEND_STICKY": str(state / "sticky"),
        "CLAUDE1_ANYROUTER_OBSERVER": str(home / "bin" / "observer"),
        "CLAUDE1_RECLAUDE_BIN": str(home / "bin" / "reclaude"),
        "CLAUDE1_ANYROUTER_SETTINGS": str(home / "settings" / "anyrouter.json"),
        "CLAUDE1_NOTION_MCP": str(home / "settings" / "notion.json"),
        "CLAUDE1_GATEWAY_BIN": str(home / "bin" / "gateway"),
        "CLAUDE1_GATEWAY_CONFIG": str(home / "settings" / "gateway.yaml"),
        "CLAUDE1_GATEWAY_LOG": str(home / "logs" / "gateway.log"),
        "CLAUDE1_HUB_SCRIPT": str(home / "bin" / "claude-hub"),
        "CLAUDE1_HUB_CONFIG": str(home / "settings" / "hub.json"),
        "CLAUDE1_HUB_DB": str(state / "hub.db"),
        "CLAUDE1_HUB_LOG": str(home / "logs" / "hub.log"),
        "CLAUDE1_TMP_DIR": str(home / "tmp"),
        "CLAUDE1_DEFAULT_CLAUDE_BIN": str(home / "bin" / "default-claude"),
    }
    env.update(overrides)
    return env


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def health_server(status_code: int, payload: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args) -> None:
            return

    # HTTPServer performs a reverse-DNS lookup during server_bind, which can
    # stall for tens of seconds on otherwise healthy macOS CI runners.
    server = ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class LauncherTuiLogicTests(unittest.TestCase):
    def test_first_run_uses_cc_switch_order_without_personal_seed_names(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                cfg = {"version": 1, "providers": {}}
                db_order = ["Third Party A", "团队渠道", "Third Party B"]

                self.assertTrue(launcher.sync_config(cfg, db_order))
                self.assertEqual(list(cfg["providers"]), db_order)
                self.assertTrue(
                    all(
                        meta == {"hidden": False}
                        for meta in cfg["providers"].values()
                    )
                )

    def test_mru_sets_cursor_and_recent_badge_without_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                cfg = {
                    "providers": {
                        "Alpha": {"hidden": False},
                        "Beta": {"hidden": False},
                        "Gamma": {"hidden": False},
                    }
                }
                mru = {"Alpha": 10.0, "Gamma": 30.0, "Beta": 20.0}

                view = launcher._build_view(
                    cfg, {"Alpha", "Beta", "Gamma"}, mru, False
                )

                self.assertEqual(view, ["Alpha", "Beta", "Gamma"])
                self.assertEqual(launcher._recent_name(view, mru), "Gamma")
                self.assertEqual(launcher._initial_index(view, mru), 2)

    def test_alias_matching_and_casefolded_conflict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                providers = [
                    {"name": "Alpha Gateway", "alias": "Fast"},
                    {"name": "Beta Gateway", "alias": "Safe"},
                ]
                matches, exact = launcher.match_providers(providers, "fAsT")
                self.assertTrue(exact)
                self.assertEqual(
                    [provider["name"] for provider in matches],
                    ["Alpha Gateway"],
                )

                meta = {
                    "Alpha Gateway": {"alias": "Fast"},
                    "Beta Gateway": {"alias": "Safe"},
                    "Codex": {},
                }
                changed, message = launcher._set_alias(meta, "Codex", "FAST")
                self.assertFalse(changed)
                self.assertIn("Alpha Gateway", message)
                changed, message = launcher._set_alias(
                    meta, "Codex", "bEtA GaTeWaY"
                )
                self.assertFalse(changed)
                self.assertIn("Beta Gateway", message)
                self.assertNotIn("alias", meta["Codex"])
                changed, message = launcher._set_alias(meta, "Codex", "hub")
                self.assertFalse(changed)
                self.assertIn("保留命令", message)
                changed, message = launcher._set_alias(meta, "Codex", "--help")
                self.assertFalse(changed)
                self.assertIn("命令参数", message)

    def test_exact_legacy_alias_collision_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                providers = [
                    {"name": "Alpha", "alias": "same"},
                    {"name": "Beta", "alias": "SAME"},
                ]
                with self.assertRaisesRegex(RuntimeError, "存在冲突"):
                    launcher.choose(providers, "same")

    def test_digit_shortcuts_and_scrolling_cover_fifteen_items(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                self.assertEqual(launcher._digit_index(ord("1")), 0)
                self.assertEqual(launcher._digit_index(ord("9")), 8)
                self.assertEqual(launcher._digit_index(ord("0")), 9)
                self.assertIsNone(launcher._digit_index(ord("x")))
                self.assertEqual(launcher._visible_window(15, 0, 11), (0, 11))
                self.assertEqual(launcher._visible_window(15, 14, 11), (4, 15))

    def test_twenty_four_line_window_keeps_footer_fixed_while_scrolling(self) -> None:
        class FakeWindow:
            def __init__(self) -> None:
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (24, 100)

            def erase(self) -> None:
                self.writes.clear()

            def addstr(self, y: int, x: int, text: str, _attr: int) -> None:
                self.writes.append((y, x, text))

            def refresh(self) -> None:
                return

        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                names = [f"Provider-{index:02d}" for index in range(1, 16)]
                cfg = {
                    "providers": {
                        name: {"hidden": False}
                        for name in names
                    }
                }
                launcher.C = {
                    "brand": 0,
                    "accent": 0,
                    "dim": 0,
                    "warning": 0,
                    "base": 0,
                    "sel": 0,
                }
                launcher._logo_pairs[:] = [0]
                window = FakeWindow()

                launcher._draw_launcher(
                    window, cfg, names, 14, False, {"Provider-15": 1.0}
                )

                footer = [text for y, _x, text in window.writes if y == 23]
                self.assertTrue(any("共 15 个" in text for text in footer))
                self.assertTrue(any("5–15/15" in text for text in footer))
                self.assertTrue(
                    any("Provider-15" in text for _y, _x, text in window.writes)
                )
                self.assertLessEqual(max(y for y, _x, _text in window.writes), 23)

    def test_branded_launcher_keeps_logo_visible_during_selection(self) -> None:
        class FakeWindow:
            def __init__(self) -> None:
                self.writes: list[tuple[int, int, str]] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (24, 100)

            def erase(self) -> None:
                self.writes.clear()

            def addstr(self, y: int, x: int, text: str, _attr: int) -> None:
                self.writes.append((y, x, text))

            def refresh(self) -> None:
                return

        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                cfg = {"providers": {"Alpha": {"hidden": False}}}
                launcher.C = {
                    "pink": 0,
                    "lime": 0,
                    "dim": 0,
                    "warning": 0,
                    "base": 0,
                    "sel": 0,
                }
                launcher._logo_pairs[:] = [0]
                launcher._row_pairs[:] = [0]
                window = FakeWindow()

                launcher._draw_launcher(
                    window,
                    cfg,
                    ["Alpha"],
                    0,
                    False,
                    {},
                    show_brand=True,
                )

                rendered = "\n".join(text for _y, _x, text in window.writes)
                self.assertIn("欢迎回来", rendered)
                self.assertTrue(
                    any(text == "█" for _y, _x, text in window.writes)
                )
                self.assertTrue(
                    any(y == 12 and "Alpha" in text for y, _x, text in window.writes)
                )

    def test_cjk_clipping_never_exceeds_requested_width(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                clipped = launcher._truncate_display("ab渠道-key", 7)
                self.assertLessEqual(launcher._dwidth(clipped), 7)
                self.assertFalse(clipped.endswith("道"))
                row = launcher._compose_row("▸  1  团队-备用", "最近", 24)
                self.assertLessEqual(launcher._dwidth(row), 24)

    def test_intro_returns_interrupting_key_and_animation_can_be_disabled(self) -> None:
        class FakeWindow:
            def __init__(self) -> None:
                self.nodelay_calls: list[bool] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (24, 100)

            def nodelay(self, value: bool) -> None:
                self.nodelay_calls.append(value)

            def erase(self) -> None:
                return

            def addstr(self, *_args) -> None:
                return

            def refresh(self) -> None:
                return

            def getch(self) -> int:
                return ord("2")

        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home), CLAUDE1_NO_ANIMATION="0")
            with loaded_launcher(env) as launcher:
                launcher._logo_pairs[:] = [0]
                window = FakeWindow()
                self.assertLessEqual(launcher.INTRO_DURATION_SECONDS, 0.3)
                self.assertEqual(launcher._intro(window), ord("2"))
                self.assertEqual(window.nodelay_calls, [True, False])
                with mock.patch.dict(
                    os.environ, {"CLAUDE1_NO_ANIMATION": "1"}, clear=False
                ):
                    self.assertFalse(launcher._animation_enabled())

    def test_logo_breathing_never_dims_the_full_logo(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home), CLAUDE1_NO_ANIMATION="0")
            with loaded_launcher(env) as launcher:
                attrs = {
                    launcher._logo_intensity(phase, breathing=True)
                    for phase in range(len(launcher.LOGO_BREATH_LEVELS))
                }

                self.assertNotIn(launcher.curses.A_DIM, attrs)
                self.assertIn(0, attrs)
                self.assertIn(launcher.curses.A_BOLD, attrs)

    def test_intro_key_selects_provider_and_single_color_screen_has_no_timer(self) -> None:
        class FakeWindow:
            def __init__(self) -> None:
                self.timeouts: list[int] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (24, 100)

            def keypad(self, _value: bool) -> None:
                return

            def timeout(self, value: int) -> None:
                self.timeouts.append(value)

            def erase(self) -> None:
                return

            def addstr(self, *_args) -> None:
                return

            def refresh(self) -> None:
                return

            def getch(self) -> int:
                raise AssertionError("intro key should be processed before another read")

        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home), CLAUDE1_NO_ANIMATION="0")
            with loaded_launcher(env) as launcher:
                cfg = {
                    "providers": {
                        "Alpha": {"hidden": False},
                        "Beta": {"hidden": False},
                    }
                }
                window = FakeWindow()
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=ord("2")),
                    mock.patch.object(launcher, "_draw_logo"),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                ):
                    selected = launcher._launcher_main(
                        window, cfg, {"Alpha", "Beta"}
                    )
                self.assertEqual(selected, "Beta")
                self.assertEqual(window.timeouts, [-1])

    def test_intro_is_followed_by_static_branded_launcher(self) -> None:
        class FakeWindow:
            def __init__(self) -> None:
                self.timeouts: list[int] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (24, 100)

            def keypad(self, _value: bool) -> None:
                return

            def timeout(self, value: int) -> None:
                self.timeouts.append(value)

            def getch(self) -> int:
                return ord("q")

            def refresh(self) -> None:
                return

        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home), CLAUDE1_NO_ANIMATION="0")
            with loaded_launcher(env) as launcher:
                cfg = {"providers": {"Alpha": {"hidden": False}}}
                window = FakeWindow()
                launcher._logo_pairs[:] = [1, 2]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "_draw_launcher") as draw_launcher,
                    mock.patch.object(launcher, "_draw_logo") as draw_logo,
                    mock.patch.object(launcher, "load_mru", return_value={}),
                ):
                    selected = launcher._launcher_main(window, cfg, {"Alpha"})

                self.assertIsNone(selected)
                self.assertEqual(window.timeouts, [-1])
                self.assertTrue(draw_launcher.call_args_list)
                self.assertTrue(
                    all(
                        call.kwargs.get("show_brand") is True
                        for call in draw_launcher.call_args_list
                    )
                )
                draw_logo.assert_not_called()

    def test_launcher_session_always_clears_the_tui(self) -> None:
        window = mock.Mock()
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                with mock.patch.object(launcher, "_launcher_main", return_value="Alpha"):
                    self.assertEqual(
                        launcher._launcher_session(window, {}, set()), "Alpha"
                    )
                window.erase.assert_called_once_with()
                window.refresh.assert_called_once_with()

    def test_blocking_launcher_and_confirmation_exit_on_terminal_eof(self) -> None:
        class FakeWindow:
            def __init__(self) -> None:
                self.getch_calls = 0
                self.timeouts: list[int] = []

            def getmaxyx(self) -> tuple[int, int]:
                return (24, 100)

            def keypad(self, _value: bool) -> None:
                return

            def timeout(self, value: int) -> None:
                self.timeouts.append(value)

            def erase(self) -> None:
                return

            def addstr(self, *_args) -> None:
                return

            def refresh(self) -> None:
                return

            def getch(self) -> int:
                self.getch_calls += 1
                return -1

        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home), CLAUDE1_NO_ANIMATION="0")
            with loaded_launcher(env) as launcher:
                cfg = {"providers": {"Alpha": {"hidden": False}}}
                window = FakeWindow()
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                ):
                    selected = launcher._launcher_main(window, cfg, {"Alpha"})

                self.assertIsNone(selected)
                self.assertFalse(launcher._confirm(window, "隐藏 Alpha?"))
                self.assertEqual(window.getch_calls, 2)

    def test_small_terminal_uses_text_fallback_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                self.assertFalse(launcher._tui_size_supported(7, 80))
                self.assertFalse(launcher._tui_size_supported(24, 31))
                self.assertTrue(launcher._tui_size_supported(24, 80))

    def test_help_and_version_do_not_require_cc_switch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(launcher.main(["--help"]), 0)
                    self.assertEqual(launcher.main(["--version"]), 0)
                rendered = output.getvalue()
                self.assertIn("默认启动只影响本次会话", rendered)
                self.assertIn(f"claude1 {launcher.VERSION}", rendered)

    def test_tui_quit_prints_a_compact_farewell(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                output = io.StringIO()
                with (
                    mock.patch.object(
                        launcher, "run_tui_launcher", return_value=("quit", None)
                    ),
                    redirect_stdout(output),
                ):
                    self.assertEqual(launcher.main([]), 0)

                self.assertEqual(
                    output.getvalue(), "Bye，欢迎下次使用 claude1。\n"
                )

    def test_hub_model_option_is_consumed_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                requested, forwarded = launcher._extract_hub_model(
                    ["--model", "Fast,sonnet-next", "-p", "hello"]
                )
                self.assertEqual(requested, "Fast,sonnet-next")
                self.assertEqual(forwarded, ["-p", "hello"])
                self.assertEqual(
                    launcher._normalize_hub_model(
                        "anthropic/Fast,sonnet-next",
                        {"fast": {"models": ["sonnet-next"]}},
                    ),
                    "fast,sonnet-next",
                )
                with self.assertRaisesRegex(RuntimeError, "没有渠道"):
                    launcher._normalize_hub_model(
                        "missing,sonnet",
                        {"fast": {}},
                    )
                with self.assertRaisesRegex(RuntimeError, "只能指定一次"):
                    launcher._extract_hub_model(
                        ["--model=a,one", "--model", "b,two"]
                    )
                backend, hint, parsed_args = launcher.parse_args(
                    ["--hub", "--model", "Fast,sonnet-next", "-p", "hello"]
                )
                self.assertEqual(backend, "hub")
                self.assertIsNone(hint)
                self.assertEqual(
                    parsed_args,
                    ["--model", "Fast,sonnet-next", "-p", "hello"],
                )
                with mock.patch.dict(
                    os.environ,
                    {"CLAUDE1_HUB_START_TIMEOUT": "nan"},
                    clear=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, "1–120"):
                        launcher._hub_start_timeout()

    def test_list_and_doctor_are_local_and_scriptable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            db_path = Path(env["CLAUDE1_DB_PATH"])
            db_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, settings_config TEXT, "
                    "app_type TEXT, sort_index INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, ?, 'claude', ?)",
                    [
                        ("a", "Alpha", '{"env": {}}', 1),
                        ("b", "团队渠道", '{"env": {}}', 2),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            db_path.chmod(0o600)

            fake_claude = home / "bin" / "claude"
            fake_claude.parent.mkdir(parents=True)
            write_executable(fake_claude, "#!/bin/sh\nexit 0\n")
            env["CLAUDE1_CLAUDE_BIN"] = str(fake_claude)

            with loaded_launcher(env) as launcher:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(launcher.main(["list"]), 0)
                    self.assertEqual(launcher.main(["doctor"]), 0)
                rendered = output.getvalue()
                self.assertLess(rendered.index("Alpha"), rendered.index("团队渠道"))
                self.assertIn("本机只读，不连接上游", rendered)
                self.assertIn("发现 2 个 Claude 渠道", rendered)
                self.assertEqual(
                    stat.S_IMODE(Path(env["CLAUDE1_CONFIG_PATH"]).stat().st_mode),
                    0o600,
                )


class LauncherSafetyTests(unittest.TestCase):
    def test_recent_provider_state_is_written_atomically_and_privately(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            mru_path = Path(env["CLAUDE1_MRU_PATH"])

            with loaded_launcher(env) as launcher:
                launcher.record_use("Fixture")

            self.assertIn("Fixture", json.loads(mru_path.read_text(encoding="utf-8")))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(mru_path.stat().st_mode), 0o600)
            leftovers = [
                path.name
                for path in mru_path.parent.iterdir()
                if path.name.startswith(f".{mru_path.name}.")
            ]
            self.assertEqual(leftovers, [])

    @unittest.skipUnless(os.name == "posix", "POSIX file safety")
    def test_runtime_log_rejects_symlinks_and_fifos_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            log_path = Path(env["CLAUDE1_HUB_LOG"])
            log_path.parent.mkdir(parents=True)
            target = log_path.parent / "target.log"
            target.write_text("unchanged", encoding="utf-8")

            with loaded_launcher(env) as launcher:
                log_path.symlink_to(target)
                with self.assertRaises(OSError):
                    launcher._open_private_append(log_path)
                self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")

                log_path.unlink()
                os.mkfifo(log_path)
                started = time.monotonic()
                with self.assertRaises(OSError):
                    launcher._open_private_append(log_path)
                self.assertLess(time.monotonic() - started, 1.0)

    def test_every_runtime_path_can_be_injected_under_a_temporary_home(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(
                Path(raw_home),
                CLAUDE1_GATEWAY_URL="http://127.0.0.1:54321",
            )
            expected = {
                "DB_PATH": "CLAUDE1_DB_PATH",
                "DEFAULT_CLAUDE_BIN": "CLAUDE1_DEFAULT_CLAUDE_BIN",
                "MRU_PATH": "CLAUDE1_MRU_PATH",
                "CONFIG_PATH": "CLAUDE1_CONFIG_PATH",
                "BACKEND_STATE": "CLAUDE1_BACKEND_STATE",
                "BACKEND_STICKY": "CLAUDE1_BACKEND_STICKY",
                "ANYROUTER_OBSERVER": "CLAUDE1_ANYROUTER_OBSERVER",
                "RECLAUDE_ISOLATED": "CLAUDE1_RECLAUDE_BIN",
                "ANYROUTER_SETTINGS": "CLAUDE1_ANYROUTER_SETTINGS",
                "NOTION_MCP": "CLAUDE1_NOTION_MCP",
                "GATEWAY_BIN": "CLAUDE1_GATEWAY_BIN",
                "GATEWAY_CONFIG": "CLAUDE1_GATEWAY_CONFIG",
                "GATEWAY_LOG": "CLAUDE1_GATEWAY_LOG",
                "HUB_SCRIPT": "CLAUDE1_HUB_SCRIPT",
                "HUB_CONFIG": "CLAUDE1_HUB_CONFIG",
                "HUB_DB": "CLAUDE1_HUB_DB",
                "HUB_LOG": "CLAUDE1_HUB_LOG",
                "TEMP_DIR": "CLAUDE1_TMP_DIR",
            }
            with loaded_launcher(env) as launcher:
                for attr, key in expected.items():
                    with self.subTest(path=attr):
                        self.assertEqual(getattr(launcher, attr), Path(env[key]))
                self.assertEqual(
                    launcher.GATEWAY_URL, "http://127.0.0.1:54321"
                )

    def test_regular_launch_records_last_session_without_touching_sticky(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            fake_claude = home / "bin" / "claude"
            fake_claude.parent.mkdir(parents=True)
            write_executable(
                fake_claude,
                """
                #!/usr/bin/env python3
                raise SystemExit(0)
                """,
            )
            env["CLAUDE1_CLAUDE_BIN"] = str(fake_claude)
            sticky = Path(env["CLAUDE1_BACKEND_STICKY"])
            sticky.parent.mkdir(parents=True)
            sticky.write_text("reclaude\n", encoding="utf-8")

            with loaded_launcher(env) as launcher:
                self.assertEqual(launcher.exec_plain_claude("direct", ["-p", "ok"]), 0)

            self.assertEqual(sticky.read_text(encoding="utf-8"), "reclaude\n")
            last_session = json.loads(
                Path(env["CLAUDE1_BACKEND_STATE"]).read_text(encoding="utf-8")
            )
            self.assertEqual(last_session["backend"], "direct")
            self.assertIsInstance(last_session["at"], float)
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE(
                        Path(env["CLAUDE1_BACKEND_STATE"]).stat().st_mode
                    ),
                    0o600,
                )

    def test_explicit_use_is_the_only_operation_that_changes_sticky(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            sticky = Path(env["CLAUDE1_BACKEND_STICKY"])

            with loaded_launcher(env) as launcher:
                self.assertEqual(launcher.set_sticky("hub"), 0)
                launcher.record_backend("provider", "Fixture")

            self.assertEqual(sticky.read_text(encoding="utf-8"), "hub\n")
            self.assertEqual(stat.S_IMODE(sticky.stat().st_mode), 0o600)
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE(
                        Path(env["CLAUDE1_BACKEND_STATE"]).stat().st_mode
                    ),
                    0o600,
                )
            self.assertEqual(
                json.loads(
                    Path(env["CLAUDE1_BACKEND_STATE"]).read_text(encoding="utf-8")
                )["provider"],
                "Fixture",
            )
            leftovers = [
                path.name
                for path in sticky.parent.iterdir()
                if path.name.startswith(f".{sticky.name}.")
            ]
            self.assertEqual(leftovers, [])

    def test_temporary_settings_are_0600_and_removed_after_fake_claude(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            fake_claude = home / "bin" / "claude"
            capture = home / "capture.json"
            fake_claude.parent.mkdir(parents=True)
            Path(env["CLAUDE1_TMP_DIR"]).mkdir(parents=True)
            write_executable(
                fake_claude,
                """
                #!/usr/bin/env python3
                import json
                import os
                import stat
                import sys
                from pathlib import Path

                idx = sys.argv.index("--settings")
                settings_path = Path(sys.argv[idx + 1])
                Path(os.environ["FAKE_CLAUDE_CAPTURE"]).write_text(json.dumps({
                    "argv": sys.argv[1:],
                    "env": dict(os.environ),
                    "mode": stat.S_IMODE(settings_path.stat().st_mode),
                    "settings_path": str(settings_path),
                    "settings": json.loads(settings_path.read_text(encoding="utf-8")),
                }), encoding="utf-8")
                raise SystemExit(0)
                """,
            )
            env.update(
                CLAUDE1_CLAUDE_BIN=str(fake_claude),
                FAKE_CLAUDE_CAPTURE=str(capture),
                ANTHROPIC_BASE_URL="https://reclaude.invalid",
                ANTHROPIC_AUTH_TOKEN="must-not-leak",
                HTTP_PROXY="http://must-not-leak.invalid",
                https_proxy="http://must-not-leak.invalid",
                CLAUDE_CONFIG_DIR=str(home / "reclaude-config"),
                CLAUDE_CODE_PARENT_SESSION_ID="must-not-leak",
                RECLAUDE_SESSION_ID="must-not-leak",
                CLAUDE_HUB_LOCAL_TOKEN="must-not-leak",
                CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN="1",
            )
            settings = {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://fixture.invalid",
                    "ANTHROPIC_AUTH_TOKEN": "fixture-secret",
                }
            }

            with loaded_launcher(env) as launcher:
                self.assertEqual(
                    launcher.launch_with_settings(settings, ["-p", "hello"]),
                    0,
                )

            observed = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(observed["mode"], 0o600)
            self.assertEqual(observed["settings"], settings)
            self.assertNotIn("fixture-secret", observed["argv"])
            self.assertFalse(Path(observed["settings_path"]).exists())
            child_env = observed["env"]
            self.assertEqual(
                child_env["ANTHROPIC_BASE_URL"], "https://fixture.invalid"
            )
            self.assertEqual(
                child_env["ANTHROPIC_AUTH_TOKEN"], "fixture-secret"
            )
            for key in (
                "HTTP_PROXY",
                "https_proxy",
                "CLAUDE_CONFIG_DIR",
                "CLAUDE_CODE_PARENT_SESSION_ID",
                "RECLAUDE_SESSION_ID",
                "CLAUDE_HUB_LOCAL_TOKEN",
            ):
                self.assertNotIn(key, child_env)
            self.assertEqual(
                child_env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"], "1"
            )

    def test_hub_health_requires_exact_service_and_protocol_contract(self) -> None:
        valid = {
            "ok": True,
            "service": "claude-hub",
            "protocol": 1,
            "version": "99.42.7",
        }
        cases = [
            (503, valid, False),
            (200, {}, False),
            (200, {**valid, "ok": 1}, False),
            (200, {**valid, "service": "other-service"}, False),
            (200, {**valid, "protocol": 2}, False),
            (200, valid, True),
        ]
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                for status_code, body, expected in cases:
                    with self.subTest(status_code=status_code, body=body):
                        payload = json.dumps(body).encode("utf-8")
                        with health_server(status_code, payload) as port:
                            self.assertIs(launcher.hub_healthy(port), expected)
                with health_server(200, b"not-json") as port:
                    self.assertFalse(launcher.hub_healthy(port))

    def test_ensure_hub_starts_with_a_scrubbed_whitelist_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            port = free_local_port()
            env = isolated_env(
                home,
                CLAUDE1_HUB_PORT=str(port),
                ANTHROPIC_AUTH_TOKEN="must-not-leak",
                ANTHROPIC_API_KEY="must-not-leak",
                HTTP_PROXY="http://must-not-leak.invalid",
                HTTPS_PROXY="http://must-not-leak.invalid",
                ALL_PROXY="socks5://must-not-leak.invalid",
                TZ="Secret/Timezone",
                CLAUDE_CODE_CHILD_SESSION="must-not-leak",
                CLAUDE_CODE_WORKFLOWS="must-not-leak",
                UNRELATED_SECRET="must-not-leak",
                CLAUDE_HUB_LOCAL_TOKEN="fixture-local-token",
                CLAUDE1_HUB_START_TIMEOUT="5",
            )
            fake_hub = Path(env["CLAUDE1_HUB_SCRIPT"])
            capture = Path(env["CLAUDE1_HUB_CONFIG"])
            fake_hub.parent.mkdir(parents=True)
            capture.parent.mkdir(parents=True)
            write_executable(
                fake_hub,
                """
                #!/usr/bin/env python3
                import json
                import os
                from http.server import BaseHTTPRequestHandler
                from pathlib import Path
                from socketserver import TCPServer

                Path(os.environ["CLAUDE_HUB_CONFIG"]).write_text(
                    json.dumps(dict(os.environ)), encoding="utf-8"
                )
                body = json.dumps({
                    "ok": True,
                    "service": "claude-hub",
                    "protocol": 1,
                    "version": "0.1.0",
                }).encode("utf-8")

                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    def log_message(self, _format, *_args):
                        return

                server = TCPServer(
                    ("127.0.0.1", int(os.environ["CLAUDE_HUB_PORT"])), Handler
                )
                server.serve_forever()
                """,
            )

            with loaded_launcher(env) as launcher:
                try:
                    launcher.ensure_hub(port)
                    child_env = json.loads(capture.read_text(encoding="utf-8"))
                finally:
                    for process in launcher._hub_processes:
                        if process.poll() is None:
                            process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)

            for key in (
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_API_KEY",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "TZ",
                "CLAUDE_CODE_CHILD_SESSION",
                "CLAUDE_CODE_WORKFLOWS",
                "UNRELATED_SECRET",
            ):
                self.assertNotIn(key, child_env)
            self.assertEqual(child_env["CLAUDE_HUB_LOCAL_TOKEN"], "fixture-local-token")
            self.assertEqual(child_env["CLAUDE_HUB_CONFIG"], str(capture))
            self.assertEqual(
                child_env["CLAUDE_HUB_DB"], env["CLAUDE1_HUB_DB"]
            )
            self.assertEqual(child_env["CLAUDE_HUB_LOG"], env["CLAUDE1_HUB_LOG"])
            self.assertEqual(child_env["CLAUDE_HUB_PORT"], str(port))
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE(Path(env["CLAUDE1_HUB_LOG"]).stat().st_mode),
                    0o600,
                )

    def test_hub_supports_environment_backed_local_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            valid_health = json.dumps(
                {
                    "ok": True,
                    "service": "claude-hub",
                    "protocol": 1,
                    "version": "0.1.0",
                }
            ).encode("utf-8")
            with health_server(200, valid_health) as port:
                env = isolated_env(
                    home,
                    CLAUDE1_HUB_PORT=str(port),
                    CLAUDE_HUB_LOCAL_TOKEN="fixture-local-token",
                )
                config = Path(env["CLAUDE1_HUB_CONFIG"])
                config.parent.mkdir(parents=True)
                config.write_text(
                    json.dumps(
                        {
                            "port": 18787,
                            "default_channel": "glm",
                            "channels": {
                                "glm": {
                                    "provider": "Fixture",
                                    "models": ["glm-fixture"],
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                fake_claude = home / "bin" / "claude"
                capture = home / "capture.json"
                fake_claude.parent.mkdir(parents=True)
                write_executable(
                    fake_claude,
                    """
                    #!/usr/bin/env python3
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    path = Path(sys.argv[sys.argv.index("--settings") + 1])
                    Path(os.environ["FAKE_CLAUDE_CAPTURE"]).write_text(
                        json.dumps(json.loads(path.read_text(encoding="utf-8"))),
                        encoding="utf-8",
                    )
                    """,
                )
                sticky = Path(env["CLAUDE1_BACKEND_STICKY"])
                sticky.parent.mkdir(parents=True)
                sticky.write_text("reclaude\n", encoding="utf-8")
                env.update(
                    CLAUDE1_CLAUDE_BIN=str(fake_claude),
                    FAKE_CLAUDE_CAPTURE=str(capture),
                )

                with loaded_launcher(env) as launcher:
                    self.assertEqual(launcher.exec_hub(["-p", "hello"]), 0)

                    settings = json.loads(capture.read_text(encoding="utf-8"))
                    self.assertEqual(
                        settings["env"]["ANTHROPIC_BASE_URL"],
                        f"http://127.0.0.1:{port}",
                    )
                    self.assertEqual(
                        settings["env"]["ANTHROPIC_AUTH_TOKEN"],
                        "fixture-local-token",
                    )
                    self.assertEqual(
                        settings["env"]["ANTHROPIC_MODEL"],
                        "glm,glm-fixture",
                    )

                    # Legacy live configs with local_token continue to work when
                    # the safer environment secret is not supplied.
                    hub_cfg = json.loads(config.read_text(encoding="utf-8"))
                    hub_cfg["local_token"] = "legacy-config-token"
                    config.write_text(json.dumps(hub_cfg), encoding="utf-8")
                    os.environ["CLAUDE_HUB_LOCAL_TOKEN"] = ""
                    self.assertEqual(launcher.exec_hub([]), 0)
                    legacy_settings = json.loads(
                        capture.read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        legacy_settings["env"]["ANTHROPIC_AUTH_TOKEN"],
                        "legacy-config-token",
                    )
                self.assertEqual(sticky.read_text(encoding="utf-8"), "reclaude\n")

    def test_hub_fails_before_start_when_no_local_token_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home, CLAUDE_HUB_LOCAL_TOKEN="")
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "port": 18787,
                        "default_channel": "glm",
                        "channels": {
                            "glm": {
                                "provider": "Fixture",
                                "models": ["glm-fixture"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with loaded_launcher(env) as launcher:
                with self.assertRaisesRegex(RuntimeError, "hub 本地凭证缺失"):
                    launcher.exec_hub([])


class ScriptedWindow:
    """A minimal curses window that replays a fixed key sequence."""

    def __init__(self, keys, size=(30, 120)) -> None:
        self._keys = list(keys)
        self._size = size
        self.timeouts: list[int] = []

    def getmaxyx(self):
        return self._size

    def keypad(self, _value):
        return

    def timeout(self, value):
        self.timeouts.append(value)

    def nodelay(self, _value):
        return

    def erase(self):
        return

    def addstr(self, *_args):
        return

    def refresh(self):
        return

    def getch(self):
        return self._keys.pop(0) if self._keys else -1


HUB_FIXTURE = {
    "port": 18787,
    "default_channel": "glm",
    "channels": {
        "glm": {"provider": "GLM Provider", "models": ["glm-5.2"]},
        "gpt": {
            "provider": "GPT Provider",
            "models": ["gpt-5.6-sol", "gpt-5.6-luna"],
            "proxy": "http://127.0.0.1:7890",
        },
        "any": {
            "provider": "Any",
            "models": ["claude-fable-5[1m]", "claude-opus-4-6"],
        },
        "grok": {"provider": "Grok", "models": ["grok-4.5"]},
    },
}


class HubWorkspaceTests(unittest.TestCase):
    def _hub_env(self, home: Path, write_config: bool = True) -> dict[str, str]:
        env = isolated_env(home, CLAUDE1_NO_ANIMATION="1")
        if write_config:
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps(HUB_FIXTURE), encoding="utf-8")
        return env

    def test_build_hub_view_orders_default_first_and_derives_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                status, options = launcher.build_hub_view(HUB_FIXTURE)
                self.assertEqual(status.channel_count, 4)
                self.assertEqual(status.model_count, 6)
                self.assertEqual(status.default_channel, "glm")
                self.assertEqual(status.default_model, "glm-5.2")
                self.assertEqual(status.summary, "4 渠道 · 6 模型")

                self.assertTrue(options[0].is_default)
                self.assertEqual(options[0].selector, "glm,glm-5.2")
                self.assertEqual(options[0].status_label, "默认")
                self.assertEqual(len(options), 6)

                by_selector = {opt.selector: opt for opt in options}
                self.assertEqual(
                    by_selector["gpt,gpt-5.6-sol"].family, "GPT / Codex"
                )
                self.assertEqual(by_selector["gpt,gpt-5.6-sol"].status_label, "代理")
                self.assertEqual(by_selector["any,claude-opus-4-6"].family, "Claude")
                self.assertEqual(by_selector["grok,grok-4.5"].family, "Grok")
                self.assertTrue(by_selector["any,claude-fable-5[1m]"].is_1m)
                self.assertEqual(
                    by_selector["any,claude-fable-5[1m]"].status_label, "1M"
                )

    def test_hub_model_family_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                self.assertEqual(launcher._hub_model_family("kimi-k3"), "Kimi")
                self.assertEqual(
                    launcher._hub_model_family("deepseek-v4-pro"), "DeepSeek"
                )
                self.assertEqual(launcher._hub_model_family("Xiaomi-MiMo"), "MiMo")
                self.assertEqual(
                    launcher._hub_model_family("codex-mini"), "GPT / Codex"
                )
                self.assertEqual(launcher._hub_model_family("glm-5.2"), "GLM")
                self.assertEqual(launcher._hub_model_family("mystery-1"), "其他")

    def test_load_hub_view_absent_then_present(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home), write_config=False)
            with loaded_launcher(env) as launcher:
                self.assertIsNone(launcher._load_hub_view())
                config = Path(env["CLAUDE1_HUB_CONFIG"])
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(json.dumps(HUB_FIXTURE), encoding="utf-8")
                view = launcher._load_hub_view()
                self.assertIsNotNone(view)
                _status, options = view
                self.assertEqual(len(options), 6)

    def _run_home(self, launcher, keys):
        cfg = {"providers": {"Alpha": {"hidden": False}}}
        window = ScriptedWindow(keys)
        launcher._logo_pairs[:] = [0]
        with (
            mock.patch.object(launcher, "_init_colors", return_value={}),
            mock.patch.object(launcher, "_intro", return_value=None),
            mock.patch.object(launcher, "load_mru", return_value={}),
            mock.patch.object(launcher, "hub_healthy", return_value=True),
        ):
            return launcher._launcher_main(window, cfg, {"Alpha"})

    def test_enter_opens_hub_and_launches_default_model(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                # Enter (home -> hub workspace), Enter (launch default).
                result = self._run_home(launcher, [10, 10])
                self.assertIsInstance(result, launcher.HubLaunch)
                self.assertEqual(result.option.selector, "glm,glm-5.2")

    def test_hub_workspace_down_then_enter_launches_second_model(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                # Enter -> hub, j (down), Enter -> launch second option.
                result = self._run_home(launcher, [10, ord("j"), 10])
                self.assertIsInstance(result, launcher.HubLaunch)
                self.assertEqual(result.option.selector, "gpt,gpt-5.6-sol")

    def test_hub_workspace_esc_returns_home_then_quit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                # Enter -> hub, Esc -> home, q -> quit launcher.
                result = self._run_home(launcher, [10, 27, ord("q")])
                self.assertIsNone(result)

    def test_home_has_no_hub_entry_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home), write_config=False)
            with loaded_launcher(env) as launcher:
                # No hub config: digit still selects the provider directly.
                result = self._run_home(launcher, [ord("1")])
                self.assertEqual(result, "Alpha")

    def test_main_launches_hub_backend_from_tui_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(
                        launcher,
                        "run_tui_launcher",
                        return_value=("hub", "gpt,gpt-5.6-sol"),
                    ),
                    mock.patch.object(
                        launcher, "exec_hub", return_value=0
                    ) as exec_hub,
                ):
                    self.assertEqual(launcher.main([]), 0)
                exec_hub.assert_called_once_with(["--model", "gpt,gpt-5.6-sol"])

    def test_run_tui_launcher_maps_hub_launch_to_hub_action(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                option = launcher.HubModelOption(
                    family="GLM",
                    channel="glm",
                    model="glm-5.2",
                    is_default=True,
                    via_proxy=False,
                    is_1m=False,
                )
                with (
                    mock.patch.object(launcher, "db_claude_rows", return_value=[]),
                    mock.patch.object(
                        launcher,
                        "load_config",
                        return_value={"version": 2, "providers": {}},
                    ),
                    mock.patch.object(launcher.sys.stdin, "isatty", return_value=True),
                    mock.patch.object(
                        launcher.sys.stdout, "isatty", return_value=True
                    ),
                    mock.patch.object(
                        launcher.shutil,
                        "get_terminal_size",
                        return_value=os.terminal_size((120, 40)),
                    ),
                    mock.patch.object(
                        launcher.curses,
                        "wrapper",
                        return_value=launcher.HubLaunch(option),
                    ),
                ):
                    self.assertEqual(
                        launcher.run_tui_launcher(), ("hub", "glm,glm-5.2")
                    )


if __name__ == "__main__":
    unittest.main()
