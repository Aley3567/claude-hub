"""End-to-end coverage for the OpenAI-compatible protocol bridge launch path.

Everything here stays on 127.0.0.1 and uses fixture credentials only: the
bridge is started exactly the way ``launch_with_protocol_bridge`` starts it in
production, but the upstream it talks to is a local fake OpenAI-compatible
server.
"""

from __future__ import annotations

import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from unittest import mock

from test_launcher import isolated_env, loaded_launcher, write_executable


ROOT = Path(__file__).resolve().parents[1]
REAL_HUB = ROOT / "claude-hub.py"

PROVIDER_NAME = "Fixture OpenAI Channel"
UPSTREAM_TOKEN = "fixture-upstream-token"
UPSTREAM_MODEL = "fixture-openai-model"

# A minimal stand-in for claude-hub: it only satisfies the versioned health
# contract and records what the launcher handed it, so settings-injection and
# lifecycle assertions do not depend on aiohttp or on a real upstream.
STUB_HUB_SOURCE = """
#!/usr/bin/env python3
import json
import os
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer

home = Path(os.environ["HOME"])
(home / "hub-child-env.json").write_text(
    json.dumps(dict(os.environ)), encoding="utf-8"
)
(home / "hub-config.json").write_text(
    Path(os.environ["CLAUDE_HUB_CONFIG"]).read_text(encoding="utf-8"),
    encoding="utf-8",
)
body = json.dumps(
    {"ok": True, "service": "claude-hub", "protocol": 1, "version": "0.1.0"}
).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


server = TCPServer(("127.0.0.1", int(os.environ["CLAUDE_HUB_PORT"])), Handler)
server.serve_forever()
"""

# A fake claude that only reports what the launcher gave it.
CAPTURE_CLAUDE_SOURCE = """
#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

settings_path = Path(sys.argv[sys.argv.index("--settings") + 1])
Path(os.environ["FAKE_CLAUDE_CAPTURE"]).write_text(
    json.dumps(
        {
            "argv": sys.argv[1:],
            "env": dict(os.environ),
            "settings": json.loads(settings_path.read_text(encoding="utf-8")),
        }
    ),
    encoding="utf-8",
)
raise SystemExit(0)
"""

# A fake claude that speaks the Anthropic Messages API to whatever
# ANTHROPIC_BASE_URL the launcher injected, once with the injected token and
# once with a wrong one.
E2E_CLAUDE_SOURCE = """
#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

settings_path = Path(sys.argv[sys.argv.index("--settings") + 1])
record = {
    "env": dict(os.environ),
    "settings": json.loads(settings_path.read_text(encoding="utf-8")),
}
base_url = os.environ["ANTHROPIC_BASE_URL"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(token, payload):
    request = urllib.request.Request(
        base_url + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "authorization": "Bearer " + token,
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            return {
                "status": response.status,
                "body": json.loads(response.read().decode("utf-8")),
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "body": exc.read().decode("utf-8", "replace"),
        }
    except OSError as exc:
        return {"status": None, "error": str(exc)}


message = {
    "model": os.environ["ANTHROPIC_MODEL"],
    "max_tokens": 64,
    "system": "fixture system prompt",
    "messages": [{"role": "user", "content": "fixture question"}],
}
record["authorized"] = call(os.environ["ANTHROPIC_AUTH_TOKEN"], message)
record["unauthorized"] = call("example-wrong-local-token", message)
Path(os.environ["FAKE_CLAUDE_CAPTURE"]).write_text(
    json.dumps(record), encoding="utf-8"
)
raise SystemExit(0)
"""

UPSTREAM_RESPONSE = {
    "id": "chatcmpl-fixture",
    "object": "chat.completion",
    "model": UPSTREAM_MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "fixture bridge reply"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3},
}


def provider_settings(base_url: str) -> dict:
    """CC Switch settings for an OpenAI-compatible fixture Provider."""
    return {
        "api_format": "openai_chat",
        "env": {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_AUTH_TOKEN": UPSTREAM_TOKEN,
            "ANTHROPIC_MODEL": UPSTREAM_MODEL,
        },
    }


def write_provider_db(path: Path, settings: dict) -> None:
    """Create the read-only CC Switch fixture the bridged hub consumes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE providers ("
            "id TEXT, name TEXT, settings_config TEXT, meta TEXT, "
            "app_type TEXT, sort_index INTEGER)"
        )
        connection.execute(
            "INSERT INTO providers VALUES (?, ?, ?, '{}', 'claude', 1)",
            ("fixture-openai", PROVIDER_NAME, json.dumps(settings)),
        )
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def write_real_hub_shim(path: Path) -> None:
    """Run the real claude-hub.py without its ``uv run --script`` shebang.

    CI installs aiohttp into the test interpreter but not uv, so the shim keeps
    the production hub code in the loop while dropping only the launcher.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    write_executable(
        path,
        f"#!{sys.executable}\n"
        "import runpy\n"
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"runpy.run_path({str(REAL_HUB)!r}, run_name='__main__')\n",
    )


@contextmanager
def fake_openai_upstream():
    """Serve one OpenAI Chat Completions response and record the request."""
    received: list[dict] = []
    body = json.dumps(UPSTREAM_RESPONSE).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            received.append(
                {
                    "path": self.path,
                    "headers": {
                        key.lower(): value for key, value in self.headers.items()
                    },
                    "payload": payload,
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def recorded_bridge_processes():
    """Expose the hub subprocess the launcher starts so tests can inspect it."""
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        child_env = kwargs.get("env")
        if isinstance(child_env, dict) and "CLAUDE_HUB_LOCAL_TOKEN" in child_env:
            processes.append(process)
        return process

    with mock.patch.object(subprocess, "Popen", recording_popen):
        yield processes


def port_is_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) != 0


class BridgeLaunchTests(unittest.TestCase):
    """The bridge must hand Claude a loopback URL and a bridge-only token."""

    def _prepare(self, home: Path, *, hub_source: str, claude_source: str) -> dict:
        env = isolated_env(home, CLAUDE1_HUB_START_TIMEOUT="20")
        fake_hub = Path(env["CLAUDE1_HUB_SCRIPT"])
        fake_hub.parent.mkdir(parents=True, exist_ok=True)
        write_executable(fake_hub, hub_source)
        fake_claude = home / "bin" / "claude"
        write_executable(fake_claude, claude_source)
        env.update(
            CLAUDE1_CLAUDE_BIN=str(fake_claude),
            FAKE_CLAUDE_CAPTURE=str(home / "claude-capture.json"),
        )
        return env

    def _bridge(self, launcher, settings: dict, args: list[str]) -> int:
        profile = launcher.resolve_capability_profile(settings=settings)
        self.assertEqual(profile.get("protocol"), "openai_chat")
        with redirect_stdout(io.StringIO()):
            return launcher.launch_with_protocol_bridge(
                {"name": PROVIDER_NAME},
                settings,
                profile,
                args,
            )

    def test_bridge_hands_claude_a_loopback_url_and_a_bridge_only_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._prepare(
                home,
                hub_source=STUB_HUB_SOURCE,
                claude_source=CAPTURE_CLAUDE_SOURCE,
            )
            settings = provider_settings("https://fixture-upstream.invalid/v1")

            with loaded_launcher(env) as launcher:
                self.assertEqual(self._bridge(launcher, settings, ["-p", "hi"]), 0)

            observed = json.loads(
                Path(env["FAKE_CLAUDE_CAPTURE"]).read_text(encoding="utf-8")
            )
            hub_env = json.loads(
                (home / "hub-child-env.json").read_text(encoding="utf-8")
            )
            hub_config = json.loads(
                (home / "hub-config.json").read_text(encoding="utf-8")
            )
            claude_env = observed["env"]
            local_token = hub_env["CLAUDE_HUB_LOCAL_TOKEN"]

            # Claude only ever sees the isolated loopback bridge.
            self.assertEqual(
                claude_env["ANTHROPIC_BASE_URL"],
                f"http://127.0.0.1:{hub_env['CLAUDE_HUB_PORT']}",
            )
            self.assertEqual(
                observed["settings"]["env"]["ANTHROPIC_BASE_URL"],
                claude_env["ANTHROPIC_BASE_URL"],
            )
            # ... authenticated with the per-launch bridge token, never the
            # upstream credential, and never through the user's proxy.
            self.assertEqual(claude_env["ANTHROPIC_AUTH_TOKEN"], local_token)
            self.assertNotEqual(local_token, UPSTREAM_TOKEN)
            self.assertGreaterEqual(len(local_token), 32)
            self.assertNotIn("ANTHROPIC_API_KEY", claude_env)
            self.assertNotIn("ANTHROPIC_API_KEY", observed["settings"]["env"])
            self.assertEqual(claude_env["NO_PROXY"], "127.0.0.1,localhost")
            self.assertEqual(claude_env["no_proxy"], "127.0.0.1,localhost")
            self.assertNotIn(UPSTREAM_TOKEN, json.dumps(observed))

            # The hub child gets the credential-free whitelist plus the token
            # and the private per-launch config it must serve.
            self.assertNotIn(UPSTREAM_TOKEN, json.dumps(hub_env))
            self.assertEqual(hub_env["CLAUDE_HUB_DB"], env["CLAUDE1_DB_PATH"])
            self.assertEqual(hub_config["default_channel"], "direct")
            self.assertEqual(
                hub_config["local_token_env"], "CLAUDE_HUB_LOCAL_TOKEN"
            )
            self.assertNotIn("local_token", hub_config)
            channel = hub_config["channels"]["direct"]
            self.assertEqual(channel["provider"], PROVIDER_NAME)
            self.assertEqual(channel["api_format"], "openai_chat")
            self.assertEqual(channel["capabilities"]["protocol"], "openai_chat")
            self.assertEqual(channel["models"], [UPSTREAM_MODEL])

    def test_bridged_settings_keep_the_subprocess_isolation_env(self) -> None:
        """resume/compact/subagent/background isolation is set at launch time.

        The bridge reuses ``prepare_provider_settings``, so the same scrub and
        capability flags must survive the extra hop.
        """
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._prepare(
                home,
                hub_source=STUB_HUB_SOURCE,
                claude_source=CAPTURE_CLAUDE_SOURCE,
            )
            settings = provider_settings("https://fixture-upstream.invalid/v1")

            with loaded_launcher(env) as launcher:
                self.assertEqual(self._bridge(launcher, settings, ["-p", "hi"]), 0)

            observed = json.loads(
                Path(env["FAKE_CLAUDE_CAPTURE"]).read_text(encoding="utf-8")
            )
            expected = {
                "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
                "ENABLE_TOOL_SEARCH": "false",
                "MAX_THINKING_TOKENS": "0",
                "CLAUDE_CODE_DISABLE_1M_CONTEXT": "1",
            }
            for key, value in expected.items():
                self.assertEqual(observed["settings"]["env"][key], value)
                self.assertEqual(observed["env"][key], value)

    def test_bridge_stops_the_hub_and_removes_its_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._prepare(
                home,
                hub_source=STUB_HUB_SOURCE,
                claude_source=CAPTURE_CLAUDE_SOURCE,
            )
            settings = provider_settings("https://fixture-upstream.invalid/v1")

            with loaded_launcher(env) as launcher:
                with recorded_bridge_processes() as processes:
                    self.assertEqual(
                        self._bridge(launcher, settings, ["-p", "hi"]), 0
                    )
                self.assertEqual(len(processes), 1)
                self.assertIsNotNone(processes[0].poll())

                hub_env = json.loads(
                    (home / "hub-child-env.json").read_text(encoding="utf-8")
                )
                port = int(hub_env["CLAUDE_HUB_PORT"])
                self.assertFalse(launcher.hub_healthy(port))

            self.assertTrue(port_is_closed(port))
            self.assertEqual(
                list(Path(env["CLAUDE1_TMP_DIR"]).glob("claude1-bridge-*")), []
            )

    def test_real_hub_bridges_anthropic_requests_to_an_openai_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home, fake_openai_upstream() as (
            upstream_port,
            upstream_requests,
        ):
            home = Path(raw_home)
            env = self._prepare(
                home,
                hub_source=STUB_HUB_SOURCE,
                claude_source=E2E_CLAUDE_SOURCE,
            )
            write_real_hub_shim(Path(env["CLAUDE1_HUB_SCRIPT"]))
            settings = provider_settings(f"http://127.0.0.1:{upstream_port}")
            write_provider_db(Path(env["CLAUDE1_DB_PATH"]), settings)

            with loaded_launcher(env) as launcher:
                self.assertEqual(self._bridge(launcher, settings, ["-p", "hi"]), 0)

            observed = json.loads(
                Path(env["FAKE_CLAUDE_CAPTURE"]).read_text(encoding="utf-8")
            )
            authorized = observed["authorized"]
            self.assertEqual(authorized["status"], 200, authorized)

            # Claude receives a translated Anthropic message.
            body = authorized["body"]
            self.assertEqual(body["type"], "message")
            self.assertEqual(body["role"], "assistant")
            self.assertEqual(
                body["content"], [{"type": "text", "text": "fixture bridge reply"}]
            )
            self.assertEqual(body["stop_reason"], "end_turn")
            self.assertEqual(body["usage"]["input_tokens"], 11)

            # The upstream receives an OpenAI Chat Completions request.
            self.assertEqual(len(upstream_requests), 1)
            forwarded = upstream_requests[0]
            self.assertEqual(forwarded["path"], "/v1/chat/completions")
            self.assertEqual(forwarded["payload"]["model"], UPSTREAM_MODEL)
            self.assertEqual(
                forwarded["payload"]["messages"],
                [
                    {"role": "system", "content": "fixture system prompt"},
                    {"role": "user", "content": "fixture question"},
                ],
            )
            self.assertNotIn("system", forwarded["payload"])

            # Both hops authenticate independently: the upstream only sees the
            # upstream credential, and the bridge token never leaves loopback.
            local_token = observed["env"]["ANTHROPIC_AUTH_TOKEN"]
            self.assertEqual(
                forwarded["headers"]["authorization"], f"Bearer {UPSTREAM_TOKEN}"
            )
            self.assertNotIn("x-api-key", forwarded["headers"])
            self.assertNotIn("anthropic-version", forwarded["headers"])
            self.assertNotIn(local_token, json.dumps(forwarded))
            self.assertNotIn(UPSTREAM_TOKEN, json.dumps(observed["env"]))
            self.assertEqual(observed["unauthorized"]["status"], 401)


class BridgeFailureTests(unittest.TestCase):
    """A bridge that cannot serve must fail fast instead of hanging."""

    def _prepare(self, home: Path, **overrides: str) -> dict:
        env = isolated_env(home, **overrides)
        marker = home / "claude-ran"
        fake_claude = home / "bin" / "claude"
        fake_claude.parent.mkdir(parents=True, exist_ok=True)
        write_executable(
            fake_claude,
            "#!/bin/sh\n" f"touch '{marker}'\n",
        )
        env["CLAUDE1_CLAUDE_BIN"] = str(fake_claude)
        return env

    def _bridge(self, launcher, settings: dict) -> int:
        profile = launcher.resolve_capability_profile(settings=settings)
        with redirect_stdout(io.StringIO()):
            return launcher.launch_with_protocol_bridge(
                {"name": PROVIDER_NAME},
                settings,
                profile,
                ["-p", "hi"],
            )

    def test_missing_hub_script_fails_before_claude_starts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._prepare(home)
            settings = provider_settings("https://fixture-upstream.invalid/v1")

            with loaded_launcher(env) as launcher:
                with self.assertRaisesRegex(RuntimeError, "协议桥"):
                    self._bridge(launcher, settings)

            self.assertFalse((home / "claude-ran").exists())

    def test_hub_exiting_early_fails_fast_without_starting_claude(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._prepare(home, CLAUDE1_HUB_START_TIMEOUT="20")
            fake_hub = Path(env["CLAUDE1_HUB_SCRIPT"])
            fake_hub.parent.mkdir(parents=True, exist_ok=True)
            write_executable(fake_hub, "#!/bin/sh\nexit 3\n")
            settings = provider_settings("https://fixture-upstream.invalid/v1")

            with loaded_launcher(env) as launcher:
                with recorded_bridge_processes() as processes:
                    with self.assertRaisesRegex(RuntimeError, "协议桥提前退出"):
                        self._bridge(launcher, settings)

            self.assertFalse((home / "claude-ran").exists())
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual(
                list(Path(env["CLAUDE1_TMP_DIR"]).glob("claude1-bridge-*")), []
            )

    def test_hub_that_never_serves_times_out_and_is_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            env = self._prepare(home, CLAUDE1_HUB_START_TIMEOUT="1")
            fake_hub = Path(env["CLAUDE1_HUB_SCRIPT"])
            fake_hub.parent.mkdir(parents=True, exist_ok=True)
            write_executable(fake_hub, "#!/bin/sh\nsleep 120\n")
            settings = provider_settings("https://fixture-upstream.invalid/v1")

            started = time.monotonic()
            with loaded_launcher(env) as launcher:
                with recorded_bridge_processes() as processes:
                    with self.assertRaisesRegex(RuntimeError, "协议桥启动超时"):
                        self._bridge(launcher, settings)
            elapsed = time.monotonic() - started

            self.assertFalse((home / "claude-ran").exists())
            self.assertLess(elapsed, 30)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())
            self.assertEqual(
                list(Path(env["CLAUDE1_TMP_DIR"]).glob("claude1-bridge-*")), []
            )


if __name__ == "__main__":
    unittest.main()
