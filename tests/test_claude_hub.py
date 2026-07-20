import asyncio
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "claude-hub.py"
SPEC = importlib.util.spec_from_file_location("claude_hub", MODULE_PATH)
hub = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(hub)


class ClaudeHubTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_file = root / "hub.json"
        self.db_file = root / "fixture ?#%.db"
        self.log_file = root / "logs" / "hub.log"
        self._write_db(
            [
                (
                    "Fixture HTTPS",
                    {
                        "ANTHROPIC_BASE_URL": "https://upstream.invalid/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                        "ANTHROPIC_DEFAULT_OPUS_MODEL": "upstream-opus",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL": "upstream-sonnet",
                    },
                ),
                (
                    "Fixture HTTP",
                    {
                        "ANTHROPIC_BASE_URL": "http://remote.invalid/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                    },
                ),
                (
                    "Fixture Loopback",
                    {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:19090/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                    },
                ),
            ]
        )
        self._write_config()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_HUB_CONFIG": str(self.config_file),
                "CLAUDE_HUB_DB": str(self.db_file),
                "CLAUDE_HUB_LOG": str(self.log_file),
                "CLAUDE_HUB_LOCAL_TOKEN": "fixture-local-token",
            },
            clear=False,
        )
        self.env_patch.start()
        os.environ.pop("CLAUDE_HUB_PORT", None)
        hub.reset_caches()

    def tearDown(self):
        hub.reset_caches()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _write_config(self, **updates):
        config = {
            "port": 18787,
            "default_channel": "fast",
            "channels": {
                "fast": {
                    "provider": "Fixture HTTPS",
                    "models": ["claude-sonnet-4", "claude-opus-4"],
                },
                "blocked": {
                    "provider": "Fixture HTTP",
                    "models": ["remote-model"],
                },
                "allowed": {
                    "provider": "Fixture HTTP",
                    "models": ["remote-model"],
                    "allow_insecure_http": True,
                },
                "local": {
                    "provider": "Fixture Loopback",
                    "models": ["local-model"],
                },
            },
        }
        config.update(updates)
        self.config_file.write_text(json.dumps(config), encoding="utf-8")

    def _write_db(self, providers):
        connection = sqlite3.connect(self.db_file)
        try:
            connection.execute(
                "CREATE TABLE providers "
                "(name TEXT, app_type TEXT, settings_config TEXT)"
            )
            for name, env in providers:
                connection.execute(
                    "INSERT INTO providers VALUES (?, 'claude', ?)",
                    (name, json.dumps({"env": env})),
                )
            connection.commit()
        finally:
            connection.close()

    def test_environment_paths_and_port_override(self):
        os.environ["CLAUDE_HUB_PORT"] = "19876"
        cfg = hub.get_config()

        self.assertEqual(hub.config_path(), self.config_file)
        self.assertEqual(hub.db_path(), self.db_file)
        self.assertEqual(hub.log_path(), self.log_file)
        self.assertEqual(cfg["port"], 19876)

    def test_config_validation_rejects_missing_default_channel(self):
        self._write_config(default_channel="missing")
        hub.reset_caches()

        with self.assertRaisesRegex(hub.ConfigError, "not present in channels"):
            hub.get_config()

    def test_config_validation_rejects_fractional_port(self):
        self._write_config(port=18787.5)
        hub.reset_caches()

        with self.assertRaisesRegex(hub.ConfigError, "port must be an integer"):
            hub.get_config()

    def test_explicit_and_default_routes(self):
        cfg = hub.get_config()

        self.assertEqual(
            hub.route("anthropic/fast,custom-model", cfg),
            ("fast", "custom-model"),
        )
        self.assertEqual(
            hub.route("claude-opus-4", cfg),
            ("fast", "upstream-opus"),
        )

    def test_local_auth_accepts_bearer_raw_and_api_key(self):
        cfg = hub.get_config()
        accepted = [
            {"authorization": "Bearer fixture-local-token"},
            {"authorization": "fixture-local-token"},
            {"x-api-key": "fixture-local-token"},
        ]
        for headers in accepted:
            with self.subTest(headers=headers):
                self.assertTrue(
                    hub.check_local_auth(SimpleNamespace(headers=headers), cfg)
                )
        self.assertFalse(
            hub.check_local_auth(
                SimpleNamespace(headers={"authorization": "Bearer wrong"}), cfg
            )
        )
        self.assertFalse(
            hub.check_local_auth(
                SimpleNamespace(headers={"x-api-key": "Bearer fixture-local-token"}),
                cfg,
            )
        )

    def test_1m_beta_is_added_and_deduplicated_case_insensitively(self):
        headers = {
            "Anthropic-Beta": (
                "claude-code-20250219,context-1m-old,context-1m-2025-08-07,"
                "context-1m-2025-08-07"
            ),
            "anthropic-beta": "claude-code-20250219",
        }

        hub.ensure_1m_beta(headers, "some-model[1m]")

        beta_keys = [key for key in headers if key.lower() == "anthropic-beta"]
        self.assertEqual(beta_keys, ["anthropic-beta"])
        values = headers["anthropic-beta"].split(",")
        self.assertEqual(values.count("claude-code-20250219"), 1)
        self.assertEqual(values.count("context-1m-2025-08-07"), 1)
        self.assertEqual(values.count("context-1m-old"), 1)

    def test_remote_http_is_rejected_without_channel_opt_in(self):
        cfg = hub.get_config()

        with self.assertRaisesRegex(hub.RouteError, "remote HTTP"):
            hub.resolve_provider("blocked", cfg)

    def test_remote_http_can_be_explicitly_allowed_per_channel(self):
        cfg = hub.get_config()

        provider = hub.resolve_provider("allowed", cfg)

        self.assertEqual(provider["base_url"], "http://remote.invalid")

    def test_loopback_http_is_allowed_without_opt_in(self):
        cfg = hub.get_config()

        provider = hub.resolve_provider("local", cfg)

        self.assertEqual(provider["base_url"], "http://127.0.0.1:19090")

    def test_malformed_upstream_url_becomes_controlled_route_error(self):
        with self.assertRaisesRegex(hub.RouteError, "invalid upstream URL"):
            hub.validate_upstream_url("http://[::1", "broken", False)

    def test_health_payload_is_fixed_and_does_not_disclose_routing(self):
        response = asyncio.run(hub.handle_healthz(None))
        payload = json.loads(response.text)

        self.assertEqual(
            payload,
            {
                "ok": True,
                "service": "claude-hub",
                "protocol": 1,
                "version": "0.1.0",
            },
        )
        serialized = json.dumps(payload)
        for forbidden in ("channels", "provider", "host", "Fixture HTTPS"):
            self.assertNotIn(forbidden, serialized)

    def test_cc_switch_database_is_opened_read_only(self):
        real_connect = sqlite3.connect
        with mock.patch.object(hub.sqlite3, "connect", wraps=real_connect) as connect:
            providers = hub.get_providers()

        self.assertIn("Fixture HTTPS", providers)
        connection_uri = connect.call_args.args[0]
        self.assertIn("mode=ro", connection_uri)
        self.assertIn("%3F%23%25", connection_uri)
        self.assertTrue(connect.call_args.kwargs["uri"])

    def test_forwarding_uses_fake_session_and_disables_redirects(self):
        class FakeUpstream:
            status = 404
            headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        class FakeSession:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeUpstream()

        class FakeRequest:
            path = "/v1/messages/count_tokens"
            path_qs = "/v1/messages/count_tokens?beta=true"

            def __init__(self, session, token):
                self.headers = {"authorization": f"Bearer {token}"}
                self.app = {"session": session}

            async def read(self):
                return json.dumps(
                    {
                        "model": "fast,upstream-sonnet[1m]",
                        "messages": [{"role": "user", "content": "fixture"}],
                    }
                ).encode()

        cfg = hub.get_config()
        session = FakeSession()
        request = FakeRequest(session, cfg["local_token"])

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(
            url,
            "https://upstream.invalid/v1/messages/count_tokens?beta=true",
        )
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(
            kwargs["headers"]["anthropic-beta"],
            "context-1m-2025-08-07",
        )
        forwarded = json.loads(kwargs["data"])
        self.assertEqual(forwarded["model"], "upstream-sonnet[1m]")

        rejected_session = FakeSession()
        rejected = FakeRequest(rejected_session, "wrong-token")
        rejected_response = asyncio.run(hub.handle_messages(rejected))
        self.assertEqual(rejected_response.status, 401)
        self.assertEqual(rejected_session.calls, [])


class ExampleConfigTests(unittest.TestCase):
    def test_example_contains_no_credentials(self):
        example = json.loads(
            (REPO_ROOT / "examples" / "claude-hub.example.json").read_text(
                encoding="utf-8"
            )
        )
        serialized = json.dumps(example).lower()

        self.assertNotIn("local_token", example)
        self.assertEqual(example["local_token_env"], "CLAUDE_HUB_LOCAL_TOKEN")
        self.assertNotIn("anthropic_auth_token", serialized)
        self.assertNotIn("anthropic_api_key", serialized)


if __name__ == "__main__":
    unittest.main()
