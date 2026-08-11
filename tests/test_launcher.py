from __future__ import annotations

import importlib.util
import errno
import hmac
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
from contextlib import contextmanager, redirect_stderr, redirect_stdout
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
        "CLAUDE1_ACCOUNT_POOL_CONFIG": str(state / "account-pools.json"),
        "CLAUDE1_ACCOUNT_POOL_STATE": str(state / "account-state.sqlite3"),
        "CLAUDE1_BACKEND_STATE": str(state / "last-session.json"),
        "CLAUDE1_BACKEND_STICKY": str(state / "sticky"),
        "CLAUDE1_ANYROUTER_OBSERVER": str(home / "bin" / "observer"),
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
def health_server(
    status_code: int,
    payload: bytes,
    *,
    ready_token: str | None = None,
    ready_instance_id: str | None = None,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            body = payload
            if self.path == "/readyz" and ready_token is not None:
                challenge = self.headers.get("X-Claude-Hub-Challenge", "")
                decoded = json.loads(payload)
                proof_version = (
                    f"v2:{ready_instance_id}:{self.server.server_address[1]}"
                    if ready_instance_id is not None
                    else f"v1:{self.server.server_address[1]}"
                )
                proof_message = f"claude-hub-ready:{proof_version}:{challenge}".encode(
                    "ascii"
                )
                decoded["proof"] = hmac.digest(
                    ready_token.encode("utf-8"),
                    proof_message,
                    "sha256",
                ).hex()
                body = json.dumps(decoded).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
                db_order = [
                    {"id": "a", "name": "Third Party A"},
                    {"id": "b", "name": "团队渠道"},
                    {"id": "c", "name": "Third Party B"},
                ]

                self.assertTrue(launcher.sync_config(cfg, db_order))
                self.assertEqual(list(cfg["providers"]), ["a", "b", "c"])
                self.assertEqual(cfg["version"], 3)
                self.assertTrue(
                    all(
                        meta["hidden"] is False
                        for meta in cfg["providers"].values()
                    )
                )

    def test_mru_sets_cursor_and_recent_badge_without_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                cfg = {
                    "providers": {
                        "a": {"name": "Alpha", "hidden": False},
                        "b": {"name": "Beta", "hidden": False},
                        "c": {"name": "Gamma", "hidden": False},
                    }
                }
                mru = {"a": 10.0, "c": 30.0, "b": 20.0}

                view = launcher._build_view(
                    cfg, {"a", "b", "c"}, mru, False
                )

                self.assertEqual(view, ["a", "b", "c"])
                self.assertEqual(launcher._recent_name(view, mru), "c")
                self.assertEqual(launcher._initial_index(view, mru), 2)

    def test_alias_matching_and_casefolded_conflict_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                providers = [
                    {"id": "a", "name": "Alpha Gateway", "alias": "Fast"},
                    {"id": "b", "name": "Beta Gateway", "alias": "Safe"},
                ]
                matches, exact = launcher.match_providers(providers, "fAsT")
                self.assertTrue(exact)
                self.assertEqual(
                    [provider["name"] for provider in matches],
                    ["Alpha Gateway"],
                )

                meta = {
                    "a": {"name": "Alpha Gateway", "alias": "Fast"},
                    "b": {"name": "Beta Gateway", "alias": "Safe"},
                    "c": {"name": "Codex"},
                }
                changed, message = launcher._set_alias(meta, "c", "FAST")
                self.assertFalse(changed)
                self.assertIn("Alpha Gateway", message)
                changed, message = launcher._set_alias(
                    meta, "c", "bEtA GaTeWaY"
                )
                self.assertFalse(changed)
                self.assertIn("Beta Gateway", message)
                self.assertNotIn("alias", meta["c"])
                changed, message = launcher._set_alias(meta, "c", "hub")
                self.assertFalse(changed)
                self.assertIn("保留命令", message)
                changed, message = launcher._set_alias(meta, "c", "--help")
                self.assertFalse(changed)
                self.assertIn("命令参数", message)

    def test_duplicate_names_migrate_to_stable_ids_without_collapsing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                cfg = {
                    "version": 2,
                    "providers": {
                        "Duplicated": {"hidden": False, "alias": "primary"}
                    },
                }
                providers = [
                    {"id": "first-id", "name": "Duplicated"},
                    {"id": "second-id", "name": "Duplicated"},
                ]

                self.assertTrue(launcher.sync_config(cfg, providers))
                self.assertEqual(
                    list(cfg["providers"]),
                    ["first-id", "second-id"],
                )
                self.assertEqual(cfg["providers"]["first-id"]["alias"], "primary")
                self.assertNotIn("alias", cfg["providers"]["second-id"])
                self.assertEqual(
                    launcher._provider_meta_label(cfg["providers"], "first-id"),
                    "Duplicated [first-id]",
                )
                with self.assertRaisesRegex(RuntimeError, "存在冲突"):
                    launcher.choose(providers, "Duplicated")
                self.assertEqual(
                    launcher.choose(providers, "id:second-id")["id"],
                    "second-id",
                )

    def test_exact_legacy_alias_collision_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                providers = [
                    {"id": "a", "name": "Alpha", "alias": "same"},
                    {"id": "b", "name": "Beta", "alias": "SAME"},
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
                with self.assertRaisesRegex(RuntimeError, "没有模型"):
                    launcher._normalize_hub_model(
                        "fast,unknown",
                        {"fast": {"models": ["sonnet-next"]}},
                    )
                with self.assertRaisesRegex(RuntimeError, "只能指定一次"):
                    launcher._extract_hub_model(
                        ["--model=a,one", "--model", "b,two"]
                    )
                with self.assertRaisesRegex(RuntimeError, "--model 后需要"):
                    launcher._extract_hub_model(
                        ["--model", "--slot", "haiku"]
                    )
                with self.assertRaisesRegex(RuntimeError, "--slot 后需要"):
                    launcher._extract_hub_slot(
                        ["--slot", "--model", "fast,sonnet-next"]
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

                requested, forwarded = launcher._extract_hub_model(
                    ["-p", "hello", "--", "--model", "not-for-hub"]
                )
                self.assertIsNone(requested)
                self.assertEqual(
                    forwarded, ["-p", "hello", "--", "--model", "not-for-hub"]
                )

    def test_conflicting_backend_selectors_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                for argv in (
                    ["--hub", "--direct"],
                    ["anyrouter", "--current"],
                    ["--direct", "hub"],
                ):
                    with self.subTest(argv=argv), self.assertRaisesRegex(
                        RuntimeError, "不能同时指定后端"
                    ):
                        launcher.parse_args(argv)

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
                        (
                            "a",
                            "Alpha",
                            json.dumps(
                                {
                                    "env": {
                                        "CLAUDE_CODE_SUBAGENT_MODEL": "pinned-model"
                                    }
                                }
                            ),
                            1,
                        ),
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
                self.assertIn("1 个 provider 固定了子代理模型", rendered)
                self.assertIn("claude1 doctor --fix", rendered)
                self.assertEqual(
                    stat.S_IMODE(Path(env["CLAUDE1_CONFIG_PATH"]).stat().st_mode),
                    0o600,
                )
            with sqlite3.connect(db_path) as connection:
                raw = connection.execute(
                    "SELECT settings_config FROM providers WHERE id = 'a'"
                ).fetchone()[0]
            self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL", raw)

    def test_doctor_reports_bad_provider_settings_and_continues_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            db_path = Path(env["CLAUDE1_DB_PATH"])
            db_path.parent.mkdir(parents=True)
            pinned = json.dumps(
                {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "pinned-model"}}
            )
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, settings_config TEXT, "
                    "app_type TEXT, sort_index INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, ?, 'claude', ?)",
                    [
                        ("broken", "Broken Provider", "{", 1),
                        (
                            "deep",
                            "Deep Provider",
                            "[" * 10_000 + "0" + "]" * 10_000,
                            2,
                        ),
                        ("valid", "Valid Provider", pinned, 3),
                    ],
                )
            db_path.chmod(0o600)
            fake_claude = home / "bin" / "claude"
            fake_claude.parent.mkdir(parents=True)
            write_executable(fake_claude, "#!/bin/sh\nexit 0\n")
            env["CLAUDE1_CLAUDE_BIN"] = str(fake_claude)
            output = io.StringIO()

            with loaded_launcher(env) as launcher, redirect_stdout(output):
                status = launcher.main(["doctor"])

            rendered = output.getvalue()
            self.assertEqual(status, 1)
            self.assertIn("Broken Provider", rendered)
            self.assertIn("Deep Provider", rendered)
            self.assertIn("settings_config 无效", rendered)
            self.assertIn("1 个 provider 固定了子代理模型", rendered)

    def test_doctor_fix_backs_up_and_cleans_every_provider_type(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            db_path = Path(env["CLAUDE1_DB_PATH"])
            db_path.parent.mkdir(parents=True)
            dirty = json.dumps(
                {
                    "env": {
                        "ANTHROPIC_MODEL": "fixture-model",
                        "CLAUDE_CODE_SUBAGENT_MODEL": "pinned-model",
                    }
                }
            )
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, settings_config TEXT, "
                    "app_type TEXT, sort_index INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?)",
                    [
                        ("claude", "Claude Fixture", dirty, "claude", 1),
                        ("desktop", "Desktop Fixture", dirty, "claude-desktop", 2),
                        ("clean", "Clean Fixture", '{"env": {}}', "claude", 3),
                    ],
                )
            db_path.chmod(0o600)
            fake_claude = home / "bin" / "claude"
            fake_claude.parent.mkdir(parents=True)
            write_executable(fake_claude, "#!/bin/sh\nexit 0\n")
            env["CLAUDE1_CLAUDE_BIN"] = str(fake_claude)

            output = io.StringIO()
            with loaded_launcher(env) as launcher, redirect_stdout(output):
                self.assertEqual(launcher.main(["doctor", "--fix"]), 0)

            backups = list(db_path.parent.glob("cc-switch.db.bak-doctor-fix-*"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(db_path) as connection:
                live_settings = [
                    json.loads(row[0])
                    for row in connection.execute(
                        "SELECT settings_config FROM providers ORDER BY id"
                    )
                ]
            self.assertTrue(
                all(
                    "CLAUDE_CODE_SUBAGENT_MODEL" not in settings.get("env", {})
                    for settings in live_settings
                )
            )
            with sqlite3.connect(backups[0]) as connection:
                backup_dirty_count = connection.execute(
                    "SELECT COUNT(*) FROM providers "
                    "WHERE settings_config LIKE '%CLAUDE_CODE_SUBAGENT_MODEL%'"
                ).fetchone()[0]
            self.assertEqual(backup_dirty_count, 2)
            rendered = output.getvalue()
            self.assertIn("Claude Fixture", rendered)
            self.assertIn("Desktop Fixture", rendered)
            self.assertIn(str(backups[0]), rendered)

    def test_doctor_fix_skips_bad_settings_and_repairs_later_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            db_path = Path(env["CLAUDE1_DB_PATH"])
            db_path.parent.mkdir(parents=True)
            dirty = json.dumps(
                {"env": {"CLAUDE_CODE_SUBAGENT_MODEL": "pinned-model"}}
            )
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, settings_config TEXT, "
                    "app_type TEXT, sort_index INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, ?, 'claude', ?)",
                    [
                        ("first", "First Valid", dirty, 1),
                        ("broken", "Broken Provider", "{", 2),
                        ("last", "Last Valid", dirty, 3),
                    ],
                )
            db_path.chmod(0o600)
            fake_claude = home / "bin" / "claude"
            fake_claude.parent.mkdir(parents=True)
            write_executable(fake_claude, "#!/bin/sh\nexit 0\n")
            env["CLAUDE1_CLAUDE_BIN"] = str(fake_claude)
            output = io.StringIO()

            with loaded_launcher(env) as launcher, redirect_stdout(output):
                status = launcher.main(["doctor", "--fix"])

            with sqlite3.connect(db_path) as connection:
                rows = dict(
                    connection.execute(
                        "SELECT id, settings_config FROM providers ORDER BY sort_index"
                    )
                )
            self.assertEqual(status, 1)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", rows["first"])
            self.assertEqual(rows["broken"], "{")
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", rows["last"])
            self.assertIn("Broken Provider", output.getvalue())
            self.assertEqual(
                len(list(db_path.parent.glob("cc-switch.db.bak-doctor-fix-*"))),
                1,
            )

    def test_current_provider_uses_unique_db_marker_and_main_launches_that_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            rows = [
                {
                    "id": "old",
                    "name": "Old",
                    "settings_config": '{"env": {}}',
                    "is_current": 0,
                },
                {
                    "id": "live",
                    "name": "Live",
                    "settings_config": '{"env": {}}',
                    "is_current": 1,
                },
            ]
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(
                        launcher, "db_claude_rows", return_value=rows
                    ),
                    mock.patch.object(
                        launcher, "launch_provider", return_value=0
                    ) as launch_provider,
                ):
                    self.assertEqual(launcher.main(["current", "-p", "ok"]), 0)

                selected = launch_provider.call_args.args[0]
                self.assertEqual(selected["id"], "live")
                self.assertEqual(launch_provider.call_args.args[1], ["-p", "ok"])
                self.assertEqual(
                    launch_provider.call_args.kwargs["backend_kind"],
                    "current",
                )

    def test_current_provider_fails_closed_for_zero_or_multiple_db_markers(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            base = {
                "name": "Fixture",
                "settings_config": '{"env": {}}',
            }
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher,
                    "db_claude_rows",
                    return_value=[{**base, "id": "none", "is_current": 0}],
                ):
                    with self.assertRaisesRegex(RuntimeError, "没有 is_current=1"):
                        launcher.current_provider()
                with mock.patch.object(
                    launcher,
                    "db_claude_rows",
                    return_value=[
                        {**base, "id": "one", "is_current": 1},
                        {**base, "id": "two", "is_current": 1},
                    ],
                ):
                    with self.assertRaisesRegex(RuntimeError, "多个 is_current=1"):
                        launcher.current_provider()


class LauncherSafetyTests(unittest.TestCase):
    def test_usage_loader_reads_rotated_backup_and_skips_malformed_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            with loaded_launcher(isolated_env(home)) as launcher:
                usage = home / "logs" / "usage.jsonl"
                usage.parent.mkdir(parents=True)
                rotated = usage.with_name(usage.name + ".1")
                rotated.write_text(
                    '\n'.join(
                        (
                            json.dumps({"ts": 100, "in": 1}),
                            json.dumps({"ts": "corrupt", "in": 999}),
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                usage.write_text(
                    json.dumps({"ts": 200, "in": 2}) + "\n",
                    encoding="utf-8",
                )

                rows = launcher._load_usage_rows(usage, 50)

        self.assertEqual([row["in"] for row in rows], [1, 2])

    @unittest.skipUnless(os.name == "posix", "POSIX special-file safety")
    def test_usage_loader_ignores_fifo_and_symlink_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            with loaded_launcher(isolated_env(home)) as launcher:
                usage = home / "usage.jsonl"
                os.mkfifo(usage)
                self.assertEqual(launcher._load_usage_rows(usage, 0), [])

                usage.unlink()
                target = home / "other.jsonl"
                target.write_text(json.dumps({"ts": 100, "in": 9}), encoding="utf-8")
                usage.symlink_to(target)
                self.assertEqual(launcher._load_usage_rows(usage, 0), [])

    def test_claude_interrupt_is_forwarded_to_its_session_without_killing_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                proc = mock.Mock(pid=4242)
                proc.wait.side_effect = [KeyboardInterrupt(), 17]
                proc.poll.return_value = None
                with (
                    mock.patch.object(launcher.subprocess, "Popen", return_value=proc) as popen,
                    mock.patch.object(launcher.os, "killpg") as killpg,
                ):
                    self.assertEqual(launcher._run_claude(["claude"], env={}), 17)

        popen.assert_called_once_with(
            ["claude"], env={}, start_new_session=(os.name == "posix")
        )
        if os.name == "posix":
            killpg.assert_called_once_with(4242, launcher.signal.SIGINT)
        else:
            proc.send_signal.assert_called_once_with(launcher.signal.SIGINT)

    def test_direct_preserves_explicitly_inherited_claude_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(
                Path(raw_home),
                CLAUDE1_CLAUDE_BIN="/fixture/claude",
                ANTHROPIC_AUTH_TOKEN="inherited-token",
                ANTHROPIC_BASE_URL="https://inherited.example",
            )
            with loaded_launcher(env) as launcher:
                with mock.patch.object(launcher, "_run_claude", return_value=0) as run:
                    self.assertEqual(launcher.exec_plain_claude("direct", ["-p", "hi"]), 0)

        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["ANTHROPIC_AUTH_TOKEN"], "inherited-token")
        self.assertEqual(child_env["ANTHROPIC_BASE_URL"], "https://inherited.example")

    def test_parse_args_preserves_double_dash_and_rejects_lost_provider_hint(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                self.assertEqual(
                    launcher.parse_args(["direct", "--", "--current", "--hub"]),
                    ("direct", None, ["--", "--current", "--hub"]),
                )
                with self.assertRaisesRegex(RuntimeError, "provider 与 --hub"):
                    launcher.parse_args(["DeepSeek", "--hub"])
                with self.assertRaisesRegex(
                    RuntimeError, "--hub 后的位置参数"
                ):
                    launcher.parse_args(["--hub", "DeepSeek"])
                self.assertEqual(
                    launcher.parse_args(["--hub", "--", "hello"]),
                    ("hub", None, ["--", "hello"]),
                )

    def test_hub_identity_colors_and_chart_coordinates_wrap_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                colors = launcher._HUB_IDENTITY_COLORS
                self.assertEqual(
                    launcher._hub_identity_color(len(colors)), colors[0]
                )
                self.assertEqual(launcher._hub_identity_color(-1), colors[-1])
                self.assertEqual(launcher._scale_chart_index(0, 7, 28), 0)
                self.assertEqual(launcher._scale_chart_index(6, 7, 28), 27)

    def test_missing_notion_config_is_not_forwarded_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            stderr = io.StringIO()
            with loaded_launcher(env) as launcher, redirect_stderr(stderr):
                backend, hint, forwarded = launcher.parse_args(["--notion", "--", "-p", "hi"])

        self.assertIsNone(backend)
        self.assertIsNone(hint)
        self.assertEqual(forwarded, ["--", "-p", "hi"])
        self.assertIn("notion 配置不存在", stderr.getvalue())

    def test_reserved_provider_name_requires_an_explicit_backend_or_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            db_path = Path(env["CLAUDE1_DB_PATH"])
            db_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, settings_config TEXT, app_type TEXT, sort_index INTEGER)"
                )
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, 'claude', 1)",
                    ("provider-hub", "hub", '{"env": {}}'),
                )
                connection.commit()
            finally:
                connection.close()
            with loaded_launcher(env) as launcher:
                with self.assertRaisesRegex(RuntimeError, "名称“hub”与 claude1 后端命令冲突"):
                    launcher.main(["hub"])
                self.assertEqual(launcher.parse_args(["--hub"])[0], "hub")

    def test_gateway_missing_binary_and_doctor_report_a_clear_action(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            db_path = Path(env["CLAUDE1_DB_PATH"])
            db_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, settings_config TEXT, app_type TEXT, sort_index INTEGER)"
                )
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, 'claude', 1)",
                    (
                        "gateway-provider",
                        "Gateway",
                        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18317"}}),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            with loaded_launcher(env) as launcher:
                with mock.patch.object(launcher, "gateway_healthy", return_value=False):
                    with self.assertRaisesRegex(RuntimeError, "CLAUDE1_GATEWAY_BIN"):
                        launcher.ensure_local_gateway("http://127.0.0.1:18317")
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(launcher.cli_doctor(), 1)

        self.assertIn("有渠道需要本地网关", output.getvalue())
        self.assertIn("CLAUDE1_GATEWAY_BIN", output.getvalue())

    def test_gateway_log_is_created_privately(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            gateway = Path(env["CLAUDE1_GATEWAY_BIN"])
            gateway.parent.mkdir(parents=True)
            write_executable(gateway, "#!/bin/sh\nexit 0\n")
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(launcher, "gateway_healthy", side_effect=[False, True]),
                    mock.patch.object(launcher.subprocess, "Popen"),
                ):
                    launcher.ensure_local_gateway("http://127.0.0.1:18317")
                if os.name == "posix":
                    self.assertEqual(
                        stat.S_IMODE(Path(env["CLAUDE1_GATEWAY_LOG"]).stat().st_mode),
                        0o600,
                    )

    def test_hub_start_is_serialized_between_concurrent_launches(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            port = free_local_port()
            env = isolated_env(home, CLAUDE1_HUB_START_TIMEOUT="5")
            fake_hub = Path(env["CLAUDE1_HUB_SCRIPT"])
            fake_hub.parent.mkdir(parents=True)
            write_executable(
                fake_hub,
                """
                #!/usr/bin/env python3
                import hmac
                import json
                import os
                import socket
                from http.server import BaseHTTPRequestHandler
                from socketserver import TCPServer

                body = json.dumps({"ok": True, "service": "claude-hub", "protocol": 1}).encode()
                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    def log_message(self, *_args):
                        return
                server = TCPServer(
                    ("127.0.0.1", int(os.environ["CLAUDE_HUB_PORT"])),
                    Handler,
                    bind_and_activate=False,
                )
                server.socket.close()
                server.socket = socket.socket(
                    fileno=int(os.environ["CLAUDE_HUB_LISTEN_FD"])
                )
                server.server_address = server.socket.getsockname()
                server.serve_forever()
                """,
            )
            with loaded_launcher(env) as launcher:
                real_popen = subprocess.Popen
                started = threading.Barrier(2)
                errors: list[BaseException] = []

                def start_hub() -> None:
                    try:
                        started.wait(timeout=2)
                        launcher.ensure_hub(port)
                    except BaseException as exc:  # assertions run in the parent thread
                        errors.append(exc)

                with mock.patch.object(launcher.subprocess, "Popen", side_effect=real_popen) as popen:
                    threads = [threading.Thread(target=start_hub) for _ in range(2)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=8)

                try:
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                    self.assertEqual(errors, [])
                    self.assertEqual(popen.call_count, 1)
                finally:
                    for process in launcher._hub_processes:
                        if process.poll() is None:
                            process.terminate()
                            process.wait(timeout=5)

    def test_hub_start_reports_an_occupied_port_before_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            script = Path(env["CLAUDE1_HUB_SCRIPT"])
            script.parent.mkdir(parents=True)
            write_executable(script, "#!/bin/sh\nexit 0\n")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = int(listener.getsockname()[1])
                with loaded_launcher(env) as launcher, mock.patch.object(
                    launcher, "hub_healthy", return_value=False
                ), mock.patch.object(launcher.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(
                        RuntimeError, rf"端口 {port}.*占用"
                    ):
                        launcher.ensure_hub(port)

            popen.assert_not_called()

    def test_hub_start_preserves_non_address_socket_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            script = Path(env["CLAUDE1_HUB_SCRIPT"])
            script.parent.mkdir(parents=True)
            write_executable(script, "#!/bin/sh\nexit 0\n")
            failure = OSError(errno.ENFILE, "fixture file table full")
            with loaded_launcher(env) as launcher, mock.patch.object(
                launcher, "hub_healthy", return_value=False
            ), mock.patch.object(
                launcher, "_reserve_loopback_port", side_effect=failure
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "fixture file table full"
                ) as raised:
                    launcher.ensure_hub(18787)

            self.assertNotIn("已被占用", str(raised.exception))

    def test_noninteractive_provider_selection_has_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                with mock.patch("builtins.input", side_effect=EOFError):
                    with self.assertRaisesRegex(RuntimeError, "标准输入不可用"):
                        launcher.choose([{"id": "one", "name": "One"}], None)

    def test_list_explains_how_to_view_when_every_provider_is_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            db_path = Path(env["CLAUDE1_DB_PATH"])
            db_path.parent.mkdir(parents=True)
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, settings_config TEXT, app_type TEXT, sort_index INTEGER)"
                )
                connection.execute(
                    "INSERT INTO providers VALUES ('one', 'One', '{\"env\": {}}', 'claude', 1)"
                )
                connection.commit()
            finally:
                connection.close()
            config_path = Path(env["CLAUDE1_CONFIG_PATH"])
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps({"version": 3, "providers": {"one": {"name": "One", "hidden": True}}}),
                encoding="utf-8",
            )
            with loaded_launcher(env) as launcher:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(launcher.cli_list_providers(), 1)

        self.assertIn("claude1 list --all", output.getvalue())

    def test_tui_reverts_alias_and_hidden_changes_when_persistence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                alias_cfg = {"providers": {"one": {"name": "One", "hidden": False}}}

                def edit_alias(_win, provider_id, meta):
                    meta[provider_id]["alias"] = "new-alias"
                    return True, "别名已设为 new-alias"

                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "_draw_launcher"),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                    mock.patch.object(launcher, "save_config", return_value=False),
                    mock.patch.object(launcher, "_edit_alias", side_effect=edit_alias),
                ):
                    launcher._launcher_main(ScriptedWindow([ord("a"), ord("q")]), alias_cfg, {"one"})
                self.assertNotIn("alias", alias_cfg["providers"]["one"])

                hidden_cfg = {"providers": {"one": {"name": "One", "hidden": False}}}
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "_draw_launcher"),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                    mock.patch.object(launcher, "save_config", return_value=False),
                    mock.patch.object(launcher, "_confirm", return_value=True),
                ):
                    launcher._launcher_main(ScriptedWindow([ord("x"), ord("q")]), hidden_cfg, {"one"})
                self.assertFalse(hidden_cfg["providers"]["one"]["hidden"])

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
                "ACCOUNT_POOL_CONFIG": "CLAUDE1_ACCOUNT_POOL_CONFIG",
                "ACCOUNT_POOL_STATE": "CLAUDE1_ACCOUNT_POOL_STATE",
                "BACKEND_STATE": "CLAUDE1_BACKEND_STATE",
                "BACKEND_STICKY": "CLAUDE1_BACKEND_STICKY",
                "ANYROUTER_OBSERVER": "CLAUDE1_ANYROUTER_OBSERVER",
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

    def test_build_settings_seals_model_slots_against_user_settings_leak(self) -> None:
        # Claude Code merges ~/.claude/settings.json env under the private
        # --settings file, so slot keys the provider does not define would
        # otherwise leak in from the CC Switch current provider and show
        # foreign models in /model.
        provider_env = {
            "ANTHROPIC_BASE_URL": "https://kimi.example/coding/",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_MODEL": "kimi-for-coding",
            "CLAUDE_CODE_SUBAGENT_MODEL": "must-not-pin-subagents",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "k3",
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "k3",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "k3[1M]",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "k3",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "kimi-for-coding",
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "kimi-for-coding",
            # Haiku model without a sibling _NAME: the leak vector.
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "kimi-for-coding",
        }
        provider = {
            "id": "p1",
            "name": "Fixture Kimi",
            "settings_config": json.dumps({"env": provider_env}),
            "meta": "{}",
            "provider_type": None,
        }
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                env = launcher.build_settings(provider)["env"]

        # Owned slots keep the provider's own values verbatim.
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "k3")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"], "k3")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "")
        # A missing _NAME is sealed to the provider's model id (the menu uses
        # ??, so an empty string would render a blank label).
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "kimi-for-coding")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"], "kimi-for-coding")
        self.assertEqual(
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL_DESCRIPTION"], "Custom Haiku model"
        )
        # Slots the provider does not serve are blanked entirely so Claude
        # Code falls back to its built-in entries instead of leaking the
        # foreign ANTHROPIC_CUSTOM_MODEL_OPTION from user settings.
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION"], "")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"], "")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"], "")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES"], "")

    def test_build_settings_seals_provider_without_any_slots(self) -> None:
        provider = {
            "id": "p2",
            "name": "Fixture Bare",
            "settings_config": json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://api.example.com",
                        "ANTHROPIC_AUTH_TOKEN": "tok",
                        "ANTHROPIC_MODEL": "some-model",
                    }
                }
            ),
            "meta": "{}",
            "provider_type": None,
        }
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                env = launcher.build_settings(provider)["env"]

                self.assertEqual(env["ANTHROPIC_MODEL"], "some-model")
                for tier in launcher.MODEL_SLOT_TIERS:
                    self.assertEqual(env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"], "")
                    self.assertEqual(env[f"ANTHROPIC_DEFAULT_{tier}_MODEL_NAME"], "")
                self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION"], "")

    def test_explicit_proxy_removes_the_api_host_from_no_proxy_case_insensitively(self) -> None:
        provider = {
            "id": "proxy",
            "name": "Proxy Provider",
            "settings_config": json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://API.EXAMPLE.COM/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-token",
                        "HTTPS_PROXY": "http://127.0.0.1:7890",
                        "NO_PROXY": "API.EXAMPLE.COM,localhost",
                    }
                }
            ),
            "meta": "{}",
            "provider_type": None,
        }
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                settings = launcher.build_settings(provider)

        self.assertEqual(settings["env"]["NO_PROXY"], "localhost")

    def test_anyrouter_observer_leaves_legacy_hook_shapes_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            observer = Path(env["CLAUDE1_ANYROUTER_OBSERVER"])
            observer.parent.mkdir(parents=True)
            observer.write_text("fixture", encoding="utf-8")
            settings = {"hooks": []}

            with loaded_launcher(env) as launcher:
                launcher.add_anyrouter_observer(settings, "Any router")

        self.assertEqual(settings, {"hooks": []})

    def test_native_provider_pool_rotates_only_the_credential_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            database = Path(env["CLAUDE1_DB_PATH"])
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, app_type TEXT, sort_index INTEGER, "
                    "settings_config TEXT)"
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, 'claude', ?, ?)",
                    [
                        (
                            "primary",
                            "Pooled provider",
                            0,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid/v1",
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-primary-token",
                                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "primary-model",
                                    }
                                }
                            ),
                        ),
                        (
                            "secondary",
                            "Pooled provider account 2",
                            1,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid/v1",
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-secondary-token",
                                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "must-not-replace-primary-model",
                                    }
                                }
                            ),
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)
            pool_config = Path(env["CLAUDE1_ACCOUNT_POOL_CONFIG"])
            pool_config.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "providers": {
                            "id:primary": {
                                "strategy": "round_robin",
                                "cooldown_seconds": 60,
                                "max_cooldown_seconds": 3600,
                                "members": [
                                    {"provider": "id:primary"},
                                    {"provider": "id:secondary"},
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            pool_config.chmod(0o600)

            with loaded_launcher(env) as launcher:
                selected = launcher._provider_from_row(launcher.db_claude_rows()[0])
                observed = []

                def capture(settings, _args):
                    observed.append(json.loads(json.dumps(settings)))
                    return 0

                with mock.patch.object(
                    launcher, "launch_with_settings", side_effect=capture
                ):
                    self.assertEqual(launcher.launch_provider(selected, []), 0)
                    self.assertEqual(launcher.launch_provider(selected, []), 0)

            self.assertEqual(
                [item["env"]["ANTHROPIC_AUTH_TOKEN"] for item in observed],
                ["fixture-primary-token", "fixture-secondary-token"],
            )
            self.assertEqual(
                [item["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] for item in observed],
                ["primary-model", "primary-model"],
            )

    def test_accounts_cli_groups_existing_provider_ids_without_copying_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            database = Path(env["CLAUDE1_DB_PATH"])
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, app_type TEXT, sort_index INTEGER, "
                    "settings_config TEXT)"
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, 'claude', ?, ?)",
                    [
                        (
                            "primary",
                            "Main account",
                            0,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid/v1",
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-main-key",
                                    }
                                }
                            ),
                        ),
                        (
                            "secondary",
                            "Backup account",
                            1,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid/v1",
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-backup-key",
                                    }
                                }
                            ),
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)

            with loaded_launcher(env) as launcher:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(
                        launcher.cli_accounts(
                            [
                                "add",
                                "id:primary",
                                "id:secondary",
                                "--weight",
                                "3",
                                "--priority",
                                "2",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        launcher.cli_accounts(
                            ["policy", "id:primary", "weighted"]
                        ),
                        0,
                    )
                    self.assertEqual(
                        launcher.cli_accounts(
                            [
                                "set",
                                "id:primary",
                                "id:primary",
                                "--weight",
                                "2",
                                "--priority",
                                "1",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        launcher.cli_accounts(["list", "id:primary"]),
                        0,
                    )

            config_path = Path(env["CLAUDE1_ACCOUNT_POOL_CONFIG"])
            config_text = config_path.read_text(encoding="utf-8")
            config = json.loads(config_text)
            pool = config["providers"]["id:primary"]
            self.assertEqual(pool["strategy"], "weighted")
            self.assertEqual(pool["members"][0]["weight"], 2)
            self.assertEqual(pool["members"][0]["priority"], 1)
            self.assertEqual(pool["members"][1]["weight"], 3)
            self.assertEqual(pool["members"][1]["priority"], 2)
            self.assertNotIn("fixture-main-key", config_text)
            self.assertNotIn("fixture-backup-key", config_text)
            rendered = output.getvalue()
            self.assertIn("Main account", rendered)
            self.assertIn("Backup account", rendered)
            self.assertNotIn("fixture-main-key", rendered)
            self.assertNotIn("fixture-backup-key", rendered)
            if os.name == "posix":
                self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

    def test_accounts_cli_normalizes_v1_and_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            database = Path(env["CLAUDE1_DB_PATH"])
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, app_type TEXT, sort_index INTEGER, "
                    "settings_config TEXT)"
                )
                connection.executemany(
                    "INSERT INTO providers VALUES (?, ?, 'claude', ?, ?)",
                    [
                        (
                            "primary",
                            "Main account",
                            0,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid",
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-main-key",
                                    }
                                }
                            ),
                        ),
                        (
                            "secondary",
                            "Second account",
                            1,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid/v1",
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-second-key",
                                    }
                                }
                            ),
                        ),
                        (
                            "duplicate",
                            "Duplicate key",
                            2,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid/v1",
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-second-key",
                                    }
                                }
                            ),
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)

            with loaded_launcher(env) as launcher:
                self.assertEqual(
                    launcher.cli_accounts(
                        ["add", "id:primary", "id:secondary"]
                    ),
                    0,
                )
                with self.assertRaisesRegex(RuntimeError, "同一个凭证"):
                    launcher.cli_accounts(
                        ["add", "id:primary", "id:duplicate"]
                    )

    def test_accounts_cli_removes_orphaned_member_by_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            database = Path(env["CLAUDE1_DB_PATH"])
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE providers ("
                    "id TEXT, name TEXT, app_type TEXT, sort_index INTEGER, "
                    "settings_config TEXT)"
                )
                for index, provider_id in enumerate(("primary", "secondary")):
                    connection.execute(
                        "INSERT INTO providers VALUES (?, ?, 'claude', ?, ?)",
                        (
                            provider_id,
                            provider_id.title(),
                            index,
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": "https://pool.invalid/v1",
                                        "ANTHROPIC_AUTH_TOKEN": f"fixture-{provider_id}-key",
                                    }
                                }
                            ),
                        ),
                    )
                connection.commit()
            finally:
                connection.close()
            database.chmod(0o600)

            with loaded_launcher(env) as launcher:
                self.assertEqual(
                    launcher.cli_accounts(
                        ["add", "id:primary", "id:secondary"]
                    ),
                    0,
                )
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "DELETE FROM providers WHERE id='secondary'"
                    )
                    connection.commit()
                finally:
                    connection.close()
                self.assertEqual(
                    launcher.cli_accounts(
                        ["remove", "id:primary", "id:secondary"]
                    ),
                    0,
                )

            config = json.loads(
                Path(env["CLAUDE1_ACCOUNT_POOL_CONFIG"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                [
                    item["provider"]
                    for item in config["providers"]["id:primary"]["members"]
                ],
                ["id:primary"],
            )

    def test_launch_rejects_invalid_provider_settings_with_the_provider_name(self) -> None:
        provider = {
            "id": "broken",
            "name": "Broken JSON",
            "settings_config": "{",
            "meta": "{}",
            "provider_type": None,
        }
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                with mock.patch.object(launcher, "launch_with_settings") as launch:
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "Broken JSON.*settings_config",
                    ):
                        launcher.launch_provider(provider, [])

                launch.assert_not_called()

    def test_launch_rejects_a_malformed_provider_url_with_the_provider_name(self) -> None:
        provider = {
            "id": "broken-url",
            "name": "Broken URL",
            "settings_config": json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "http://[::1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-token",
                    }
                }
            ),
            "meta": "{}",
            "provider_type": None,
        }
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                with mock.patch.object(launcher, "launch_with_settings") as launch:
                    with self.assertRaisesRegex(RuntimeError, "Broken URL.*URL 无效"):
                        launcher.launch_provider(provider, [])

                launch.assert_not_called()

    def test_launch_rejects_a_non_object_provider_environment(self) -> None:
        provider = {
            "id": "broken-env",
            "name": "Broken Env",
            "settings_config": json.dumps({"env": []}),
            "meta": "{}",
            "provider_type": None,
        }
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "Broken Env.*env 必须是 JSON 对象",
                ):
                    launcher.build_settings(provider)

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
            sticky.write_text("hub\n", encoding="utf-8")

            with loaded_launcher(env) as launcher:
                self.assertEqual(launcher.exec_plain_claude("direct", ["-p", "ok"]), 0)

            self.assertEqual(sticky.read_text(encoding="utf-8"), "hub\n")
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

    def test_use_does_not_claim_ordinary_claude_changed_without_shell_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(launcher.set_sticky("hub"), 0)

            self.assertIn("已保存粘性后端 = hub", output.getvalue())
            self.assertIn("当前 shell 未启用", output.getvalue())
            self.assertNotIn("之后普通 claude", output.getvalue())

    def test_use_confirms_routing_when_shell_opt_in_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(
                Path(raw_home),
                CLAUDE1_STICKY_INTEGRATION="1",
            )
            with loaded_launcher(env) as launcher:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(launcher.set_sticky("hub"), 0)

            self.assertIn("之后普通 claude 走多渠道网关", output.getvalue())

    def test_temporary_settings_are_0600_and_removed_after_fake_claude(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(home)
            fake_claude = home / "bin" / "claude"
            capture = home / "capture.json"
            global_settings = home / "global-settings.json"
            fake_claude.parent.mkdir(parents=True)
            Path(env["CLAUDE1_TMP_DIR"]).mkdir(parents=True)
            global_settings.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_AUTH_TOKEN": "fixture-current-credential"
                        }
                    }
                ),
                encoding="utf-8",
            )
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
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                effective = dict(os.environ)
                effective.update(json.loads(
                    Path(os.environ["FAKE_GLOBAL_SETTINGS"]).read_text(
                        encoding="utf-8"
                    )
                )["env"])
                effective.update(settings["env"])
                Path(os.environ["FAKE_CLAUDE_CAPTURE"]).write_text(json.dumps({
                    "argv": sys.argv[1:],
                    "env": dict(os.environ),
                    "effective_auth": effective["ANTHROPIC_AUTH_TOKEN"],
                    "mode": stat.S_IMODE(settings_path.stat().st_mode),
                    "settings_path": str(settings_path),
                    "settings": settings,
                }), encoding="utf-8")
                raise SystemExit(0)
                """,
            )
            env.update(
                CLAUDE1_CLAUDE_BIN=str(fake_claude),
                FAKE_CLAUDE_CAPTURE=str(capture),
                FAKE_GLOBAL_SETTINGS=str(global_settings),
                ANTHROPIC_BASE_URL="https://untrusted.invalid",
                ANTHROPIC_AUTH_TOKEN="must-not-leak",
                HTTP_PROXY="http://must-not-leak.invalid",
                https_proxy="http://must-not-leak.invalid",
                CLAUDE_CONFIG_DIR=str(home / "untrusted-config"),
                CLAUDE_CODE_PARENT_SESSION_ID="must-not-leak",
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
            self.assertEqual(
                observed["settings"],
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://fixture.invalid",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-secret",
                    }
                },
            )
            self.assertNotIn("fixture-secret", observed["argv"])
            self.assertEqual(observed["effective_auth"], "fixture-secret")
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

                payload = json.dumps(valid).encode("utf-8")
                with health_server(200, payload) as port:
                    self.assertFalse(
                        launcher.hub_healthy(port, "fixture-local-token")
                    )
                with health_server(
                    200, payload, ready_token="fixture-local-token"
                ) as port:
                    self.assertTrue(
                        launcher.hub_healthy(port, "fixture-local-token")
                    )
                identity_payload = json.dumps(
                    {**valid, "identity_protocol": 2}
                ).encode("utf-8")
                with health_server(
                    200,
                    identity_payload,
                    ready_token="fixture-local-token",
                    ready_instance_id="kimi-hub",
                ) as port:
                    self.assertTrue(
                        launcher.hub_healthy(
                            port,
                            "fixture-local-token",
                            "kimi-hub",
                        )
                    )
                    self.assertFalse(
                        launcher.hub_healthy(
                            port,
                            "fixture-local-token",
                            "any-hub",
                        )
                    )

    def test_ensure_hub_timeout_stops_and_reaps_spawned_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            script = Path(env["CLAUDE1_HUB_SCRIPT"])
            script.parent.mkdir(parents=True)
            write_executable(script, "#!/bin/sh\nexit 0\n")
            port = free_local_port()
            with loaded_launcher(env) as launcher:
                process = mock.Mock()
                process.poll.return_value = None
                process.wait.return_value = 0
                with mock.patch.object(
                    launcher, "hub_healthy", return_value=False
                ), mock.patch.object(
                    launcher.subprocess, "Popen", return_value=process
                ), mock.patch.object(
                    launcher, "_hub_start_timeout", return_value=1
                ), mock.patch.object(
                    launcher.time, "monotonic", side_effect=[0, 2]
                ):
                    with self.assertRaisesRegex(RuntimeError, "启动失败"):
                        launcher.ensure_hub(port, token="fixture-local-token")

                process.terminate.assert_called_once_with()
                process.wait.assert_called_once()
                self.assertNotIn(process, launcher._hub_processes)

    def test_protocol_bridge_selects_provider_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            script = Path(env["CLAUDE1_HUB_SCRIPT"])
            script.parent.mkdir(parents=True)
            write_executable(script, "#!/bin/sh\nexit 0\n")
            with loaded_launcher(env) as launcher:
                written = []
                real_write = launcher._atomic_private_write

                def capture(path, content):
                    if path.name == "hub.json":
                        written.append(json.loads(content))
                    return real_write(path, content)

                process = mock.Mock()
                process.poll.return_value = None
                process.wait.return_value = 0
                with mock.patch.object(
                    launcher, "_atomic_private_write", side_effect=capture
                ), mock.patch.object(
                    launcher.subprocess, "Popen", return_value=process
                ) as popen, mock.patch.object(
                    launcher, "hub_healthy", return_value=True
                ), mock.patch.object(
                    launcher, "launch_with_settings", return_value=0
                ):
                    result = launcher.launch_with_protocol_bridge(
                        {"id": "provider-id", "name": "Duplicate Name"},
                        {"env": {}},
                        "openai_chat",
                        [],
                    )

                self.assertEqual(result, 0)
                self.assertEqual(
                    written[0]["channels"]["direct"]["provider"],
                    "id:provider-id",
                )
                spawn = popen.call_args
                inherited_fd = spawn.kwargs["pass_fds"][0]
                self.assertEqual(
                    spawn.kwargs["env"]["CLAUDE_HUB_LISTEN_FD"],
                    str(inherited_fd),
                )

    def _bridge_fixture(self, raw_home: str):
        env = isolated_env(Path(raw_home))
        script = Path(env["CLAUDE1_HUB_SCRIPT"])
        script.parent.mkdir(parents=True)
        write_executable(script, "#!/bin/sh\nexit 0\n")
        return env

    def test_protocol_bridge_routes_unknown_models_to_the_default_channel(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(self._bridge_fixture(raw_home)) as launcher:
                written = []
                real_write = launcher._atomic_private_write

                def capture(path, content):
                    if path.name == "hub.json":
                        written.append(json.loads(content))
                    return real_write(path, content)

                process = mock.Mock()
                process.poll.return_value = None
                process.wait.return_value = 0
                with mock.patch.object(
                    launcher, "_atomic_private_write", side_effect=capture
                ), mock.patch.object(
                    launcher.subprocess, "Popen", return_value=process
                ), mock.patch.object(
                    launcher, "hub_healthy", return_value=True
                ), mock.patch.object(
                    launcher, "launch_with_settings", return_value=0
                ):
                    result = launcher.launch_with_protocol_bridge(
                        {"id": "chat-provider", "name": "Chat Provider"},
                        {"env": {"ANTHROPIC_AUTH_TOKEN": "fixture-token"}},
                        "openai_chat",
                        [],
                    )

                self.assertEqual(result, 0)
                # 无 ANTHROPIC_MODEL 的 provider 也允许 Claude Code
                # 内置默认模型名透传到 default channel。
                self.assertEqual(written[0]["default_channel"], "direct")
                self.assertIs(
                    written[0]["channels"]["direct"]["route_unknown_to_default"], True
                )

    def test_protocol_bridge_error_includes_a_bounded_log_tail(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(self._bridge_fixture(raw_home)) as launcher:
                process = mock.Mock()
                process.poll.return_value = 1
                process.wait.return_value = 1

                def fake_popen(*_args, **kwargs):
                    kwargs["stdout"].write(
                        b"x" * 5000 + b"fatal upstream auth error\n"
                    )
                    kwargs["stdout"].flush()
                    return process

                with mock.patch.object(
                    launcher.subprocess, "Popen", side_effect=fake_popen
                ), mock.patch.object(
                    launcher, "hub_healthy", return_value=False
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        launcher.launch_with_protocol_bridge(
                            {"id": "chat-provider", "name": "Chat Provider"},
                            {"env": {}},
                            "openai_chat",
                            [],
                        )

                message = str(caught.exception)
                self.assertIn("协议桥提前退出（状态 1）", message)
                self.assertIn("fatal upstream auth error", message)
                # 日志尾部有界 4 KiB：更靠前的填充内容不得进入错误消息。
                self.assertNotIn("x" * 5000, message)

    def test_protocol_bridge_ensures_the_local_gateway_for_the_original_url(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(self._bridge_fixture(raw_home)) as launcher:
                process = mock.Mock()
                process.poll.return_value = None
                process.wait.return_value = 0
                original_url = "http://127.0.0.1:18317/v1"
                with mock.patch.object(
                    launcher, "ensure_local_gateway"
                ) as gateway, mock.patch.object(
                    launcher.subprocess, "Popen", return_value=process
                ), mock.patch.object(
                    launcher, "hub_healthy", return_value=True
                ), mock.patch.object(
                    launcher, "launch_with_settings", return_value=0
                ):
                    result = launcher.launch_with_protocol_bridge(
                        {"id": "chat-provider", "name": "Chat Provider"},
                        {"env": {"ANTHROPIC_BASE_URL": original_url}},
                        "openai_chat",
                        [],
                    )

                self.assertEqual(result, 0)
                # 必须对原始（未改写为桥地址的）provider URL 调一次。
                gateway.assert_called_once_with(original_url)

    def test_protocol_bridge_gateway_failure_aborts_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(self._bridge_fixture(raw_home)) as launcher:
                with mock.patch.object(
                    launcher,
                    "ensure_local_gateway",
                    side_effect=RuntimeError("本地网关启动失败"),
                ), mock.patch.object(
                    launcher.subprocess, "Popen"
                ) as popen:
                    with self.assertRaisesRegex(RuntimeError, "本地网关启动失败"):
                        launcher.launch_with_protocol_bridge(
                            {"id": "chat-provider", "name": "Chat Provider"},
                            {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18317/v1"}},
                            "openai_chat",
                            [],
                        )

                popen.assert_not_called()

    def test_protocol_bridge_records_usage_only_after_a_healthy_start(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(self._bridge_fixture(raw_home)) as launcher:
                provider = {"id": "chat-provider", "name": "Chat Provider"}

                # 启动失败：bridge 提前退出时不计数。
                failed = mock.Mock()
                failed.poll.return_value = 1
                failed.wait.return_value = 1
                with mock.patch.object(
                    launcher, "record_use"
                ) as record_use, mock.patch.object(
                    launcher, "record_backend"
                ) as record_backend, mock.patch.object(
                    launcher.subprocess, "Popen", return_value=failed
                ), mock.patch.object(
                    launcher, "hub_healthy", return_value=False
                ):
                    with self.assertRaisesRegex(RuntimeError, "协议桥提前退出"):
                        launcher.launch_with_protocol_bridge(
                            provider,
                            {"env": {}},
                            "openai_chat",
                            [],
                        )
                record_use.assert_not_called()
                record_backend.assert_not_called()

                # 健康检查通过、即将真正启动时按 provider 计数一次。
                healthy = mock.Mock()
                healthy.poll.return_value = None
                healthy.wait.return_value = 0
                with mock.patch.object(
                    launcher, "record_use"
                ) as record_use, mock.patch.object(
                    launcher, "record_backend"
                ) as record_backend, mock.patch.object(
                    launcher.subprocess, "Popen", return_value=healthy
                ), mock.patch.object(
                    launcher, "hub_healthy", return_value=True
                ), mock.patch.object(
                    launcher, "launch_with_settings", return_value=0
                ):
                    self.assertEqual(
                        launcher.launch_with_protocol_bridge(
                            provider,
                            {"env": {}},
                            "openai_chat",
                            [],
                            backend_kind="current",
                        ),
                        0,
                    )
                record_use.assert_called_once_with("chat-provider")
                record_backend.assert_called_once_with("current", "Chat Provider")

    def test_protocol_bridge_requires_a_posix_platform(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(self._bridge_fixture(raw_home)) as launcher:
                with mock.patch.object(
                    launcher.os, "name", "nt"
                ), mock.patch.object(
                    launcher.subprocess, "Popen"
                ) as popen:
                    with self.assertRaisesRegex(RuntimeError, "POSIX"):
                        launcher.launch_with_protocol_bridge(
                            {"id": "chat-provider", "name": "Chat Provider"},
                            {"env": {}},
                            "openai_chat",
                            [],
                        )
                popen.assert_not_called()

    def test_launch_records_usage_only_after_preflight_checks_pass(self) -> None:
        provider = {
            "id": "gateway-provider",
            "name": "Gateway Provider",
            "settings_config": json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18317/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-token",
                    }
                }
            ),
            "meta": "{}",
            "provider_type": None,
        }
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                # 直连分支前置检查失败：ensure_local_gateway 抛错时不计数。
                with mock.patch.object(
                    launcher,
                    "ensure_local_gateway",
                    side_effect=RuntimeError("本地网关启动失败"),
                ), mock.patch.object(
                    launcher, "record_use"
                ) as record_use, mock.patch.object(
                    launcher, "record_backend"
                ) as record_backend, mock.patch.object(
                    launcher, "launch_with_settings", return_value=0
                ) as launch:
                    with self.assertRaisesRegex(RuntimeError, "本地网关启动失败"):
                        launcher.launch_provider(provider, [])
                record_use.assert_not_called()
                record_backend.assert_not_called()
                launch.assert_not_called()

                # 前置检查通过、即将启动时计数一次。
                with mock.patch.object(
                    launcher, "ensure_local_gateway"
                ), mock.patch.object(
                    launcher, "record_use"
                ) as record_use, mock.patch.object(
                    launcher, "record_backend"
                ) as record_backend, mock.patch.object(
                    launcher, "launch_with_settings", return_value=0
                ):
                    self.assertEqual(launcher.launch_provider(provider, []), 0)
                record_use.assert_called_once_with("gateway-provider")
                record_backend.assert_called_once_with("provider", "Gateway Provider")

    def test_gateway_health_requires_a_successful_http_response(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                for status_code, expected in ((200, True), (204, True), (400, False), (503, False)):
                    with self.subTest(status_code=status_code):
                        with health_server(status_code, b"gateway") as port:
                            with mock.patch.object(launcher, "GATEWAY_URL", f"http://127.0.0.1:{port}"):
                                self.assertIs(launcher.gateway_healthy(), expected)

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
                FIXTURE_HUB_TOKEN="fixture-local-token",
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
                import hmac
                import json
                import os
                import socket
                from http.server import BaseHTTPRequestHandler
                from pathlib import Path
                from socketserver import TCPServer

                Path(os.environ["CLAUDE_HUB_CONFIG"]).write_text(
                    json.dumps(dict(os.environ)), encoding="utf-8"
                )
                health = {
                    "ok": True,
                    "service": "claude-hub",
                    "protocol": 1,
                    "version": "0.1.0",
                }

                class Handler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        challenge = self.headers.get("X-Claude-Hub-Challenge", "")
                        proof_message = (
                            f"claude-hub-ready:v1:{os.environ['CLAUDE_HUB_PORT']}:{challenge}"
                            .encode()
                        )
                        body = json.dumps({
                            **health,
                            "proof": hmac.digest(
                                os.environ["FIXTURE_HUB_TOKEN"].encode(),
                                proof_message,
                                "sha256",
                            ).hex(),
                        }).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    def log_message(self, _format, *_args):
                        return

                server = TCPServer(
                    ("127.0.0.1", int(os.environ["CLAUDE_HUB_PORT"])),
                    Handler,
                    bind_and_activate=False,
                )
                server.socket.close()
                server.socket = socket.socket(
                    fileno=int(os.environ["CLAUDE_HUB_LISTEN_FD"])
                )
                server.server_address = server.socket.getsockname()
                server.serve_forever()
                """,
            )

            with loaded_launcher(env) as launcher:
                try:
                    launcher.ensure_hub(
                        port,
                        token="fixture-local-token",
                        token_env="FIXTURE_HUB_TOKEN",
                    )
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
            self.assertEqual(child_env["FIXTURE_HUB_TOKEN"], "fixture-local-token")
            self.assertNotIn("CLAUDE_HUB_LOCAL_TOKEN", child_env)
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

    def test_isolated_wechat_coding_plan_text_and_tool_smoke(self) -> None:
        """Run the public launcher -> Hub path without a real Claude credential.

        The fake Claude is deliberately a process rather than an in-process
        client: it receives the private settings overlay produced by the
        launcher, sends two Anthropic Messages requests to the real Hub, and
        performs the normal tool-use / tool-result continuation.  It writes
        only non-sensitive outcome facts for this test to inspect.
        """

        received: list[dict] = []
        received_lock = threading.Lock()

        class LoopbackUpstream(ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != "/v1/chat/completions":
                    self._send_json({"error": "unexpected fixture path"}, 404)
                    return
                try:
                    content_length = int(self.headers.get("Content-Length", ""))
                    payload = json.loads(
                        self.rfile.read(content_length).decode("utf-8")
                    )
                except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json({"error": "invalid fixture request"}, 400)
                    return
                if not isinstance(payload, dict):
                    self._send_json({"error": "invalid fixture request"}, 400)
                    return

                # Do not capture request headers: they can contain the
                # fixture upstream credential and are irrelevant to the
                # conversion contract under test.
                with received_lock:
                    received.append(payload)
                    call_number = len(received)

                if call_number == 1:
                    self._send_json(
                        {
                            "id": "chatcmpl_fixture_plan_tool",
                            "object": "chat.completion",
                            "model": "fixture-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": None,
                                        "tool_calls": [
                                            {
                                                "id": "call_fixture_plan",
                                                "type": "function",
                                                "function": {
                                                    "name": "write_plan",
                                                    "arguments": (
                                                        '{"title":"微信 Coding Plan"}'  # secret-guard: allow private-provider-name 65941246a1
                                                    ),
                                                },
                                            }
                                        ],
                                    },
                                    "finish_reason": "tool_calls",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 7,
                                "completion_tokens": 3,
                            },
                        }
                    )
                    return
                if call_number == 2:
                    self._send_json(
                        {
                            "id": "chatcmpl_fixture_plan_final",
                            "object": "chat.completion",
                            "model": "fixture-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "plan complete",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 13,
                                "completion_tokens": 2,
                            },
                        }
                    )
                    return
                self._send_json({"error": "too many fixture requests"}, 409)

            def log_message(self, _format: str, *_args) -> None:
                return

        server = LoopbackUpstream(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            upstream_port = int(server.server_address[1])
            with tempfile.TemporaryDirectory() as raw_home:
                home = Path(raw_home)
                hub_port = free_local_port()
                env = isolated_env(
                    home,
                    CLAUDE1_HUB_PORT=str(hub_port),
                    CLAUDE1_HUB_START_TIMEOUT="5",
                    FIXTURE_HUB_TOKEN="fixture-local-token",
                    # Sentinel routing values prove the launcher/Hub child
                    # processes do not inherit ambient provider or proxy state.
                    ANTHROPIC_AUTH_TOKEN="must-not-leak",
                    ANTHROPIC_API_KEY="must-not-leak",
                    ANTHROPIC_BASE_URL="http://must-not-leak.invalid",
                    HTTP_PROXY="http://must-not-leak.invalid",
                    HTTPS_PROXY="http://must-not-leak.invalid",
                    ALL_PROXY="socks5://must-not-leak.invalid",
                    CLAUDE_HUB_LOCAL_TOKEN="must-not-leak",
                )

                provider_db = Path(env["CLAUDE1_HUB_DB"])
                provider_db.parent.mkdir(parents=True)
                with sqlite3.connect(provider_db) as connection:
                    connection.execute(
                        "CREATE TABLE providers ("
                        "id TEXT, name TEXT, app_type TEXT, "
                        "settings_config TEXT, meta TEXT, sort_index INTEGER)"
                    )
                    connection.execute(
                        "INSERT INTO providers VALUES (?, ?, 'claude', ?, ?, 1)",
                        (
                            "fixture-provider",
                            "Fixture OpenAI Chat",
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": (
                                            f"http://127.0.0.1:{upstream_port}/v1"
                                        ),
                                        "ANTHROPIC_AUTH_TOKEN": (
                                            "fixture-upstream-token"
                                        ),
                                    }
                                }
                            ),
                            json.dumps({"apiFormat": "openai_chat"}),
                        ),
                    )
                provider_db.chmod(0o600)

                selector = "wechat,fixture-model"
                hub_config = Path(env["CLAUDE1_HUB_CONFIG"])
                hub_config.parent.mkdir(parents=True)
                hub_config.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "port": hub_port,
                            "local_token_env": "FIXTURE_HUB_TOKEN",
                            "default_channel": "wechat",
                            "channels": {
                                "wechat": {
                                    "provider": "id:fixture-provider",
                                    "models": ["fixture-model"],
                                }
                            },
                            "launch_slot": "fable",
                            "model_slots": {
                                "fable": selector,
                                "opus": selector,
                                "sonnet": selector,
                                "haiku": selector,
                            },
                            "effort_by_slot": {
                                "fable": "high",
                                "opus": "high",
                                "sonnet": "medium",
                                "haiku": "low",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                hub_config.chmod(0o600)

                # Avoid the production file's ``uv run --script`` shebang in
                # the test while still executing its real Python entry point.
                hub_wrapper = Path(env["CLAUDE1_HUB_SCRIPT"])
                aiohttp_spec = importlib.util.find_spec("aiohttp")
                self.assertIsNotNone(aiohttp_spec)
                assert aiohttp_spec is not None
                package_locations = aiohttp_spec.submodule_search_locations
                self.assertIsNotNone(package_locations)
                assert package_locations is not None
                dependency_root = Path(next(iter(package_locations))).parent
                runtime_pythonpath = os.pathsep.join(
                    (str(dependency_root), str(ROOT))
                )
                hub_wrapper.parent.mkdir(parents=True)
                write_executable(
                    hub_wrapper,
                    """
                    #!__PYTHON__
                    import os
                    import sys

                    # The temporary HOME deliberately hides Python's user
                    # site-packages.  This is a dependency-only path found by
                    # the parent test process, not inherited configuration.
                    os.environ["PYTHONPATH"] = __PYTHONPATH__
                    os.execv(
                        sys.executable,
                        [sys.executable, __HUB_SCRIPT__, *sys.argv[1:]],
                    )
                    """
                    .replace("__PYTHON__", sys.executable)
                    .replace("__HUB_SCRIPT__", repr(str(ROOT / "claude-hub.py")))
                    .replace("__PYTHONPATH__", repr(runtime_pythonpath)),
                )

                result_path = home / "fake-claude-result.json"
                fake_claude = home / "bin" / "claude"
                fake_claude.parent.mkdir(parents=True, exist_ok=True)
                write_executable(
                    fake_claude,
                    """
                    #!__PYTHON__
                    import json
                    import os
                    import sys
                    import urllib.request
                    from pathlib import Path


                    def require(value):
                        if not value:
                            raise RuntimeError("fixture assertion failed")


                    def post(base_url, token, payload):
                        request = urllib.request.Request(
                            base_url.rstrip("/") + "/v1/messages",
                            data=json.dumps(
                                payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "x-api-key": token,
                                "anthropic-version": "2023-06-01",
                            },
                            method="POST",
                        )
                        opener = urllib.request.build_opener(
                            urllib.request.ProxyHandler({})
                        )
                        with opener.open(request, timeout=5) as response:
                            require(response.status == 200)
                            result = json.loads(response.read().decode("utf-8"))
                        require(isinstance(result, dict))
                        return result


                    try:
                        settings_index = sys.argv.index("--settings")
                        settings = json.loads(
                            Path(sys.argv[settings_index + 1]).read_text(
                                encoding="utf-8"
                            )
                        )
                        settings_env = settings.get("env")
                        require(isinstance(settings_env, dict))
                        base_url = settings_env.get("ANTHROPIC_BASE_URL")
                        token = settings_env.get("ANTHROPIC_AUTH_TOKEN")
                        require(isinstance(base_url, str) and base_url)
                        require(isinstance(token, str) and token)

                        forwarded = sys.argv[settings_index + 2 :]
                        require("--" in forwarded)
                        claude_args = forwarded[forwarded.index("--") + 1 :]
                        prompt_index = claude_args.index("-p")
                        require(
                            claude_args[prompt_index + 1]
                            == "微信 Coding Plan"  # secret-guard: allow private-provider-name 65941246a1
                        )

                        tool = {
                            "name": "write_plan",
                            "description": "Write a Coding Plan",
                            "input_schema": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"}
                                },
                                "required": ["title"],
                            },
                        }
                        initial = post(
                            base_url,
                            token,
                            {
                                "model": settings_env["ANTHROPIC_MODEL"],
                                "max_tokens": 128,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "微信 Coding Plan",  # secret-guard: allow private-provider-name 65941246a1
                                    }
                                ],
                                "tools": [tool],
                            },
                        )
                        tool_blocks = [
                            block
                            for block in initial.get("content", [])
                            if isinstance(block, dict)
                            and block.get("type") == "tool_use"
                        ]
                        require(initial.get("stop_reason") == "tool_use")
                        require(len(tool_blocks) == 1)
                        tool_block = tool_blocks[0]
                        require(tool_block.get("id") == "call_fixture_plan")
                        require(tool_block.get("name") == "write_plan")
                        require(
                            tool_block.get("input")
                            == {"title": "微信 Coding Plan"}  # secret-guard: allow private-provider-name 65941246a1
                        )

                        final = post(
                            base_url,
                            token,
                            {
                                "model": settings_env["ANTHROPIC_MODEL"],
                                "max_tokens": 128,
                                "messages": [
                                    {
                                        "role": "user",
                                        "content": "微信 Coding Plan",  # secret-guard: allow private-provider-name 65941246a1
                                    },
                                    {
                                        "role": "assistant",
                                        "content": [tool_block],
                                    },
                                    {
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "tool_result",
                                                "tool_use_id": tool_block["id"],
                                                "is_error": True,
                                                "content": [
                                                    {
                                                        "type": "text",
                                                        "text": (
                                                            "draft validation failed"
                                                        ),
                                                    },
                                                    {
                                                        "type": "document",
                                                        "source": {
                                                            "type": "text",
                                                            "media_type": (
                                                                "text/plain"
                                                            ),
                                                            "data": (
                                                                "missing milestone"
                                                            ),
                                                        },
                                                    },
                                                ],
                                            }
                                        ],
                                    },
                                ],
                            },
                        )
                        final_text = [
                            block.get("text")
                            for block in final.get("content", [])
                            if isinstance(block, dict)
                            and block.get("type") == "text"
                        ]
                        require(final.get("stop_reason") == "end_turn")
                        require(final_text == ["plan complete"])
                        Path(os.environ["FAKE_CLAUDE_RESULT"]).write_text(
                            json.dumps(
                                {
                                    "tool_id": tool_block["id"],
                                    "first_stop_reason": initial["stop_reason"],
                                    "final_stop_reason": final["stop_reason"],
                                },
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                    except Exception:
                        raise SystemExit(1)
                    """.replace("__PYTHON__", sys.executable),
                )
                env.update(
                    CLAUDE1_CLAUDE_BIN=str(fake_claude),
                    FAKE_CLAUDE_RESULT=str(result_path),
                )

                with loaded_launcher(env) as launcher:
                    try:
                        self.assertEqual(
                            launcher.main(
                                ["--hub", "--", "-p", "微信 Coding Plan"]  # secret-guard: allow private-provider-name 65941246a1
                            ),
                            0,
                        )
                    finally:
                        for process in tuple(launcher._hub_processes):
                            launcher._stop_spawned_process(process, timeout=5)

                self.assertEqual(
                    json.loads(result_path.read_text(encoding="utf-8")),
                    {
                        "tool_id": "call_fixture_plan",
                        "first_stop_reason": "tool_use",
                        "final_stop_reason": "end_turn",
                    },
                )
                with received_lock:
                    self.assertEqual(len(received), 2)
                    initial_upstream, continued_upstream = received

                self.assertEqual(initial_upstream["model"], "fixture-model")
                self.assertEqual(
                    initial_upstream["messages"],
                    [{"role": "user", "content": "微信 Coding Plan"}],  # secret-guard: allow private-provider-name 65941246a1
                )
                function = initial_upstream["tools"][0]["function"]
                self.assertEqual(function["name"], "write_plan")
                self.assertEqual(
                    function["parameters"]["required"],
                    ["title"],
                )

                self.assertEqual(
                    continued_upstream["messages"][1]["tool_calls"][0]["id"],
                    "call_fixture_plan",
                )
                tool_result = continued_upstream["messages"][2]
                self.assertEqual(tool_result["role"], "tool")
                self.assertEqual(tool_result["tool_call_id"], "call_fixture_plan")
                self.assertEqual(
                    json.loads(tool_result["content"]),
                    {
                        "type": "anthropic_tool_result",
                        "is_error": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "draft validation failed",
                            },
                            {
                                "type": "document",
                                "source": {
                                    "type": "text",
                                    "media_type": "text/plain",
                                    "data": "missing milestone",
                                },
                            },
                        ],
                    },
                )
                serialized_requests = json.dumps(
                    received,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self.assertNotIn("must-not-leak", serialized_requests)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

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
            with health_server(
                200, valid_health, ready_token="fixture-local-token"
            ) as port:
                env = isolated_env(
                    home,
                    CLAUDE1_HUB_PORT=str(port),
                    FIXTURE_HUB_TOKEN="fixture-local-token",
                )
                config = Path(env["CLAUDE1_HUB_CONFIG"])
                config.parent.mkdir(parents=True)
                config.write_text(
                    json.dumps(
                        {
                            "port": 18787,
                            "local_token_env": "FIXTURE_HUB_TOKEN",
                            "effort_level": "xhigh",
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
                        json.dumps(
                            {
                                "settings": json.loads(
                                    path.read_text(encoding="utf-8")
                                ),
                                "credential": os.environ.get(
                                    "ANTHROPIC_AUTH_TOKEN"
                                ),
                            }
                        ),
                        encoding="utf-8",
                    )
                    """,
                )
                sticky = Path(env["CLAUDE1_BACKEND_STICKY"])
                sticky.parent.mkdir(parents=True)
                sticky.write_text("direct\n", encoding="utf-8")
                env.update(
                    CLAUDE1_CLAUDE_BIN=str(fake_claude),
                    FAKE_CLAUDE_CAPTURE=str(capture),
                )

                with loaded_launcher(env) as launcher:
                    with mock.patch.object(
                        launcher, "ensure_hub", wraps=launcher.ensure_hub
                    ) as ensure_hub:
                        self.assertEqual(launcher.exec_hub(["-p", "hello"]), 0)
                    ensure_hub.assert_called_once_with(
                        port,
                        token="fixture-local-token",
                        token_env="FIXTURE_HUB_TOKEN",
                    )

                    launched = json.loads(capture.read_text(encoding="utf-8"))
                    settings = launched["settings"]
                    self.assertEqual(
                        settings["env"]["ANTHROPIC_BASE_URL"],
                        f"http://127.0.0.1:{port}",
                    )
                    self.assertEqual(
                        settings["env"]["ANTHROPIC_AUTH_TOKEN"],
                        "fixture-local-token",
                    )
                    self.assertEqual(launched["credential"], "fixture-local-token")
                    self.assertEqual(
                        settings["env"]["ANTHROPIC_MODEL"],
                        "glm,glm-fixture",
                    )
                    self.assertEqual(settings["effortLevel"], "xhigh")
                    self.assertNotIn(
                        "CLAUDE_CODE_EFFORT_LEVEL", settings["env"]
                    )
                    for tier in launcher.MODEL_SLOT_TIERS:
                        self.assertEqual(
                            settings["env"][f"ANTHROPIC_DEFAULT_{tier}_MODEL"],
                            "glm,glm-fixture",
                        )
                        self.assertEqual(
                            settings["env"][
                                f"ANTHROPIC_DEFAULT_{tier}_MODEL_SUPPORTED_CAPABILITIES"
                            ],
                            "thinking,adaptive_thinking,effort,xhigh_effort",
                        )
                    self.assertNotIn(
                        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
                        settings["env"],
                    )
                    self.assertEqual(
                        settings["env"]["CLAUDE_CODE_SUBAGENT_MODEL"],
                        "",
                    )
                    self.assertEqual(
                        settings["env"]["ANTHROPIC_CUSTOM_MODEL_OPTION"], ""
                    )

                    # Legacy live configs with local_token continue to work when
                    # the safer environment secret is not supplied.
                    hub_cfg = json.loads(config.read_text(encoding="utf-8"))
                    hub_cfg.pop("local_token_env")
                    hub_cfg["local_token"] = "fixture-local-token"
                    config.write_text(json.dumps(hub_cfg), encoding="utf-8")
                    os.environ["FIXTURE_HUB_TOKEN"] = ""
                    os.environ["CLAUDE_HUB_LOCAL_TOKEN"] = ""
                    self.assertEqual(launcher.exec_hub([]), 0)
                    legacy_launch = json.loads(
                        capture.read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        legacy_launch["settings"]["env"]["ANTHROPIC_AUTH_TOKEN"],
                        "fixture-local-token",
                    )
                    self.assertEqual(
                        legacy_launch["credential"], "fixture-local-token"
                    )
                self.assertEqual(sticky.read_text(encoding="utf-8"), "direct\n")

    def test_hub_resume_restores_the_session_channel_selector(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = isolated_env(
                home,
                CLAUDE1_HUB_PORT="18787",
                FIXTURE_HUB_TOKEN="fixture-local-token",
            )
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "port": 18787,
                        "local_token_env": "FIXTURE_HUB_TOKEN",
                        "default_channel": "glm",
                        "channels": {
                            "glm": {"provider": "GLM", "models": ["glm-fixture"]},
                            "grok": {"provider": "Grok", "models": ["grok-4.5"]},
                        },
                    }
                ),
                encoding="utf-8",
            )
            session_id = "5ffb0882-0050-4e71-9917-c76025876da8"
            project_key = str(Path.cwd().resolve()).replace("/", "-")
            transcript_dir = home / ".claude" / "projects" / project_key
            transcript_dir.mkdir(parents=True)
            (transcript_dir / f"{session_id}.jsonl").write_text(
                "\n".join(
                    (
                        json.dumps({"type": "assistant", "message": {"model": "glm-fixture"}}),
                        json.dumps({"type": "assistant", "message": {"model": "grok-4.5"}}),
                    )
                )
                + '\n{"type":"assistant"',
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

                settings_path = Path(sys.argv[sys.argv.index("--settings") + 1])
                Path(os.environ["FAKE_CLAUDE_CAPTURE"]).write_text(
                    json.dumps(
                        {
                            "argv": sys.argv,
                            "settings": json.loads(settings_path.read_text(encoding="utf-8")),
                        }
                    ),
                    encoding="utf-8",
                )
                """,
            )
            env.update(
                CLAUDE1_CLAUDE_BIN=str(fake_claude),
                FAKE_CLAUDE_CAPTURE=str(capture),
            )

            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher,
                    "ensure_hub",
                    side_effect=lambda port, **_kwargs: port,
                ):
                    self.assertEqual(
                        launcher.exec_hub(["--resume", session_id]),
                        0,
                    )

            launched = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(
                launched["settings"]["env"]["ANTHROPIC_MODEL"],
                "grok,grok-4.5",
            )
            self.assertEqual(launched["argv"][-2:], ["--resume", session_id])

    def test_hub_native_model_slots_route_fixture_and_other_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                channels = {
                    "fixture": {"models": ["k3-256k"]},
                    "n1": {"models": ["gpt-5.6-sol"]},
                }
                cfg = {
                    "model_slots": {
                        "fab" + "le": "fixture,k3-256k",
                        "opus": "n1,gpt-5.6-sol",
                        "sonnet": "n1,gpt-5.6-sol",
                        "haiku": "n1,gpt-5.6-sol",
                    }
                }

                slots = launcher._hub_model_slots(
                    cfg, channels, "n1,gpt-5.6-sol"
                )

        self.assertEqual(slots["FABLE"], "fixture,k3-256k")
        for tier in ("OPUS", "SONNET", "HAIKU"):
            self.assertEqual(slots[tier], "n1,gpt-5.6-sol")

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
        self.added: list[tuple] = []

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

    def addstr(self, *args):
        self.added.append(args)

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
            "models": ["claude-fixture-5[1m]", "claude-opus-4-6"],
        },
        "grok": {"provider": "Grok", "models": ["grok-4.5"]},
    },
}

HUB_V2_FIXTURE = {
    **HUB_FIXTURE,
    "version": 2,
    "launch_slot": "fable",
    "model_slots": {
        "fable": "any,claude-fixture-5[1m]",
        "opus": "grok,grok-4.5",
        "sonnet": "glm,glm-5.2",
        "haiku": "gpt,gpt-5.6-sol",
    },
    "effort_by_slot": {
        "fable": "xhigh",
        "opus": "high",
        "sonnet": "medium",
        "haiku": "low",
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

    def _multi_hub_env(self, home: Path) -> dict[str, str]:
        env = self._hub_env(home, write_config=False)
        root = home / ".cc-switch"
        hubs_dir = root / "hubs"
        hubs_dir.mkdir(parents=True, exist_ok=True)
        first = json.loads(json.dumps(HUB_V2_FIXTURE))
        first["port"] = 18787
        first["instance_id"] = "claude-hub1"
        first["local_token"] = "fixture-local-token"
        second = json.loads(json.dumps(HUB_V2_FIXTURE))
        second["port"] = 18788
        second["launch_slot"] = "sonnet"
        second["instance_id"] = "kimi-hub"
        second["local_token"] = "fixture-local-token"
        (root / "claude-hub.json").write_text(
            json.dumps(first), encoding="utf-8"
        )
        (hubs_dir / "kimi-hub.json").write_text(
            json.dumps(second), encoding="utf-8"
        )
        catalog = root / "claude-hubs.json"
        catalog.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default_hub": "claude-hub1",
                    "order": ["claude-hub1", "kimi-hub"],
                    "hubs": {
                        "claude-hub1": {
                            "name": "claude-hub1",
                            "config": "claude-hub.json",
                            "log": "logs/claude-hub.log",
                            "usage": "logs/claude-hub-usage.jsonl",
                        },
                        "kimi-hub": {
                            "name": "kimi-hub",
                            "config": "hubs/kimi-hub.json",
                            "log": "logs/hubs/kimi-hub.log",
                            "usage": "logs/hubs/kimi-hub-usage.jsonl",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        env["CLAUDE1_HUB_CATALOG"] = str(catalog)
        return env

    def test_build_hub_view_orders_default_first_and_derives_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                status, options = launcher.build_hub_view(HUB_V2_FIXTURE)
                self.assertEqual(status.channel_count, 4)
                self.assertEqual(status.model_count, 6)
                self.assertEqual(status.default_channel, "glm")
                self.assertEqual(status.default_model, "glm-5.2")
                self.assertEqual(
                    status.summary,
                    "4 槽 · 4 渠道 · 启动 any,claude-fixture-5[1m] · xhigh",
                )

                self.assertTrue(options[0].is_default)
                self.assertEqual(options[0].selector, "glm,glm-5.2")
                self.assertEqual(options[0].status_label, "回退")
                self.assertEqual(len(options), 6)

                by_selector = {opt.selector: opt for opt in options}
                self.assertEqual(
                    by_selector["gpt,gpt-5.6-sol"].family, "GPT / Codex"
                )
                self.assertEqual(by_selector["gpt,gpt-5.6-sol"].status_label, "代理")
                self.assertEqual(by_selector["any,claude-opus-4-6"].family, "Claude")
                self.assertEqual(by_selector["grok,grok-4.5"].family, "Grok")
                self.assertTrue(by_selector["any,claude-fixture-5[1m]"].is_1m)
                self.assertEqual(
                    by_selector["any,claude-fixture-5[1m]"].status_label, "1M"
                )

    def test_hub_display_uses_slot_count_and_distinct_route_terms(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                hub_ref = launcher.HubRef(
                    "fixture-hub",
                    "Fixture Hub",
                    Path("hub.json"),
                    Path("hub.log"),
                    Path("usage.jsonl"),
                )
                status = launcher.HubStatus(
                    port=18787,
                    default_channel="fast",
                    default_model="expected-model",
                    channel_count=1,
                    model_count=1,
                    launch_slot="fable",
                    launch_selector="other,start-model",
                    launch_effort="high",
                )
                ready = launcher.NamedHubLauncherState(
                    hub_ref,
                    launcher.HubLauncherState(status, (), ()),
                )
                setup = launcher.NamedHubLauncherState(hub_ref, None, 2)
                slots = (*launcher.HUB_SLOT_ORDER, "extra")

                with mock.patch.object(launcher, "HUB_SLOT_ORDER", slots):
                    ready_text = launcher._hub_home_text(ready, 120)
                    setup_text = launcher._hub_home_text(setup, 120)
                    setup_window = ScriptedWindow([], size=(18, 100))
                    launcher.C = {}
                    launcher._draw_hub_setup(
                        setup_window,
                        hub_ref,
                        {"mappings": {}},
                        0,
                    )
                    choice_window = ScriptedWindow([], size=(18, 100))
                    launcher._draw_hub_setup_choice_shell(
                        choice_window,
                        hub_ref.name,
                        "fable",
                        "选择渠道",
                    )

                setup_rendered = " ".join(
                    value
                    for call in setup_window.added + choice_window.added
                    for value in call
                    if isinstance(value, str)
                )
                self.assertIn("5 槽", ready_text)
                self.assertIn("启动 Fable", ready_text)
                self.assertIn("2/5", setup_text)
                self.assertIn("已完成 0/5", setup_rendered)
                self.assertIn("第 1/5 槽", setup_rendered)

                mismatch_window = ScriptedWindow([], size=(18, 100))
                launcher._draw_hub_workspace(
                    mismatch_window,
                    status,
                    [
                        launcher.HubChannel(
                            "fast", "Fixture", ("first-model",), False
                        )
                    ],
                    0,
                    tab="channels",
                )
                mismatch = " ".join(
                    value
                    for call in mismatch_window.added
                    for value in call
                    if isinstance(value, str)
                )
                self.assertIn("启动 other,start-model", mismatch)
                self.assertIn("可用", mismatch)
                self.assertNotIn("回退", mismatch)

                fallback_window = ScriptedWindow([], size=(18, 100))
                launcher._draw_hub_workspace(
                    fallback_window,
                    launcher.replace(status, default_model="first-model"),
                    [
                        launcher.HubChannel(
                            "fast", "Fixture", ("first-model",), False
                        )
                    ],
                    0,
                    tab="channels",
                )
                fallback = " ".join(
                    value
                    for call in fallback_window.added
                    for value in call
                    if isinstance(value, str)
                )
                self.assertIn("回退", fallback)

    def test_loading_v1_config_migrates_once_and_keeps_route_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            legacy = {
                **HUB_FIXTURE,
                "version": 1,
                "effort_level": "xhigh",
                "model_slots": {
                    "fable": "any,claude-fixture-5[1m]",
                    "opus": "grok,grok-4.5",
                    "sonnet": "glm,glm-5.2",
                    "haiku": "gpt,gpt-5.6-sol",
                },
                "future_field": {"preserved": True},
            }
            config.write_text(json.dumps(legacy), encoding="utf-8")
            config.chmod(0o600)

            with loaded_launcher(env) as launcher:
                migrated = launcher.load_hub_config(migrate=True)
                first_backups = list(config.parent.glob("hub.json.bak-migrate-*"))
                reloaded = launcher.load_hub_config(migrate=True)
                second_backups = list(config.parent.glob("hub.json.bak-migrate-*"))

            self.assertEqual(migrated, reloaded)
            self.assertEqual(migrated["version"], 2)
            self.assertEqual(migrated["default_channel"], "glm")
            self.assertEqual(migrated["launch_slot"], "fable")
            self.assertEqual(
                migrated["effort_by_slot"],
                {
                    "fable": "xhigh",
                    "opus": "high",
                    "sonnet": "high",
                    "haiku": "high",
                },
            )
            self.assertNotIn("effort_level", migrated)
            self.assertEqual(migrated["future_field"], {"preserved": True})
            self.assertEqual(len(first_backups), 1)
            self.assertEqual(second_backups, first_backups)
            self.assertEqual(json.loads(first_backups[0].read_text()), legacy)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(first_backups[0].stat().st_mode), 0o600)

    def test_v1_migration_accepts_legacy_aliases_and_normalizes_models(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home, write_config=False)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                json.dumps(
                    {
                        "default_channel": "1.legacy",
                        "channels": {
                            "1.legacy": {
                                "provider": "Legacy",
                                "models": [" spaced-model "],
                            },
                            "empty.channel": {"provider": "Empty", "models": []},
                        },
                    }
                ),
                encoding="utf-8",
            )
            config.chmod(0o600)

            with loaded_launcher(env) as launcher:
                migrated = launcher.load_hub_config(migrate=True)

            self.assertEqual(
                migrated["channels"]["1.legacy"]["models"], ["spaced-model"]
            )
            self.assertEqual(migrated["channels"]["empty.channel"]["models"], [])
            self.assertEqual(
                migrated["model_slots"]["fable"], "1.legacy,spaced-model"
            )

    @unittest.skipUnless(os.name == "posix", "POSIX lock-file safety")
    def test_hub_config_lock_refuses_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.chmod(0o600)
            target = home / "do-not-chmod"
            target.write_text("fixture", encoding="utf-8")
            target.chmod(0o644)
            config.with_name(config.name + ".lock").symlink_to(target)

            with loaded_launcher(env) as launcher:
                with self.assertRaises(OSError):
                    launcher.load_hub_config(migrate=True)

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    @unittest.skipUnless(os.name == "posix", "POSIX lock-file safety")
    def test_named_hub_start_lock_refuses_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                hub = launcher.resolve_hub_ref("kimi-hub")
                lock = hub.log_path.parent / f"claude-hub-{hub.hub_id}.lock"
                lock.parent.mkdir(parents=True, exist_ok=True)
                target = Path(raw_home) / "do-not-chmod"
                target.write_text("fixture", encoding="utf-8")
                target.chmod(0o644)
                lock.symlink_to(target)

                with self.assertRaises(OSError):
                    with launcher._hub_start_lock(hub):
                        pass

            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_catalog_migration_refuses_unrepresentable_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            catalog = home / "catalog-only" / "claude-hubs.json"
            env["CLAUDE1_HUB_CATALOG"] = str(catalog)

            with loaded_launcher(env) as launcher:
                with self.assertRaisesRegex(ValueError, "catalog 目录内"):
                    launcher.list_hub_refs(migrate=True)

            self.assertFalse(catalog.exists())

    def test_legacy_port_override_keeps_single_hub_mode(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home, write_config=False)
            env.pop("CLAUDE1_HUB_CONFIG")
            env.pop("CLAUDE1_HUB_LOG")
            env["CLAUDE1_HUB_PORT"] = "19999"
            config = home / ".cc-switch" / "claude-hub.json"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")

            with loaded_launcher(env) as launcher:
                ref = launcher.resolve_hub_ref()
                loaded = launcher.load_hub_config(hub=ref)
                resolved_port = launcher._hub_port(loaded)

            self.assertFalse(launcher.HUB_CATALOG_ENABLED)
            self.assertEqual(ref.config_path, config)
            self.assertEqual(resolved_port, 19999)

    def test_hub_port_rejects_boolean_and_fractional_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                for value in (True, 18787.5):
                    with self.subTest(value=value), self.assertRaisesRegex(
                        RuntimeError,
                        "hub 端口无效",
                    ):
                        launcher._hub_port({"port": value})

                self.assertEqual(launcher._hub_port({"port": "18787"}), 18787)

    def test_workspace_has_four_native_slots_then_only_unbound_models(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                slots, pool = launcher.build_hub_workspace(HUB_V2_FIXTURE)

            self.assertEqual([item.slot for item in slots], list(launcher.HUB_SLOT_ORDER))
            self.assertEqual(
                [item.selector for item in slots],
                [
                    "any,claude-fixture-5[1m]",
                    "grok,grok-4.5",
                    "glm,glm-5.2",
                    "gpt,gpt-5.6-sol",
                ],
            )
            self.assertEqual(
                [item.effort for item in slots], ["xhigh", "high", "medium", "low"]
            )
            self.assertEqual(
                [item.selector for item in pool],
                ["gpt,gpt-5.6-luna", "any,claude-opus-4-6"],
            )

    def test_exec_hub_slot_uses_that_slots_model_and_initial_effort(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            env["FIXTURE_HUB_TOKEN"] = "fixture-local-token"
            config = {
                **HUB_V2_FIXTURE,
                "local_token_env": "FIXTURE_HUB_TOKEN",
            }
            config_path = Path(env["CLAUDE1_HUB_CONFIG"])
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(
                        launcher,
                        "ensure_hub",
                        side_effect=lambda port, **_kwargs: port,
                    ),
                    mock.patch.object(
                        launcher, "launch_with_settings", return_value=0
                    ) as launch,
                ):
                    self.assertEqual(
                        launcher.exec_hub(["--slot", "sonnet", "-p", "hello"]),
                        0,
                    )

            settings, forwarded = launch.call_args.args
            self.assertEqual(forwarded, ["-p", "hello"])
            self.assertEqual(settings["env"]["ANTHROPIC_MODEL"], "glm,glm-5.2")
            self.assertEqual(settings["effortLevel"], "medium")
            self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", settings["env"])
            self.assertEqual(
                settings["env"][
                    "ANTHROPIC_DEFAULT_FABLE_MODEL_SUPPORTED_CAPABILITIES"
                ],
                "thinking,adaptive_thinking,effort,xhigh_effort",
            )
            self.assertEqual(
                settings["env"][
                    "ANTHROPIC_DEFAULT_SONNET_MODEL_SUPPORTED_CAPABILITIES"
                ],
                "thinking,adaptive_thinking,effort,xhigh_effort",
            )

    def test_exec_hub_uses_the_selected_named_hubs_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(
                        launcher,
                        "ensure_hub",
                        side_effect=lambda port, **_kwargs: port,
                    ) as ensure,
                    mock.patch.object(
                        launcher, "launch_with_settings", return_value=0
                    ) as launch,
                ):
                    self.assertEqual(
                        launcher.exec_hub(
                            ["--slot", "sonnet"], hub_id="kimi-hub"
                        ),
                        0,
                    )

            selected_hub = ensure.call_args.kwargs["hub"]
            self.assertEqual(ensure.call_args.args, (18788,))
            self.assertEqual(selected_hub.hub_id, "kimi-hub")
            self.assertEqual(
                selected_hub.config_path.name,
                "kimi-hub.json",
            )
            self.assertEqual(ensure.call_args.kwargs["instance_id"], "kimi-hub")
            settings, _forwarded = launch.call_args.args
            self.assertEqual(settings["env"]["ANTHROPIC_MODEL"], "glm,glm-5.2")
            self.assertEqual(settings["effortLevel"], "medium")

    def test_exec_hub_refuses_an_incomplete_named_hub(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                created = launcher.create_named_hub("pending-hub")
                with mock.patch.object(launcher, "ensure_hub") as ensure:
                    with self.assertRaisesRegex(RuntimeError, "尚未配置"):
                        launcher.exec_hub([], hub_id=created.hub_id)

            ensure.assert_not_called()

    def test_usage_aggregates_every_named_hubs_usage_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            now = time.time()
            with loaded_launcher(env) as launcher:
                for index, hub in enumerate(launcher.list_hub_refs()):
                    hub.usage_path.parent.mkdir(parents=True, exist_ok=True)
                    hub.usage_path.write_text(
                        json.dumps(
                            {
                                "ts": now - index,
                                "in": 10,
                                "out": 2,
                                "cr": 0,
                                "cw": 0,
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(launcher.cli_usage(["--day"]), 0)

            self.assertIn("请求数        2", output.getvalue())
            self.assertIn("输入 token    20  (20)", output.getvalue())

    def test_exec_hub_model_inherits_unique_slot_effort_and_forwards_cli_effort(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            env["FIXTURE_HUB_TOKEN"] = "fixture-local-token"
            config = {
                **HUB_V2_FIXTURE,
                "local_token_env": "FIXTURE_HUB_TOKEN",
            }
            path = Path(env["CLAUDE1_HUB_CONFIG"])
            path.write_text(json.dumps(config), encoding="utf-8")
            path.chmod(0o600)
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(
                        launcher,
                        "ensure_hub",
                        side_effect=lambda port, **_kwargs: port,
                    ),
                    mock.patch.object(
                        launcher, "launch_with_settings", return_value=0
                    ) as launch,
                ):
                    launcher.exec_hub(
                        [
                            "--model",
                            "any,claude-fixture-5[1m]",
                            "--effort",
                            "low",
                        ]
                    )

            settings, forwarded = launch.call_args.args
            self.assertEqual(settings["effortLevel"], "xhigh")
            self.assertEqual(forwarded, ["--effort", "low"])
            self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", settings["env"])

    def test_resume_prefers_launch_slot_effort_when_selectors_are_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            env["FIXTURE_HUB_TOKEN"] = "fixture-local-token"
            selector = "glm,glm-5.2"
            config = {
                **HUB_V2_FIXTURE,
                "local_token_env": "FIXTURE_HUB_TOKEN",
                "model_slots": {
                    slot: selector
                    for slot in ("fable", "opus", "sonnet", "haiku")
                },
            }
            path = Path(env["CLAUDE1_HUB_CONFIG"])
            path.write_text(json.dumps(config), encoding="utf-8")
            path.chmod(0o600)
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(
                        launcher,
                        "ensure_hub",
                        side_effect=lambda port, **_kwargs: port,
                    ),
                    mock.patch.object(
                        launcher, "_resume_session_selector", return_value=selector
                    ),
                    mock.patch.object(
                        launcher, "launch_with_settings", return_value=0
                    ) as launch,
                ):
                    self.assertEqual(launcher.exec_hub(["--continue"]), 0)

            settings, _args = launch.call_args.args
            self.assertEqual(settings["effortLevel"], "xhigh")

    def test_hub_model_family_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                self.assertEqual(launcher._hub_model_family("kimi-k3"), "Kimi")
                self.assertEqual(launcher._hub_model_family("k3-256k"), "Kimi")
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
        cfg = {"providers": {"alpha-id": {"name": "Alpha", "hidden": False}}}
        window = ScriptedWindow(keys)
        launcher._logo_pairs[:] = [0]
        with (
            mock.patch.object(launcher, "_init_colors", return_value={}),
            mock.patch.object(launcher, "_intro", return_value=None),
            mock.patch.object(launcher, "load_mru", return_value={}),
            mock.patch.object(launcher, "hub_healthy", return_value=True),
        ):
            return launcher._launcher_main(window, cfg, {"alpha-id"})

    def test_enter_launches_configured_slot_without_opening_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            with loaded_launcher(env) as launcher:
                result = self._run_home(launcher, [10])
                self.assertIsInstance(result, launcher.HubLaunch)
                self.assertEqual(result.slot, "fable")
                self.assertIsNone(result.option)

    def test_home_lists_named_hubs_without_models_and_launches_selected_hub(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            window = ScriptedWindow([ord("j"), 10], size=(30, 120))
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            with loaded_launcher(env) as launcher:
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                    mock.patch.object(launcher, "hub_healthy", return_value=True),
                ):
                    result = launcher._launcher_main(
                        window, cfg, {"alpha-id"}
                    )

            self.assertIsInstance(result, launcher.HubLaunch)
            self.assertEqual(result.hub_id, "kimi-hub")
            self.assertEqual(result.slot, "sonnet")
            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIn("claude-hub1", rendered)
            self.assertIn("kimi-hub", rendered)
            self.assertNotIn("gpt-5.6-sol", rendered)
            self.assertNotIn("claude-fixture-5[1m]", rendered)

    def test_home_add_creates_empty_hub_and_opens_first_mapping_setup(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._multi_hub_env(home)
            window = ScriptedWindow(
                [ord("a"), *map(ord, "any-hub"), 10, 27, ord("q")],
                size=(30, 120),
            )
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            with loaded_launcher(env) as launcher:
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                ):
                    result = launcher._launcher_main(
                        window, cfg, {"alpha-id"}
                    )
                catalog = launcher.load_hub_catalog()
                created = launcher.resolve_hub_ref("any-hub")
                draft = json.loads(created.draft_path.read_text(encoding="utf-8"))

            self.assertIsNone(result)
            self.assertEqual(catalog["order"][-1], "any-hub")
            self.assertEqual(created.name, "any-hub")
            self.assertEqual(created.state, "setup")
            self.assertFalse(created.config_path.exists())
            self.assertEqual(draft, {"version": 1, "mappings": {}})
            self.assertEqual(created.draft_path.stat().st_mode & 0o777, 0o600)
            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIn("首次设置", rendered)
            self.assertIn("0/4", rendered)
            self.assertIn("Fable", rendered)
            self.assertNotIn("glm-5.2", rendered)
            self.assertNotIn("gpt-5.6-sol", rendered)

    def test_home_n_creates_the_first_hub_without_a_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home, write_config=False)
            env["CLAUDE1_HUB_CATALOG"] = str(
                home / ".cc-switch" / "claude-hubs.json"
            )
            window = ScriptedWindow(
                [ord("n"), *map(ord, "first-hub"), 10, 27, ord("q")],
                size=(18, 90),
            )
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            with loaded_launcher(env) as launcher:
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                ):
                    result = launcher._launcher_main(
                        window, cfg, {"alpha-id"}
                    )
                catalog = launcher.load_hub_catalog()
                created = launcher.resolve_hub_ref("first-hub")

            self.assertIsNone(result)
            self.assertEqual(catalog["default_hub"], "first-hub")
            self.assertEqual(catalog["order"], ["first-hub"])
            self.assertEqual(created.state, "setup")
            self.assertFalse(created.config_path.exists())
            self.assertTrue(created.draft_path.is_file())

    def test_enter_on_incomplete_hub_resumes_first_unmapped_slot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            window = ScriptedWindow(
                [ord("j"), ord("j"), 10, 27, ord("q")],
                size=(30, 120),
            )
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            provider = {
                "id": "fable-provider-id",
                "name": "Fable Provider",
            }
            with loaded_launcher(env) as launcher:
                created = launcher.create_named_hub("draft-hub")
                launcher.set_hub_setup_mapping(
                    created,
                    "fable",
                    provider,
                    "fable-model",
                    api_format="anthropic",
                )
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                ):
                    result = launcher._launcher_main(
                        window, cfg, {"alpha-id"}
                    )
                persisted = launcher.load_hub_setup_draft(created)

            self.assertIsNone(result)
            self.assertEqual(
                persisted["mappings"]["fable"]["model"], "fable-model"
            )
            rendered_strings = [
                value
                for call in window.added
                for value in call
                if isinstance(value, str)
            ]
            rendered = " ".join(rendered_strings)
            self.assertIn("已完成 1/4", rendered)
            self.assertTrue(any("▸ ◆ Opus" in value for value in rendered_strings))
            self.assertIn("未设置", rendered)

    def test_corrupt_setup_draft_remains_visible_as_an_error_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                created = launcher.create_named_hub("broken-setup")
                assert created.draft_path is not None
                created.draft_path.write_text("{", encoding="utf-8")

                states = launcher._load_named_hub_launcher_states()
                broken = next(
                    item for item in states if item.hub.hub_id == created.hub_id
                )
                rendered = launcher._hub_home_text(broken, 80)
                window = ScriptedWindow([10, ord("q")])
                cfg = {
                    "providers": {
                        "alpha-id": {"name": "Alpha", "hidden": False}
                    }
                }
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                    mock.patch.object(
                        launcher,
                        "_load_named_hub_launcher_states",
                        return_value=[broken],
                    ),
                    mock.patch.object(launcher, "_hub_setup_wizard") as wizard,
                ):
                    result = launcher._launcher_main(
                        window, cfg, {"alpha-id"}
                    )

            self.assertIsNone(broken.state)
            self.assertEqual(broken.error, "配置异常")
            self.assertIn("配置异常", rendered)
            self.assertIsNone(result)
            wizard.assert_not_called()

    def test_setup_overview_keeps_all_slots_and_finish_visible_at_eight_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                created = launcher.create_named_hub("compact-setup")
                window = ScriptedWindow([], size=(8, 80))
                launcher.C = {}
                launcher._draw_hub_setup(
                    window,
                    created,
                    launcher.load_hub_setup_draft(created),
                    0,
                )

            writes = [
                (call[0], call[2])
                for call in window.added
                if len(call) >= 3
                and isinstance(call[0], int)
                and isinstance(call[2], str)
            ]
            rendered = " ".join(text for _row, text in writes)
            for label in ("Fable", "Opus", "Sonnet", "Haiku", "完成配置"):
                self.assertIn(label, rendered)
            finish_row = next(row for row, text in writes if "完成配置" in text)
            footer_row = next(row for row, text in writes if "Esc 稍后设置" in text)
            self.assertLess(finish_row, footer_row)

    def test_setup_provider_picker_scrolls_above_footer_at_eight_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            providers = [
                {"id": f"provider-{index}", "name": f"Provider {index}"}
                for index in range(7)
            ]
            window = ScriptedWindow(
                [ord("j"), ord("j"), ord("j"), ord("j"), 10],
                size=(8, 80),
            )
            with loaded_launcher(env) as launcher:
                launcher.C = {}
                selected = launcher._choose_hub_setup_provider(
                    window,
                    "Compact Hub",
                    "fable",
                    providers,
                )

            self.assertEqual(selected["id"], "provider-4")
            selected_rows = [
                call[0]
                for call in window.added
                if len(call) >= 3
                and isinstance(call[2], str)
                and "▸ Provider 4" in call[2]
            ]
            self.assertTrue(selected_rows)
            self.assertTrue(all(row < 7 for row in selected_rows))

    def test_setup_provider_picker_starts_from_initial_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            providers = [
                {"id": "provider-a", "name": "Provider A"},
                {"id": "provider-b", "name": "Provider B"},
            ]
            window = ScriptedWindow([10])
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                launcher.C = {}
                selected = launcher._choose_hub_setup_provider(
                    window,
                    "Fixture Hub",
                    "fable",
                    providers,
                    initial_provider_id="provider-b",
                )

            self.assertEqual(selected, providers[1])

    def test_api_format_pickers_share_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = isolated_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                launcher.C = {}
                for shortcut, expected in (
                    ("a", "anthropic"),
                    ("c", "openai_chat"),
                    ("r", "openai_responses"),
                ):
                    with self.subTest(shortcut=shortcut):
                        add_window = ScriptedWindow([ord(shortcut)])
                        setup_window = ScriptedWindow([ord(shortcut)])

                        self.assertEqual(
                            launcher._choose_hub_api_format(add_window),
                            expected,
                        )
                        self.assertEqual(
                            launcher._choose_hub_setup_api_format(
                                setup_window,
                                "Fixture Hub",
                                "fable",
                                "metadata unavailable",
                            ),
                            expected,
                        )

            setup_text = " ".join(
                str(value)
                for call in setup_window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIn("a/c/r 快捷选择", setup_text)

    def test_choice_pickers_keep_enter_and_escape_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            providers = [{"id": "provider-a", "name": "Provider A"}]
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                launcher.C = {}
                for key, expected_provider, expected_format in (
                    (10, providers[0], "anthropic"),
                    (27, None, None),
                ):
                    with self.subTest(key=key):
                        self.assertEqual(
                            launcher._choose_hub_provider(
                                ScriptedWindow([key]), providers
                            ),
                            expected_provider,
                        )
                        self.assertEqual(
                            launcher._choose_hub_setup_provider(
                                ScriptedWindow([key]),
                                "Fixture Hub",
                                "fable",
                                providers,
                            ),
                            expected_provider,
                        )
                        self.assertEqual(
                            launcher._choose_hub_api_format(
                                ScriptedWindow([key])
                            ),
                            expected_format,
                        )
                        self.assertEqual(
                            launcher._choose_hub_setup_api_format(
                                ScriptedWindow([key]),
                                "Fixture Hub",
                                "fable",
                                "metadata unavailable",
                            ),
                            expected_format,
                        )

    def test_manual_setup_model_explains_that_commas_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            window = ScriptedWindow(
                [*map(ord, "bad,model"), 10, 27],
                size=(18, 90),
            )
            with loaded_launcher(isolated_env(Path(raw_home))) as launcher:
                launcher.C = {}
                result = launcher._prompt_hub_setup_model(
                    window,
                    "Fixture Hub",
                    "fable",
                    "Fixture Provider",
                )

            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIsNone(result)
            self.assertIn("不能包含逗号", rendered)

    def test_completing_fourth_mapping_makes_hub_launchable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            window = ScriptedWindow(
                [
                    ord("j"),
                    ord("j"),
                    10,  # pending Hub -> setup
                    10,  # set the selected Haiku row
                    10,  # choose the only provider for Haiku
                    10,  # choose its exact model
                    10,  # finish the now-complete setup
                    10,  # launch from home
                ],
                size=(30, 120),
            )
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            final_provider = {
                "id": "haiku-provider-id",
                "name": "Haiku Provider",
                "settings_config": json.dumps(
                    {"env": {"ANTHROPIC_MODEL": "haiku-model"}}
                ),
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            with loaded_launcher(env) as launcher:
                created = launcher.create_named_hub("ready-after-setup")
                for slot in ("fable", "opus", "sonnet"):
                    launcher.set_hub_setup_mapping(
                        created,
                        slot,
                        {"id": f"{slot}-provider-id", "name": f"{slot} Provider"},
                        f"{slot}-model",
                        api_format="anthropic",
                    )
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                    mock.patch.object(
                        launcher, "list_providers", return_value=[final_provider]
                    ),
                ):
                    result = launcher._launcher_main(
                        window, cfg, {"alpha-id"}
                    )
                ready = launcher.resolve_hub_ref("ready-after-setup")
                config = launcher.load_hub_config(hub=ready)
                catalog = launcher.load_hub_catalog()

            self.assertIsInstance(result, launcher.HubLaunch)
            self.assertEqual(result.hub_id, "ready-after-setup")
            self.assertEqual(result.slot, "fable")
            self.assertEqual(ready.state, "ready")
            self.assertIsNone(ready.draft_path)
            self.assertTrue(ready.config_path.is_file())
            self.assertEqual(
                config["model_slots"],
                {
                    "fable": "fable-provider,fable-model",
                    "opus": "opus-provider,opus-model",
                    "sonnet": "sonnet-provider,sonnet-model",
                    "haiku": "haiku-provider,haiku-model",
                },
            )
            self.assertEqual(
                {
                    alias: channel["models"]
                    for alias, channel in config["channels"].items()
                },
                {
                    "fable-provider": ["fable-model"],
                    "opus-provider": ["opus-model"],
                    "sonnet-provider": ["sonnet-model"],
                    "haiku-provider": ["haiku-model"],
                },
            )
            self.assertEqual(catalog["hubs"][ready.hub_id]["state"], "ready")
            self.assertNotIn("draft", catalog["hubs"][ready.hub_id])

    def test_setup_wizard_stays_open_when_its_hub_was_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            window = ScriptedWindow([10, 27], size=(18, 90))
            with loaded_launcher(env) as launcher:
                stale = launcher.create_named_hub("stale-hub")
                provider = {"id": "fixture-provider", "name": "Fixture Provider"}
                for slot in launcher.HUB_SLOT_ORDER:
                    launcher.set_hub_setup_mapping(
                        stale,
                        slot,
                        provider,
                        f"{slot}-model",
                        api_format="anthropic",
                    )

                def remove_stale(catalog):
                    catalog["order"].remove(stale.hub_id)
                    del catalog["hubs"][stale.hub_id]

                launcher.mutate_hub_catalog(remove_stale)
                launcher.C = {}
                outcome = launcher._hub_setup_wizard(window, stale)

            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertEqual(outcome, "back")
            self.assertIn("已被移除", rendered)

    def test_failed_catalog_write_removes_the_new_hub_draft(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                real_write = launcher._atomic_private_write

                def fail_catalog(path, text):
                    if path == launcher.HUB_CATALOG:
                        raise OSError("fixture catalog failure")
                    return real_write(path, text)

                with mock.patch.object(
                    launcher,
                    "_atomic_private_write",
                    side_effect=fail_catalog,
                ):
                    with self.assertRaisesRegex(OSError, "catalog failure"):
                        launcher.create_named_hub("orphan-hub")

                catalog = launcher.load_hub_catalog()
                orphan = (
                    launcher.HUB_CATALOG.parent
                    / "hubs"
                    / "orphan-hub.setup.json"
                )

            self.assertNotIn("orphan-hub", catalog["hubs"])
            self.assertFalse(orphan.exists())

    def test_failed_setup_promotion_keeps_draft_and_removes_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                created = launcher.create_named_hub("retry-setup")
                for slot in launcher.HUB_SLOT_ORDER:
                    launcher.set_hub_setup_mapping(
                        created,
                        slot,
                        {"id": f"{slot}-id", "name": slot},
                        f"{slot}-model",
                        api_format="anthropic",
                    )
                real_write = launcher._atomic_private_write

                def fail_catalog(path, text):
                    if path == launcher.HUB_CATALOG:
                        raise OSError("fixture promotion failure")
                    return real_write(path, text)

                with mock.patch.object(
                    launcher,
                    "_atomic_private_write",
                    side_effect=fail_catalog,
                ):
                    with self.assertRaisesRegex(OSError, "promotion failure"):
                        launcher.complete_hub_setup(created)

                catalog = launcher.load_hub_catalog()
                persisted = launcher.load_hub_setup_draft(created)

            self.assertEqual(
                catalog["hubs"][created.hub_id]["state"], "setup"
            )
            self.assertEqual(len(persisted["mappings"]), 4)
            self.assertTrue(created.draft_path.is_file())
            self.assertFalse(created.config_path.exists())

    def test_setup_promotion_recovers_a_config_left_before_catalog_write(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                created = launcher.create_named_hub("recover-setup")
                for slot in launcher.HUB_SLOT_ORDER:
                    launcher.set_hub_setup_mapping(
                        created,
                        slot,
                        {"id": f"{slot}-id", "name": slot},
                        f"{slot}-model",
                        api_format="anthropic",
                    )
                draft = launcher.load_hub_setup_draft(created)
                ready_configs = {
                    ref.hub_id: launcher.load_hub_config(hub=ref)
                    for ref in launcher.list_hub_refs()
                    if ref.state == "ready"
                }
                port = launcher._next_hub_port(ready_configs, 18787)
                interrupted_config = launcher._hub_config_from_setup_draft(
                    created, draft, port
                )
                interrupted_config["future_field"] = {"preserved": True}
                launcher._atomic_private_write(
                    created.config_path,
                    launcher._hub_config_text(interrupted_config),
                )

                ready, recovered = launcher.complete_hub_setup(created)
                catalog = launcher.load_hub_catalog()

            self.assertEqual(ready.state, "ready")
            self.assertEqual(recovered, interrupted_config)
            self.assertEqual(recovered["future_field"], {"preserved": True})
            self.assertEqual(catalog["hubs"][ready.hub_id]["state"], "ready")
            self.assertFalse(created.draft_path.exists())

    def test_setup_recovery_reassigns_a_port_taken_after_the_crash(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                crashed = launcher.create_named_hub("crashed-setup")
                other = launcher.create_named_hub("completed-later")
                for hub in (crashed, other):
                    for slot in launcher.HUB_SLOT_ORDER:
                        launcher.set_hub_setup_mapping(
                            hub,
                            slot,
                            {"id": f"{hub.hub_id}-{slot}", "name": slot},
                            f"{slot}-model",
                            api_format="anthropic",
                        )
                ready_configs = {
                    ref.hub_id: launcher.load_hub_config(hub=ref)
                    for ref in launcher.list_hub_refs()
                    if ref.state == "ready"
                }
                crashed_port = launcher._next_hub_port(ready_configs, 18787)
                crashed_config = launcher._hub_config_from_setup_draft(
                    crashed,
                    launcher.load_hub_setup_draft(crashed),
                    crashed_port,
                )
                launcher._atomic_private_write(
                    crashed.config_path,
                    launcher._hub_config_text(crashed_config),
                )

                _other_ref, other_config = launcher.complete_hub_setup(other)
                recovered_ref, recovered_config = launcher.complete_hub_setup(
                    crashed
                )

            self.assertEqual(other_config["port"], crashed_port)
            self.assertNotEqual(recovered_config["port"], crashed_port)
            self.assertEqual(recovered_ref.state, "ready")
            self.assertEqual(
                recovered_config["local_token"], crashed_config["local_token"]
            )

    def test_named_hub_launch_reassigns_a_port_taken_after_setup(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            script = Path(env["CLAUDE1_HUB_SCRIPT"])
            script.parent.mkdir(parents=True, exist_ok=True)
            write_executable(script, "#!/bin/sh\nexit 0\n")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as thief:
                thief.bind(("127.0.0.1", 0))
                thief.listen()
                stolen_port = int(thief.getsockname()[1])

                with loaded_launcher(env) as launcher:
                    hub = launcher.resolve_hub_ref("kimi-hub")

                    def steal_configured_port(config):
                        config["port"] = stolen_port

                    launcher.mutate_hub_config(
                        steal_configured_port,
                        hub=hub,
                    )
                    process = mock.Mock()
                    process.poll.return_value = None
                    spawned: dict[str, object] = {}

                    def capture_spawn(log_path, child_env, listener):
                        spawned["log_path"] = log_path
                        spawned["port"] = int(listener.getsockname()[1])
                        spawned["env_port"] = child_env["CLAUDE_HUB_PORT"]
                        return process

                    with mock.patch.object(
                        launcher,
                        "hub_healthy",
                        side_effect=[False, False, False, True],
                    ), mock.patch.object(
                        launcher,
                        "_spawn_hub_process",
                        side_effect=capture_spawn,
                    ), mock.patch.object(
                        launcher,
                        "launch_with_settings",
                        return_value=0,
                    ) as launch:
                        status = launcher.exec_hub(
                            [],
                            hub_id=hub.hub_id,
                        )

                    persisted = launcher.load_hub_config(hub=hub)
                    settings = launch.call_args.args[0]
                    launcher._hub_processes.clear()

            actual_port = spawned["port"]
            self.assertEqual(status, 0)
            self.assertNotEqual(actual_port, stolen_port)
            self.assertEqual(persisted["port"], actual_port)
            self.assertEqual(spawned["env_port"], str(actual_port))
            self.assertEqual(
                settings["env"]["ANTHROPIC_BASE_URL"],
                f"http://127.0.0.1:{actual_port}",
            )

    def test_create_rejects_a_symlinked_catalog_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._multi_hub_env(home)
            root = home / ".cc-switch"
            catalog_path = root / "claude-hubs.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["order"] = ["claude-hub1"]
            catalog["hubs"] = {
                "claude-hub1": catalog["hubs"]["claude-hub1"]
            }
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            kimi_config = root / "hubs" / "kimi-hub.json"
            kimi_config.unlink()
            kimi_config.parent.rmdir()
            outside = home / "outside"
            outside.mkdir()
            (root / "hubs").symlink_to(outside, target_is_directory=True)

            with loaded_launcher(env) as launcher:
                with self.assertRaisesRegex(ValueError, "符号链接"):
                    launcher.create_named_hub("escape-hub")

            self.assertFalse((outside / "escape-hub.setup.json").exists())

    def test_renaming_hub_preserves_its_stable_identity_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._multi_hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                before = launcher.resolve_hub_ref("kimi-hub")
                renamed = launcher.rename_named_hub(
                    "kimi-hub", "Kimi Research Hub"
                )
                resolved = launcher.resolve_hub_ref("kimi research hub")

            self.assertEqual(renamed.hub_id, "kimi-hub")
            self.assertEqual(resolved.hub_id, "kimi-hub")
            self.assertEqual(resolved.config_path, before.config_path)
            self.assertEqual(resolved.log_path, before.log_path)

    def test_home_down_moves_from_the_only_hub_to_direct_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            with loaded_launcher(env) as launcher:
                result = self._run_home(launcher, [ord("j"), 10])

            self.assertEqual(result, "alpha-id")

    def test_home_effort_key_updates_the_selected_hub_slot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            with loaded_launcher(env) as launcher:
                self._run_home(
                    launcher,
                    [ord("\t"), ord("j"), ord("e"), 27, ord("q")],
                )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(persisted["effort_by_slot"]["opus"], "xhigh")
            self.assertEqual(persisted["effort_by_slot"]["fable"], "xhigh")

    def test_tab_opens_slots_and_enter_preserves_the_selected_slot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            with loaded_launcher(env) as launcher:
                result = self._run_home(
                    launcher, [ord("\t"), ord("j"), 10]
                )
            self.assertIsInstance(result, launcher.HubLaunch)
            self.assertEqual(result.slot, "opus")
            self.assertIsNone(result.option)

    def test_tab_round_trip_restores_the_previous_slot_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            with loaded_launcher(env) as launcher:
                result = self._run_home(
                    launcher,
                    [
                        ord("\t"),
                        ord("j"),
                        ord("\t"),
                        ord("\t"),
                        10,
                    ],
                )

            self.assertIsInstance(result, launcher.HubLaunch)
            self.assertEqual(result.slot, "opus")

    def test_workspace_effort_key_persists_the_highlighted_slot(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            with loaded_launcher(env) as launcher:
                result = self._run_home(
                    launcher,
                    [ord("\t"), ord("e"), 27, ord("q")],
                )

            self.assertIsNone(result)
            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(persisted["effort_by_slot"]["fable"], "low")
            self.assertEqual(persisted["effort_by_slot"]["opus"], "high")

    def test_workspace_effort_conflict_reloads_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            with loaded_launcher(env) as launcher:
                real_mutate = launcher.mutate_hub_config
                first = True

                def racing_mutate(mutator):
                    nonlocal first
                    if first:
                        first = False
                        external = json.loads(config.read_text(encoding="utf-8"))
                        external["effort_by_slot"]["fable"] = "medium"
                        config.write_text(json.dumps(external), encoding="utf-8")
                        config.chmod(0o600)
                        raise ValueError("槽位 effort 已被另一窗口修改，请重试")
                    return real_mutate(mutator)

                with mock.patch.object(
                    launcher, "mutate_hub_config", side_effect=racing_mutate
                ):
                    self._run_home(
                        launcher,
                        [ord("\t"), ord("e"), ord("e"), 27, ord("q")],
                    )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(persisted["effort_by_slot"]["fable"], "high")

    def test_channels_tab_launches_the_highlighted_channels_first_model(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            with loaded_launcher(env) as launcher:
                result = self._run_home(
                    launcher,
                    [ord("\t"), ord("\t"), 10],
                )

            self.assertIsInstance(result, launcher.HubLaunch)
            self.assertIsNone(result.slot)
            self.assertEqual(result.option.selector, "glm,glm-5.2")

    def test_pool_binding_updates_one_slot_and_removes_model_from_pool(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            with loaded_launcher(env) as launcher:
                result = self._run_home(
                    launcher,
                    [
                        ord("\t"),
                        ord("j"), ord("j"), ord("j"), ord("j"),
                        ord("b"), ord("h"), ord("y"), 10,
                        27, ord("q"),
                    ],
                )
                persisted = launcher.load_hub_config()
                _slots, pool = launcher.build_hub_workspace(persisted)

            self.assertIsNone(result)
            self.assertEqual(
                persisted["model_slots"]["haiku"], "gpt,gpt-5.6-luna"
            )
            self.assertNotIn(
                "gpt,gpt-5.6-luna", [option.selector for option in pool]
            )

    def test_adding_channel_persists_stable_provider_id_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {
                "id": "provider-stable-id",
                "name": "Renamable Provider",
                "settings_config": json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_MODEL": "new-model",
                            "ANTHROPIC_AUTH_TOKEN": "fixture-private-value",
                        }
                    }
                ),
                "meta": "{}",
            }
            with loaded_launcher(env) as launcher:
                updated = launcher.add_hub_channel(
                    provider,
                    alias="new_channel",
                    models=["new-model"],
                )

            self.assertEqual(
                updated["channels"]["new_channel"],
                {"provider": "id:provider-stable-id", "models": ["new-model"]},
            )
            serialized = config.read_text(encoding="utf-8")
            self.assertNotIn("fixture-private-value", serialized)

    def test_hub_provider_picker_scrolls_to_the_selected_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            providers = [
                {"id": f"p{i}", "name": f"Provider {i}"} for i in range(6)
            ]
            window = ScriptedWindow(
                [ord("j"), ord("j"), 10],
                size=(6, 80),
            )
            with loaded_launcher(env) as launcher:
                launcher.C = {}
                selected = launcher._choose_hub_provider(window, providers)

            self.assertEqual(selected["id"], "p2")
            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIn("▸ Provider 2", rendered)

    def test_compact_hub_home_keeps_one_provider_visible(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            window = ScriptedWindow([], size=(8, 80))
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            with loaded_launcher(env) as launcher:
                launcher.C = {}
                launcher._logo_pairs[:] = [0]
                status, _options = launcher.build_hub_view(HUB_V2_FIXTURE)
                state = launcher.build_hub_launcher_state(HUB_V2_FIXTURE)
                named = launcher.NamedHubLauncherState(
                    launcher._legacy_hub_ref(), state
                )
                self.assertLessEqual(
                    sum(launcher._hub_columns(32)) + 4,
                    32 - 4,
                )
                launcher._draw_launcher(
                    window,
                    cfg,
                    ["alpha-id"],
                    0,
                    False,
                    {},
                    hub_focus=True,
                    hubs=[named],
                )

            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIn("Alpha", rendered)

    def test_home_visibly_exposes_all_hub_controls(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            window = ScriptedWindow([ord("q")], size=(30, 120))
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            with loaded_launcher(env) as launcher:
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                ):
                    self.assertIsNone(
                        launcher._launcher_main(window, cfg, {"alpha-id"})
                    )

            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIn("Hub 工作区 · 1 个", rendered)
            self.assertIn("Claude-Hub", rendered)
            self.assertNotIn("gpt-5.6-sol", rendered)
            self.assertNotIn("claude-fixture-5[1m]", rendered)
            self.assertIn("n 新建 Hub", rendered)
            self.assertIn("m/→ 管理", rendered)
            self.assertIn("Alpha", rendered)

    def test_expanded_hub_home_uses_compact_logo_in_medium_height_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            window = ScriptedWindow([], size=(18, 90))
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            with loaded_launcher(env) as launcher:
                launcher.C = {}
                launcher._logo_pairs[:] = [0]
                state = launcher.build_hub_launcher_state(HUB_V2_FIXTURE)
                named = launcher.NamedHubLauncherState(
                    launcher._legacy_hub_ref(), state
                )
                launcher._draw_launcher(
                    window,
                    cfg,
                    ["alpha-id"],
                    0,
                    False,
                    {},
                    hub_focus=True,
                    hubs=[named],
                )

            rendered_strings = [
                value
                for call in window.added
                for value in call
                if isinstance(value, str)
            ]
            self.assertIn("◤ claude1 ◢", rendered_strings)
            self.assertNotIn("█", rendered_strings)

    def test_workspace_failure_is_shown_and_home_reloads_after_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            window = ScriptedWindow(
                [
                    # Enter workspace, Channels, try deleting fallback, then back.
                    # The confirmation consumes y.
                    ord("\t"), ord("\t"), ord("d"), ord("y"), 10, 27, ord("q")
                ]
            )
            with loaded_launcher(env) as launcher:
                cfg = {
                    "providers": {
                        "alpha-id": {"name": "Alpha", "hidden": False}
                    }
                }
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                    mock.patch.object(launcher, "hub_healthy", return_value=True),
                    mock.patch.object(
                        launcher,
                        "_load_named_hub_launcher_states",
                        wraps=launcher._load_named_hub_launcher_states,
                    ) as load_state,
                ):
                    self.assertIsNone(
                        launcher._launcher_main(window, cfg, {"alpha-id"})
                    )

            rendered = " ".join(
                str(value)
                for call in window.added
                for value in call
                if isinstance(value, str)
            )
            self.assertIn("fallback", rendered)
            self.assertGreaterEqual(load_state.call_count, 2)

    def test_channels_add_wizard_uses_provider_defaults_and_adds_to_pool(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {
                "id": "new-provider-id",
                "name": "New Provider",
                "settings_config": json.dumps(
                    {"env": {"ANTHROPIC_MODEL": "new-model"}}
                ),
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher, "list_providers", return_value=[provider]
                ):
                    result = self._run_home(
                        launcher,
                        [
                            ord("\t"),
                            ord("\t"), ord("a"),
                            10,  # provider
                            10,  # generated alias
                            10,  # provider model
                            10,  # confirm add
                            27, ord("q"),
                        ],
                    )

            self.assertIsNone(result)
            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["channels"]["new-provider"],
                {"provider": "id:new-provider-id", "models": ["new-model"]},
            )

    def test_home_add_key_opens_the_channel_wizard_directly(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {
                "id": "home-provider-id",
                "name": "Home Provider",
                "settings_config": json.dumps(
                    {"env": {"ANTHROPIC_MODEL": "home-model"}}
                ),
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher, "list_providers", return_value=[provider]
                ):
                    self._run_home(
                        launcher,
                        [
                            ord("a"),
                            10,
                            10,
                            10,
                            10,  # confirm add
                            ord("q"),
                        ],
                    )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["channels"]["home-provider"],
                {"provider": "id:home-provider-id", "models": ["home-model"]},
            )

    def test_add_wizard_can_choose_a_nondefault_provider_model(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {
                "id": "multi-model-id",
                "name": "Multi Model",
                "settings_config": json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_MODEL": "model-default",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL": "model-preferred",
                        }
                    }
                ),
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher, "list_providers", return_value=[provider]
                ):
                    self._run_home(
                        launcher,
                        [
                            ord("a"),
                            10,  # provider
                            ord(" "),  # deselect default model
                            ord("j"), 10,  # continue with second model selected
                            10,  # generated alias
                            10,  # confirm add
                            ord("q"),
                        ],
                    )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["channels"]["multi-model"]["models"],
                ["model-preferred"],
            )

    def test_home_add_keeps_all_provider_models_without_replacing_slots(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            original_slots = dict(HUB_V2_FIXTURE["model_slots"])
            provider = {
                "id": "all-models-id",
                "name": "All Models",
                "settings_config": json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_MODEL": "model-default",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL": "model-opus",
                        }
                    }
                ),
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher, "list_providers", return_value=[provider]
                ):
                    self._run_home(
                        launcher,
                        [
                            ord("a"),
                            10,  # provider
                            10,  # accept the default model selection
                            10,  # generated alias
                            10,  # add channel
                            ord("q"),
                        ],
                    )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["channels"]["all-models"]["models"],
                ["model-default", "model-opus"],
            )
            self.assertEqual(persisted["model_slots"], original_slots)

    def test_add_wizard_accepts_a_custom_model_not_in_provider_settings(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {
                "id": "custom-model-id",
                "name": "Custom Model",
                "settings_config": json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_MODEL": "listed-default",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL": "listed-opus",
                        }
                    }
                ),
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            custom_model = "my-custom-model"
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher, "list_providers", return_value=[provider]
                ):
                    self._run_home(
                        launcher,
                        [
                            ord("a"),
                            10,  # provider
                            ord(" "),  # deselect first declared model
                            ord("j"), ord(" "),  # deselect second declared model
                            ord("a"),  # manually add a model
                            *(ord(char) for char in custom_model),
                            10,  # custom model input
                            10,  # continue with custom model selected
                            10,  # generated alias
                            10,  # confirm add
                            ord("q"),
                        ],
                    )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["channels"]["custom-model"]["models"],
                [custom_model],
            )

    def test_add_wizard_offers_custom_model_when_provider_declares_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {
                "id": "no-model-id",
                "name": "No Model",
                "settings_config": "{}",
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            custom_model = "manually-entered-model"
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher, "list_providers", return_value=[provider]
                ):
                    self._run_home(
                        launcher,
                        [
                            ord("a"),
                            10,  # provider
                            10,  # only the custom-model row
                            *(ord(char) for char in custom_model),
                            10,
                            10,  # continue with custom model selected
                            10,  # generated alias
                            10,  # confirm add
                            ord("q"),
                        ],
                    )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["channels"]["no-model"]["models"],
                [custom_model],
            )

    def test_add_wizard_renders_one_coherent_four_stage_surface(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            window = ScriptedWindow(
                [ord("a"), 10, 10, 10, 27, ord("q")],
                size=(24, 100),
            )
            cfg = {
                "providers": {"alpha-id": {"name": "Alpha", "hidden": False}}
            }
            provider = {
                "id": "visual-provider-id",
                "name": "Visual Provider",
                "settings_config": json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_MODEL": "visual-default",
                            "ANTHROPIC_DEFAULT_OPUS_MODEL": "visual-opus",
                        }
                    }
                ),
                "meta": json.dumps({"apiFormat": "anthropic"}),
            }
            with loaded_launcher(env) as launcher:
                launcher._logo_pairs[:] = [0]
                with (
                    mock.patch.object(launcher, "_init_colors", return_value={}),
                    mock.patch.object(launcher, "_intro", return_value=None),
                    mock.patch.object(launcher, "load_mru", return_value={}),
                    mock.patch.object(launcher, "list_providers", return_value=[provider]),
                ):
                    self.assertIsNone(
                        launcher._launcher_main(window, cfg, {"alpha-id"})
                    )

            rendered_strings = [
                value
                for call in window.added
                for value in call
                if isinstance(value, str)
            ]
            rendered = " ".join(rendered_strings)
            self.assertIn("新增 Hub 渠道", rendered)
            self.assertIn("● 渠道", rendered)
            self.assertIn("选择 CC Switch 渠道", rendered)
            self.assertIn("选择要加入 Hub 的模型", rendered)
            self.assertIn("手动添加模型 ID", rendered)
            self.assertIn("设置渠道", rendered)
            self.assertIn("确认新增渠道", rendered)
            self.assertIn("只新增到 Claude-Hub", rendered)
            self.assertNotIn("替换", rendered)
            self.assertNotIn("绑定 Fable", rendered)
            self.assertFalse(any("Step" in value for value in rendered_strings))

    def test_add_wizard_uses_distinct_stage_and_model_family_colors(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            window = ScriptedWindow([27], size=(24, 100))
            with loaded_launcher(env) as launcher:
                launcher.C = {
                    "dim": 1,
                    "lime": 2,
                    "orange": 4,
                    "accent": 8,
                    "sel": 16,
                    "gold": 32,
                    "violet": 64,
                }
                launcher._row_pairs[:] = [128, 256]
                self.assertIsNone(
                    launcher._choose_hub_models(
                        window,
                        {"id": "color-id", "name": "Color Provider"},
                        ["claude-color", "glm-color"],
                    )
                )

            writes = {
                value: call[3]
                for call in window.added
                for value in call[2:3]
                if isinstance(value, str) and len(call) >= 4
            }
            self.assertEqual(writes["✓ 渠道"], 2)
            self.assertNotEqual(writes["✓ 渠道"], writes["● 模型"])
            self.assertNotEqual(writes["● 模型"], writes["○ 设置"])
            glm_row = next(text for text in writes if "[✓] glm-color" in text)
            manual_row = next(text for text in writes if "手动添加模型 ID" in text)
            self.assertNotEqual(writes[glm_row], writes[manual_row])

    def test_channels_add_wizard_prompts_when_api_format_cannot_be_inferred(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {
                "id": "unknown-format-id",
                "name": "Unknown Format",
                "settings_config": json.dumps(
                    {"env": {"ANTHROPIC_MODEL": "unknown-model"}}
                ),
                "meta": "{}",
            }
            with loaded_launcher(env) as launcher:
                with mock.patch.object(
                    launcher, "list_providers", return_value=[provider]
                ):
                    self._run_home(
                        launcher,
                        [
                            ord("\t"),
                            ord("\t"),
                            ord("a"),
                            10,
                            10,
                            10,
                            ord("c"),
                            10,  # confirm add
                            27,
                            ord("q"),
                        ],
                    )

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["channels"]["unknown-format"],
                {
                    "provider": "id:unknown-format-id",
                    "models": ["unknown-model"],
                    "api_format": "openai_chat",
                },
            )

    def test_channel_delete_refuses_route_or_slot_references(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            with loaded_launcher(env) as launcher:
                with self.assertRaisesRegex(ValueError, "fallback"):
                    launcher.remove_hub_channel("glm")
                with self.assertRaisesRegex(ValueError, "槽位"):
                    launcher.remove_hub_channel("grok")

            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertIn("glm", persisted["channels"])
            self.assertIn("grok", persisted["channels"])

    def test_channels_delete_removes_confirmed_unbound_channel(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._hub_env(home)
            config = Path(env["CLAUDE1_HUB_CONFIG"])
            config.write_text(json.dumps(HUB_V2_FIXTURE), encoding="utf-8")
            config.chmod(0o600)
            provider = {"id": "removable-id", "name": "Removable"}
            with loaded_launcher(env) as launcher:
                launcher.add_hub_channel(
                    provider, alias="removable", models=["temporary-model"]
                )
                result = self._run_home(
                    launcher,
                    [
                        ord("\t"), ord("\t"),
                        ord("j"), ord("j"), ord("j"), ord("j"),
                        ord("d"), ord("y"), 10,
                        27, ord("q"),
                    ],
                )

            self.assertIsNone(result)
            persisted = json.loads(config.read_text(encoding="utf-8"))
            self.assertNotIn("removable", persisted["channels"])

    def test_hub_workspace_esc_returns_home_then_quit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                result = self._run_home(
                    launcher, [ord("\t"), 27, ord("q")]
                )
                self.assertIsNone(result)

    def test_home_has_no_hub_entry_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home), write_config=False)
            with loaded_launcher(env) as launcher:
                # No hub config: digit still selects the provider directly.
                result = self._run_home(launcher, [ord("1")])
                self.assertEqual(result, "alpha-id")

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

    def test_main_launches_hub_slot_from_tui_selection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(
                        launcher,
                        "run_tui_launcher",
                        return_value=("hub-slot", "fable"),
                    ),
                    mock.patch.object(
                        launcher, "exec_hub", return_value=0
                    ) as exec_hub,
                ):
                    self.assertEqual(launcher.main([]), 0)
                exec_hub.assert_called_once_with(["--slot", "fable"])

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

    def test_run_tui_launcher_preserves_slot_launch_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            env = self._hub_env(Path(raw_home))
            with loaded_launcher(env) as launcher:
                with (
                    mock.patch.object(launcher, "db_claude_rows", return_value=[]),
                    mock.patch.object(
                        launcher,
                        "load_config",
                        return_value={"version": 2, "providers": {}},
                    ),
                    mock.patch.object(launcher.sys.stdin, "isatty", return_value=True),
                    mock.patch.object(launcher.sys.stdout, "isatty", return_value=True),
                    mock.patch.object(
                        launcher.shutil,
                        "get_terminal_size",
                        return_value=os.terminal_size((120, 40)),
                    ),
                    mock.patch.object(
                        launcher.curses,
                        "wrapper",
                        return_value=launcher.HubLaunch(slot="fable"),
                    ),
                ):
                    self.assertEqual(
                        launcher.run_tui_launcher(), ("hub-slot", "fable")
                    )


if __name__ == "__main__":
    unittest.main()
