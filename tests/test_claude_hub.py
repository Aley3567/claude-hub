import asyncio
import codecs
import gzip
import hmac
import importlib.util
import ipaddress
import io
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest import mock

import aiohttp
from multidict import CIMultiDict


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "claude-hub.py"
SPEC = importlib.util.spec_from_file_location("claude_hub", MODULE_PATH)
hub = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(hub)


def _loopback_host(host):
    if host in ("localhost", b"localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_REAL_AIOHTTP_REQUEST = aiohttp.ClientSession._request
_NETWORK_GUARDS = []


def _guarded_socket_connect(sock, address):
    if isinstance(address, tuple) and not _loopback_host(address[0]):
        raise AssertionError(f"external socket blocked in tests: {address[0]}")
    return _REAL_SOCKET_CONNECT(sock, address)


def _guarded_socket_connect_ex(sock, address):
    if isinstance(address, tuple) and not _loopback_host(address[0]):
        raise AssertionError(f"external socket blocked in tests: {address[0]}")
    return _REAL_SOCKET_CONNECT_EX(sock, address)


async def _guarded_aiohttp_request(self, method, str_or_url, **kwargs):
    host = getattr(str_or_url, "host", None)
    if host is None:
        host = urlparse(str(str_or_url)).hostname
    if not _loopback_host(host):
        raise AssertionError(f"external aiohttp request blocked in tests: {host}")
    return await _REAL_AIOHTTP_REQUEST(self, method, str_or_url, **kwargs)


def setUpModule():
    _NETWORK_GUARDS.extend(
        [
            mock.patch.object(socket.socket, "connect", _guarded_socket_connect),
            mock.patch.object(
                socket.socket,
                "connect_ex",
                _guarded_socket_connect_ex,
            ),
            mock.patch.object(
                aiohttp.ClientSession,
                "_request",
                _guarded_aiohttp_request,
            ),
        ]
    )
    for guard in _NETWORK_GUARDS:
        guard.start()


def tearDownModule():
    for guard in reversed(_NETWORK_GUARDS):
        guard.stop()
    _NETWORK_GUARDS.clear()


class _NeverSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise AssertionError("request unexpectedly reached an upstream session")


class _FakeContent:
    def __init__(self, chunks, fail_after=False):
        self.chunks = list(chunks)
        self.fail_after = fail_after
        self.events = []

    async def iter_any(self):
        for chunk in self.chunks:
            self.events.append("yield")
            yield chunk
        if self.fail_after:
            self.events.append("raise")
            raise aiohttp.ClientPayloadError("fixture stream ended abruptly")


class _FakeUpstream:
    def __init__(self, status, headers, chunks, fail_after=False):
        self.status = status
        self.headers = CIMultiDict(headers)
        self.content = _FakeContent(chunks, fail_after=fail_after)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeSession:
    def __init__(self, upstream):
        self.upstream = upstream
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.upstream


class _SequencedFakeSession:
    def __init__(self, upstreams):
        self.upstreams = list(upstreams)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.upstreams:
            raise AssertionError("unexpected extra upstream attempt")
        return self.upstreams.pop(0)


class _FakeDownstream:
    def __init__(self, status):
        self.status = status
        self.headers = CIMultiDict()
        self.writes = []
        self.prepared = False
        self.eof = False

    async def prepare(self, _request):
        self.prepared = True
        return self

    async def write(self, chunk):
        self.writes.append(chunk)

    async def write_eof(self):
        self.eof = True


class _FakeTransport:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class ClaudeHubTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.root = root
        self.config_file = root / "hub.json"
        self.db_file = root / "fixture ?#%.db"
        self.log_file = root / "logs" / "hub.log"
        self.usage_file = root / "logs" / "hub-usage.jsonl"
        self.account_pool_config = root / "account-pools.json"
        self.account_pool_state = root / "account-state.sqlite3"
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
                "CLAUDE_HUB_USAGE": str(self.usage_file),
                "CLAUDE_HUB_LOCAL_TOKEN": "fixture-local-token",
                "CLAUDE1_ACCOUNT_POOL_CONFIG": str(self.account_pool_config),
                "CLAUDE1_ACCOUNT_POOL_STATE": str(self.account_pool_state),
            },
            clear=False,
        )
        self.env_patch.start()
        os.environ.pop("CLAUDE_HUB_PORT", None)
        hub.reset_caches()

    def tearDown(self):
        if hub._log_fp is not None:
            hub._log_fp.close()
            hub._log_fp = None
        if hub._usage_fp is not None:
            hub._usage_fp.close()
            hub._usage_fp = None
        hub.reset_caches()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_validate_config_preserves_hub_and_channel_transport_policy(self):
        raw = json.loads(self.config_file.read_text(encoding="utf-8"))
        raw["transport"] = {"mode": "auto", "proxies": ["system"]}
        raw["channels"]["fast"]["transport"] = {
            "mode": "proxy",
            "proxies": ["http://127.0.0.1:7897"],
        }

        cfg = hub.validate_config(raw)

        self.assertEqual(cfg["transport"], raw["transport"])
        self.assertEqual(
            cfg["channels"]["fast"]["transport"],
            raw["channels"]["fast"]["transport"],
        )

    def test_log_rotation_keeps_current_and_rotated_files_private(self):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.write_text("oversized", encoding="utf-8")
        self.log_file.chmod(0o644)

        with mock.patch.object(hub, "LOG_MAX_BYTES", 1):
            hub.open_log()

        rotated = self.log_file.with_name(self.log_file.name + ".1")
        self.assertTrue(rotated.is_file())
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), "")
        if os.name == "posix":
            self.assertEqual(self.log_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(rotated.stat().st_mode & 0o777, 0o600)

    def test_log_escapes_newlines_and_rotates_while_server_is_running(self):
        with mock.patch.object(hub, "LOG_MAX_BYTES", 1):
            hub.open_log()
            hub.log("model\nforged-entry")

        rotated = self.log_file.with_name(self.log_file.name + ".1")
        self.assertIn("model\\nforged-entry", rotated.read_text(encoding="utf-8"))
        self.assertEqual(self.log_file.read_text(encoding="utf-8"), "")

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW unavailable")
    def test_usage_log_does_not_follow_symlinks(self):
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "must-not-be-written"
        target.write_text("unchanged", encoding="utf-8")
        self.usage_file.symlink_to(target)

        hub.record_usage("fast", "model", "anthropic", {})

        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
        self.assertIsNone(hub._usage_fp)

    def test_usage_log_rotates_after_crossing_its_size_limit(self):
        with mock.patch.object(hub, "USAGE_LOG_MAX_BYTES", 1):
            hub.record_usage(
                "fast",
                "fixture-model",
                "anthropic",
                {"input_tokens": 3, "output_tokens": 5},
            )

        rotated = self.usage_file.with_name(self.usage_file.name + ".1")
        row = json.loads(rotated.read_text(encoding="utf-8"))
        self.assertEqual(row["model"], "fixture-model")
        self.assertEqual((row["in"], row["out"]), (3, 5))
        self.assertNotIn("hub", row)
        self.assertEqual(self.usage_file.read_text(encoding="utf-8"), "")
        if os.name == "posix":
            self.assertEqual(self.usage_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(rotated.stat().st_mode & 0o777, 0o600)

    def test_usage_log_preserves_provenance_and_does_not_invent_cache_fields(self):
        hub.record_usage(
            "fast",
            "fixture-model",
            "openai_chat",
            {
                "input_tokens": 11,
                "output_tokens": 3,
                "cache_creation": {"ephemeral_5m_input_tokens": 2},
                "server_tool_use": {"web_search_requests": 1},
            },
        )

        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(row["source"], "upstream")
        self.assertEqual((row["in"], row["out"]), (11, 3))
        self.assertNotIn("cr", row)
        self.assertNotIn("cw", row)
        self.assertEqual(
            row["cache_creation"],
            {"ephemeral_5m_input_tokens": 2},
        )
        self.assertEqual(
            row["server_tool_use"],
            {"web_search_requests": 1},
        )

    def test_native_compressed_json_usage_is_shadow_decoded_with_provenance(self):
        encoded = gzip.compress(
            json.dumps(
                {
                    "usage": {
                        "input_tokens": 17,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 5,
                    }
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            mtime=0,
        )
        self.assertEqual(
            hub._usage_from_json_bytes(
                encoded,
                {"content-encoding": "gzip"},
            ),
            {
                "input_tokens": 17,
                "output_tokens": 4,
                "cache_read_input_tokens": 5,
            },
        )
        self.assertIsNone(
            hub._usage_from_json_bytes(
                b"not-a-valid-gzip-stream",
                {"content-encoding": "gzip"},
            )
        )

        hub.record_usage(
            "fast",
            "fixture-model",
            "anthropic",
            None,
            source="unavailable",
        )
        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(row["source"], "unavailable")
        self.assertNotIn("in", row)
        self.assertNotIn("out", row)

    def test_transformed_usage_log_filters_schema_only_zero_placeholders(self):
        prepared = hub.prepare_response(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ]
            },
            "openai_chat",
        )
        observed, source = hub._usage_for_recording(
            prepared.payload.get("usage"),
            prepared.plan,
        )
        self.assertEqual(observed, {})
        self.assertEqual(source, "unavailable")

        partial = hub.prepare_response(
            {
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 9},
            },
            "openai_responses",
        )
        observed, source = hub._usage_for_recording(
            partial.payload.get("usage"),
            partial.plan,
        )
        self.assertEqual(observed, {"input_tokens": 9})
        self.assertEqual(source, "upstream")

    @unittest.skipUnless(os.name == "posix", "POSIX file safety")
    def test_log_open_tightens_existing_file_and_rejects_special_paths(self):
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.write_text("existing", encoding="utf-8")
        self.log_file.chmod(0o644)

        hub.open_log()
        self.assertEqual(self.log_file.stat().st_mode & 0o777, 0o600)
        hub._log_fp.close()
        hub._log_fp = None

        self.log_file.unlink()
        self.log_file.symlink_to(self.config_file)
        with self.assertRaises((OSError, RuntimeError)):
            hub.open_log()

        self.log_file.unlink()
        os.mkfifo(self.log_file)
        with self.assertRaises((OSError, RuntimeError)):
            hub.open_log()

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
        self.config_file.chmod(0o600)

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
        self.db_file.chmod(0o600)

    def _set_provider_endpoint(self, name, endpoint, api_format):
        connection = sqlite3.connect(self.db_file)
        try:
            connection.execute("ALTER TABLE providers ADD COLUMN meta TEXT")
            connection.execute(
                "UPDATE providers SET settings_config=?, meta=? WHERE name=?",
                (
                    json.dumps(
                        {
                            "env": {
                                "ANTHROPIC_BASE_URL": endpoint,
                                "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                            }
                        }
                    ),
                    json.dumps({"isFullUrl": True, "apiFormat": api_format}),
                    name,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _write_account_pool_db(self, *, api_format="anthropic"):
        self.db_file.unlink(missing_ok=True)
        connection = sqlite3.connect(self.db_file)
        try:
            connection.execute(
                "CREATE TABLE providers ("
                "id TEXT, name TEXT, app_type TEXT, settings_config TEXT, meta TEXT)"
            )
            for provider_id, token, base_url in (
                ("primary", "fixture-primary-account-token", "https://upstream.invalid/v1"),
                ("secondary", "fixture-secondary-account-token", "https://upstream.invalid/v1"),
            ):
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, 'claude', ?, ?)",
                    (
                        provider_id,
                        "Pooled account",
                        json.dumps(
                            {
                                "env": {
                                    "ANTHROPIC_BASE_URL": base_url,
                                    "ANTHROPIC_AUTH_TOKEN": token,
                                    "ANTHROPIC_DEFAULT_SONNET_MODEL": "pooled-model",
                                }
                            }
                        ),
                        json.dumps({"apiFormat": api_format}),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        self.db_file.chmod(0o600)
        self._write_config(
            channels={
                "fast": {
                    "provider": "id:primary",
                    "models": ["pooled-model"],
                }
            },
            default_channel="fast",
        )

    def _write_account_pool_config(self, *, strategy="round_robin"):
        self.account_pool_config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "id:primary": {
                            "strategy": strategy,
                            "cooldown_seconds": 60,
                            "max_cooldown_seconds": 3600,
                            "members": [
                                {
                                    "provider": "id:primary",
                                    "weight": 1,
                                    "priority": 0,
                                    "enabled": True,
                                },
                                {
                                    "provider": "id:secondary",
                                    "weight": 1,
                                    "priority": 0,
                                    "enabled": True,
                                },
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.account_pool_config.chmod(0o600)

    def _request(
        self,
        body,
        *,
        session=None,
        path="/v1/messages",
        query="",
        headers=None,
    ):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        session = session or _NeverSession()
        request_headers = CIMultiDict(headers or {})
        if "authorization" not in request_headers:
            request_headers["authorization"] = "Bearer fixture-local-token"

        class FakeRequest:
            method = "POST"
            path_qs = path + query

            def __init__(self):
                self.path = path
                self.query_string = (
                    query[len("?") :] if query.startswith("?") else query
                )
                self.headers = request_headers
                self.app = {"session": session}
                self.transport = _FakeTransport()

            async def read(self):
                return body

        return FakeRequest()

    def test_network_guards_block_external_socket_and_aiohttp(self):
        sock = socket.socket()
        try:
            with self.assertRaisesRegex(AssertionError, "external socket"):
                sock.connect(("203.0.113.1", 443))
        finally:
            sock.close()

        async def blocked_client():
            async with aiohttp.ClientSession() as session:
                with self.assertRaisesRegex(AssertionError, "external aiohttp"):
                    await session.get("https://upstream.invalid/v1/messages")

        asyncio.run(blocked_client())

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

    def test_config_validation_rejects_non_integer_version(self):
        for version in (True, 1.0):
            with self.subTest(version=version):
                self._write_config(version=version)
                hub.reset_caches()
                with self.assertRaisesRegex(hub.ConfigError, "version must be 1 or 2"):
                    hub.get_config()

    def test_gateway_preserves_request_effort_even_with_legacy_config_field(self):
        self._write_config(effort_level="xhigh")
        hub.reset_caches()
        upstream = _FakeUpstream(
            429,
            {"Content-Type": "application/json"},
            [b'{"type":"error","error":{"type":"rate_limit_error"}}'],
        )
        session = _FakeSession(upstream)
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "output_config": {"effort": "low"},
            },
            session=session,
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            asyncio.run(hub.handle_messages(request))

        forwarded = json.loads(session.calls[0][1]["data"])
        self.assertEqual(forwarded["output_config"]["effort"], "low")

    def test_config_validation_rejects_invalid_v2_slot_effort(self):
        self._write_config(
            version=2,
            launch_slot="fable",
            model_slots={
                "fable": "fast,claude-sonnet-4",
                "opus": "fast,claude-opus-4",
                "sonnet": "fast,claude-sonnet-4",
                "haiku": "fast,claude-sonnet-4",
            },
            effort_by_slot={
                "fable": "maximum",
                "opus": "high",
                "sonnet": "high",
                "haiku": "high",
            },
        )
        hub.reset_caches()

        with self.assertRaisesRegex(hub.ConfigError, "effort_by_slot.fable"):
            hub.get_config()

    def test_config_validation_accepts_complete_v2_slot_schema(self):
        self._write_config(
            version=2,
            launch_slot="fable",
            model_slots={
                "fable": "fast,claude-opus-4",
                "opus": "fast,claude-opus-4",
                "sonnet": "fast,claude-sonnet-4",
                "haiku": "fast,claude-sonnet-4",
            },
            effort_by_slot={
                "fable": "xhigh",
                "opus": "high",
                "sonnet": "medium",
                "haiku": "low",
            },
        )
        hub.reset_caches()

        cfg = hub.get_config()
        self.assertEqual(cfg["version"], 2)
        self.assertEqual(cfg["launch_slot"], "fable")
        self.assertEqual(cfg["model_slots"]["sonnet"], "fast,claude-sonnet-4")
        self.assertEqual(cfg["effort_by_slot"]["fable"], "xhigh")

    def test_config_validation_rejects_unsafe_hub_instance_ids(self):
        for instance_id in (
            None,
            True,
            123,
            "",
            "-leading-hyphen",
            "two words",
            "contains:colon",
            "non-ascii-中文",
            "a" * 129,
        ):
            with self.subTest(instance_id=instance_id):
                self._write_config(
                    version=2,
                    instance_id=instance_id,
                    launch_slot="fable",
                    model_slots={
                        "fable": "fast,claude-opus-4",
                        "opus": "fast,claude-opus-4",
                        "sonnet": "fast,claude-sonnet-4",
                        "haiku": "fast,claude-sonnet-4",
                    },
                    effort_by_slot={
                        "fable": "xhigh",
                        "opus": "high",
                        "sonnet": "medium",
                        "haiku": "low",
                    },
                )
                hub.reset_caches()

                with self.assertRaisesRegex(hub.ConfigError, "instance_id"):
                    hub.get_config()

    def test_legacy_base_url_channel_resolves_existing_provider_read_only(self):
        self._write_config(
            default_channel="legacy",
            channels={
                "legacy": {
                    "base_url": "http://127.0.0.1:19090",
                    "token_file": "/ignored/legacy-token",
                    "ensure_cmd": ["ignored-legacy-command"],
                    "models": ["local-model"],
                }
            },
        )
        hub.reset_caches()

        cfg = hub.get_config()
        provider = hub.resolve_provider("legacy", cfg)

        self.assertEqual(cfg["channels"]["legacy"]["provider"], "")
        self.assertEqual(
            cfg["channels"]["legacy"]["provider_base_url"],
            "http://127.0.0.1:19090",
        )
        self.assertEqual(provider["base_url"], "http://127.0.0.1:19090")
        self.assertEqual(provider["token"], "fixture-upstream-token")

    def test_channel_requires_provider_or_base_url_selector(self):
        self._write_config(
            default_channel="broken",
            channels={"broken": {"models": ["some-model"]}},
        )
        hub.reset_caches()

        with self.assertRaisesRegex(
            hub.ConfigError, "provider or base_url must be a non-empty string"
        ):
            hub.get_config()

    def test_explicit_channel_and_declared_default_routes(self):
        cfg = hub.get_config()

        self.assertEqual(
            hub.route("anthropic/fast,custom-model", cfg),
            ("fast", "custom-model"),
        )
        self.assertEqual(
            hub.route("claude-opus-4", cfg),
            ("fast", "claude-opus-4"),
        )

    def test_declared_bare_model_is_not_rewritten_by_tier_name(self):
        cfg = hub.get_config()
        model = "claude-sonnet-4-20250929"
        cfg["channels"]["fast"]["models"].append(model)

        self.assertEqual(
            hub.route(model, cfg),
            ("fast", model),
        )

    def test_unique_declared_bare_model_routes_to_its_channel(self):
        cfg = hub.get_config()

        self.assertEqual(
            hub.route("local-model", cfg),
            ("local", "local-model"),
        )

    def test_ambiguous_declared_bare_model_requires_a_channel(self):
        cfg = hub.get_config()

        with self.assertRaisesRegex(hub.RouteError, "ambiguous model"):
            hub.route("remote-model", cfg)

    def test_bare_fable_slot_uses_the_fallback_providers_fable_mapping(self):
        cfg = hub.get_config()
        providers = {
            "Fixture HTTPS": {
                "model_map": {"fable": "upstream-fable"},
            }
        }

        self.assertEqual(
            hub.route("fable", cfg, providers),
            ("fast", "upstream-fable"),
        )

    def test_unlisted_model_containing_tier_name_fails_clearly(self):
        cfg = hub.get_config()
        providers = {
            "Fixture HTTPS": {
                "model_map": {"fable": "upstream-fable"},
            }
        }

        with self.assertRaisesRegex(hub.RouteError, "unknown model"):
            hub.route("claude-fable-5", cfg, providers)

    def test_config_validation_rejects_non_boolean_route_unknown_to_default(self):
        self._write_config(
            default_channel="fast",
            channels={
                "fast": {
                    "provider": "Fixture HTTPS",
                    "models": ["claude-sonnet-4"],
                    "route_unknown_to_default": "yes",
                }
            },
        )
        hub.reset_caches()

        with self.assertRaisesRegex(
            hub.ConfigError, "route_unknown_to_default must be a boolean"
        ):
            hub.get_config()

    def test_route_unknown_to_default_passes_unlisted_models_through(self):
        self._write_config(
            default_channel="fast",
            channels={
                "fast": {
                    "provider": "Fixture HTTPS",
                    "models": ["claude-sonnet-4"],
                    "route_unknown_to_default": True,
                }
            },
        )
        hub.reset_caches()
        cfg = hub.get_config()

        self.assertIs(cfg["channels"]["fast"]["route_unknown_to_default"], True)
        self.assertEqual(
            hub.route("some-future-model", cfg),
            ("fast", "some-future-model"),
        )
        self.assertEqual(
            hub.route("claude-sonnet-4", cfg),
            ("fast", "claude-sonnet-4"),
        )

    def test_route_unknown_to_default_defaults_off_in_normalized_config(self):
        cfg = hub.get_config()

        for channel in cfg["channels"].values():
            self.assertIs(channel["route_unknown_to_default"], False)

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
        headers = CIMultiDict(
            [
                (
                    "Anthropic-Beta",
                    (
                        "claude-code-20250219,context-1m-old,"
                        "context-1m-2025-08-07,context-1m-2025-08-07"
                    ),
                ),
                ("anthropic-beta", "claude-code-20250219"),
            ]
        )

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

    def test_provider_https_proxy_is_inherited_and_channel_proxy_wins(self):
        connection = sqlite3.connect(self.db_file)
        try:
            env = {
                "ANTHROPIC_BASE_URL": "https://upstream.invalid/v1",
                "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                "HTTPS_PROXY": "http://127.0.0.1:7897",
            }
            connection.execute(
                "UPDATE providers SET settings_config=? WHERE name=?",
                (json.dumps({"env": env}), "Fixture HTTPS"),
            )
            connection.commit()
        finally:
            connection.close()
        hub.reset_caches()

        cfg = hub.get_config()
        provider = hub.resolve_provider("fast", cfg)
        self.assertEqual(
            hub.channel_proxy("fast", cfg, provider), "http://127.0.0.1:7897"
        )

        cfg["channels"]["fast"]["proxy"] = "http://127.0.0.1:8899"
        self.assertEqual(
            hub.channel_proxy("fast", cfg, provider), "http://127.0.0.1:8899"
        )

    def test_legacy_provider_proxy_becomes_proxy_only_transport_policy(self):
        cfg = hub.get_config()
        provider = hub.resolve_provider("fast", cfg)
        provider["proxy"] = "http://127.0.0.1:7897"

        policy = hub.channel_transport_policy(
            "fast",
            cfg,
            provider,
            "https://upstream.invalid/v1/messages",
        )

        self.assertEqual(policy.mode, "proxy")
        self.assertEqual(
            [candidate.proxy for candidate in policy.candidates],
            ["http://127.0.0.1:7897"],
        )

    def test_provider_transport_policy_is_inherited_from_database_settings(self):
        connection = sqlite3.connect(self.db_file)
        try:
            settings = {
                "transport": {"mode": "direct", "proxies": []},
                "env": {
                    "ANTHROPIC_BASE_URL": "https://upstream.invalid/v1",
                    "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                },
            }
            connection.execute(
                "UPDATE providers SET settings_config=? WHERE name=?",
                (json.dumps(settings), "Fixture HTTPS"),
            )
            connection.commit()
        finally:
            connection.close()
        hub.reset_caches()

        cfg = hub.get_config()
        provider = hub.resolve_provider("fast", cfg)
        policy = hub.channel_transport_policy(
            "fast",
            cfg,
            provider,
            "https://upstream.invalid/v1/messages",
        )

        self.assertEqual(policy.mode, "direct")
        self.assertEqual([candidate.identity for candidate in policy.candidates], ["direct"])

    def test_upstream_ssl_context_adds_certifi_ca_bundle(self):
        context = mock.Mock()
        with mock.patch.object(
            hub.ssl, "create_default_context", return_value=context
        ) as create_default_context, mock.patch.object(
            hub.certifi, "where", return_value="/fixture/cacert.pem"
        ):
            result = hub._upstream_ssl_context()

        self.assertIs(result, context)
        create_default_context.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(
            cafile="/fixture/cacert.pem"
        )

    def test_upstream_socket_factory_enables_tcp_keepalive(self):
        created = mock.Mock()
        sock = mock.Mock()
        created.return_value = sock
        address_info = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", 443),
        )

        with mock.patch.object(hub.socket, "socket", created):
            result = hub.upstream_socket_factory(address_info)

        self.assertIs(result, sock)
        created.assert_called_once_with(
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
        sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def test_upstream_connector_uses_keepalive_socket_factory(self):
        connector = mock.Mock()
        tls_context = mock.Mock()
        with mock.patch.object(
            hub, "_upstream_ssl_context", return_value=tls_context
        ), mock.patch.object(
            hub.aiohttp, "TCPConnector", return_value=connector
        ) as tcp_connector:
            result = hub._upstream_connector()

        self.assertIs(result, connector)
        tcp_connector.assert_called_once_with(
            ssl=tls_context,
            socket_factory=hub.upstream_socket_factory,
        )

    def test_upstream_socket_factory_tunes_dead_peer_detection(self):
        sock = mock.Mock()
        address_info = (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("::1", 443, 0, 0),
        )

        with mock.patch.object(hub.socket, "socket", return_value=sock):
            hub.upstream_socket_factory(address_info)

        idle_option = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
            socket, "TCP_KEEPALIVE", None
        )
        if idle_option is not None:
            sock.setsockopt.assert_any_call(
                socket.IPPROTO_TCP,
                idle_option,
                30,
            )
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt.assert_any_call(
                socket.IPPROTO_TCP,
                socket.TCP_KEEPINTVL,
                15,
            )
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt.assert_any_call(
                socket.IPPROTO_TCP,
                socket.TCP_KEEPCNT,
                4,
            )

    def test_upstream_socket_factory_tolerates_unsupported_keepalive_tuning(self):
        sock = mock.Mock()

        def set_socket_option(level, _option, _value):
            if level == socket.IPPROTO_TCP:
                raise OSError("unsupported by fixture kernel")

        sock.setsockopt.side_effect = set_socket_option
        address_info = (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", 443),
        )

        with mock.patch.object(hub.socket, "socket", return_value=sock):
            result = hub.upstream_socket_factory(address_info)

        self.assertIs(result, sock)
        sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    def test_malformed_upstream_url_becomes_controlled_route_error(self):
        with self.assertRaisesRegex(hub.RouteError, "invalid upstream URL"):
            hub.validate_upstream_url("http://[::1", "broken", False)

    def test_https_private_and_metadata_addresses_are_rejected(self):
        for host in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "::1",
            "127.1",
            "2130706433",
            "0x7f000001",
            "①②⑦.⓪.⓪.①",
            "１６９.２５４.１６９.２５４",
            "ⓛⓞⓒⓐⓛⓗⓞⓢⓣ",
            "127。0。0。1",
        ):
            with self.subTest(host=host), self.assertRaisesRegex(
                hub.RouteError, "must not target a private address"
            ):
                url = f"https://[{host}]" if ":" in host else f"https://{host}"
                hub.validate_upstream_url(url, "blocked", False)

    def test_transformed_compressed_json_is_decoded_before_translation(self):
        transformed = {
            "id": "resp_fixture",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "compressed"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }
        upstream = _FakeUpstream(
            200,
            {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            [gzip.compress(json.dumps(transformed).encode("utf-8"), mtime=0)],
        )
        request = self._request({}, session=_FakeSession(upstream))
        provider = {
            "api_format": "openai_chat",
            "base_url": "https://upstream.invalid/v1",
            "token": "fixture-upstream-token",
        }
        response = asyncio.run(
            hub._handle_transformed_messages(
                request,
                cfg=hub.get_config(),
                provider=provider,
                payload={
                    "model": "custom-model",
                    "messages": [{"role": "user", "content": "fixture"}],
                },
                alias="fast",
                model_in="fast,custom-model",
                model_out="custom-model",
                started=0,
            )
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["content"][0]["text"], "compressed")

    def test_cross_protocol_capability_rejection_is_a_client_4xx_before_network(self):
        session = _NeverSession()
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "future_native_block", "opaque": True}],
                    }
                ],
            },
            session=session,
        )
        provider = {
            "api_format": "openai_chat",
            "base_url": "https://upstream.invalid/v1",
            "token": "fixture-upstream-token",
        }

        response = asyncio.run(
            hub._handle_transformed_messages(
                request,
                cfg=hub.get_config(),
                provider=provider,
                payload={
                    "model": "fixture-model",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "future_native_block", "opaque": True}
                            ],
                        }
                    ],
                },
                alias="fast",
                model_in="fast,fixture-model",
                model_out="fixture-model",
                started=0,
            )
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(
            response.headers["x-hub-protocol-code"],
            "HUB_UNSUPPORTED_CONTENT_BLOCK",
        )
        body = json.loads(response.text)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertIn("$.messages[0].content[0]", body["error"]["message"])
        self.assertEqual(session.calls, [])

    def test_usage_record_includes_configured_hub_instance(self):
        instance_id = "fixture-hub_01"
        self._write_config(
            version=2,
            instance_id=instance_id,
            launch_slot="fable",
            model_slots={
                "fable": "fast,claude-opus-4",
                "opus": "fast,claude-opus-4",
                "sonnet": "fast,claude-sonnet-4",
                "haiku": "fast,claude-sonnet-4",
            },
            effort_by_slot={
                "fable": "xhigh",
                "opus": "high",
                "sonnet": "medium",
                "haiku": "low",
            },
        )
        hub.reset_caches()
        transformed = {
            "id": "resp_fixture",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fixture"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps(transformed).encode("utf-8")],
        )
        request = self._request({}, session=_FakeSession(upstream))
        provider = {
            "api_format": "openai_chat",
            "base_url": "https://upstream.invalid/v1",
            "token": "fixture-upstream-token",
        }

        asyncio.run(
            hub._handle_transformed_messages(
                request,
                cfg=hub.get_config(),
                provider=provider,
                payload={
                    "model": "custom-model",
                    "messages": [{"role": "user", "content": "fixture"}],
                },
                alias="fast",
                model_in="fast,custom-model",
                model_out="custom-model",
                started=0,
            )
        )

        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(row["hub"], instance_id)

    def test_usage_tracker_discards_overlong_line_and_recovers_after_newline(self):
        tracker = hub._SSEUsageTracker()
        for _ in range(8):
            tracker.feed(b"x" * (128 * 1024))

        self.assertLessEqual(len(tracker._pending), hub.SSE_LINE_LIMIT)
        self.assertTrue(tracker._discarding_line)

        valid_event = json.dumps(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 17}},
            },
            separators=(",", ":"),
        ).encode()
        tracker.feed(b"\n" + b"data:" + valid_event + b"\n")

        self.assertEqual(tracker.usage["input_tokens"], 17)
        self.assertFalse(tracker._discarding_line)

    def test_native_usage_tracker_preserves_cache_and_server_detail(self):
        tracker = hub._SSEUsageTracker()
        event = json.dumps(
            {
                "type": "message_delta",
                "usage": {
                    "output_tokens": 9,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 4,
                        "ephemeral_1h_input_tokens": 2,
                    },
                    "server_tool_use": {"web_search_requests": 1},
                },
            },
            separators=(",", ":"),
        ).encode()
        tracker.feed(b"data:" + event + b"\n\n")

        self.assertEqual(
            tracker.usage["cache_creation"],
            {
                "ephemeral_5m_input_tokens": 4,
                "ephemeral_1h_input_tokens": 2,
            },
        )
        self.assertEqual(
            tracker.usage["server_tool_use"],
            {"web_search_requests": 1},
        )

    def test_stream_telemetry_measures_first_chunk_and_largest_gap(self):
        timestamps = iter((10.2, 10.5, 11.0, 12.25))
        telemetry = hub.StreamTelemetry(
            started_at=10.0,
            clock=lambda: next(timestamps),
        )

        telemetry.observe(b"a")
        telemetry.observe(b"bc")
        telemetry.observe(b"def")

        self.assertEqual(
            telemetry.snapshot(),
            {
                "headers_ms": 200,
                "first_chunk_ms": 500,
                "max_gap_ms": 1250,
                "chunks": 3,
                "upstream_bytes": 6,
            },
        )

    def test_usage_json_buffer_has_the_same_64_mib_cap_as_transform_bodies(self):
        buffer = bytearray(b"a" * (hub.MAX_UPSTREAM_BODY_BYTES - 1))
        self.assertIsNone(hub._append_bounded_json_buffer(buffer, b"bc"))
        self.assertEqual(
            hub._append_bounded_json_buffer(bytearray(b"a"), b"b", limit=2),
            bytearray(b"ab"),
        )

    def test_transformed_stream_type_errors_abort_the_started_response(self):
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [b"data: fixture\n\n"],
        )
        session = _FakeSession(upstream)
        request = self._request(
            {"model": "fast,model", "messages": [], "stream": True},
            session=session,
        )
        downstream = _FakeDownstream(200)

        class TypeFailingBridge:
            input_tokens = output_tokens = cache_read = cache_write = 0

            def __init__(self, _api_format):
                pass

            def feed(self, _event, _data):
                raise TypeError("invalid upstream usage")

            def finish(self):
                return []

        provider = {
            "api_format": "openai_chat",
            "base_url": "https://upstream.invalid/v1",
            "token": "fixture-upstream-token",
        }
        with mock.patch.object(hub, "AnthropicStreamBridge", TypeFailingBridge), mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ), mock.patch.object(hub, "log") as write_log:
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(
                    hub._handle_transformed_messages(
                        request,
                        cfg=hub.get_config(),
                        provider=provider,
                        payload={"model": "model", "messages": [], "stream": True},
                        alias="fast",
                        model_in="fast,model",
                        model_out="model",
                        started=0,
                    )
                )
        self.assertTrue(request.transport.aborted)
        self.assertEqual(len(session.calls), 1)
        rendered_log = "\n".join(
            call.args[0] for call in write_log.call_args_list
        )
        self.assertIn("first_chunk_ms=", rendered_log)
        self.assertIn("max_gap_ms=", rendered_log)
        self.assertIn("chunks=1", rendered_log)
        self.assertIn("upstream_bytes=15", rendered_log)
        self.assertIn("downstream_bytes=0", rendered_log)
        self.assertIn("terminal=error", rendered_log)
        self.assertIn("error=TypeError", rendered_log)

    def test_transformed_stream_clean_eof_emits_anthropic_terminal_events(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        # 新协议内核契约：无终止事件的裸 EOF 会 fail closed（见
        # test_transformed_stream_clean_eof_without_terminal_aborts_fail_closed）。
        # 本测试覆盖正常收尾：终止 chunk + [DONE] 后 EOF 发出完整终止事件。
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b'data: {"id":"chatcmpl_fixture","model":"fixture-model",'
                b'"choices":[{"delta":{"content":"partial"},'
                b'"finish_reason":null}]}\n\n',
                b'data: {"id":"chatcmpl_fixture","model":"fixture-model",'
                b'"choices":[{"delta":{},'
                b'"finish_reason":"stop"}]}\n\n',
                b"data: [DONE]\n\n",
            ],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ), mock.patch.object(hub, "log") as write_log:
            response = asyncio.run(hub.handle_messages(request))

        rendered = b"".join(downstream.writes)
        self.assertIs(response, downstream)
        self.assertIn(b'"text":"partial"', rendered)
        self.assertIn(b"event: message_delta\n", rendered)
        self.assertIn(b"event: message_stop\n", rendered)
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)
        rendered_log = "\n".join(
            call.args[0] for call in write_log.call_args_list
        )
        self.assertIn("first_chunk_ms=", rendered_log)
        self.assertIn("max_gap_ms=", rendered_log)
        self.assertIn("chunks=3", rendered_log)
        self.assertIn("terminal=complete", rendered_log)

    def test_transformed_stream_clean_eof_without_terminal_aborts_fail_closed(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b'data: {"id":"chatcmpl_fixture","model":"fixture-model",'
                b'"choices":[{"delta":{"content":"partial"},'
                b'"finish_reason":null}]}\n\n'
            ],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        rendered = b"".join(downstream.writes)
        self.assertIn(b'"text":"partial"', rendered)
        self.assertNotIn(b"event: message_delta\n", rendered)
        self.assertNotIn(b"event: message_stop\n", rendered)
        self.assertFalse(downstream.eof)
        self.assertTrue(request.transport.aborted)

    def test_transformed_stream_runtime_degradation_codes_are_logged(self):
        events = [
            {
                "choices": [
                    {
                        "delta": {"reasoning_content": "unsigned thought"},
                        "finish_reason": "stop",
                    }
                ]
            }
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b"data:"
                + json.dumps(event, separators=(",", ":")).encode()
                + b"\n\n"
                for event in events
            ],
        )
        request = self._request(
            {"model": "fast,model", "messages": [], "stream": True},
            session=_FakeSession(upstream),
        )
        provider = {
            "api_format": "openai_chat",
            "base_url": "https://upstream.invalid/v1",
            "token": "fixture-upstream-token",
        }
        downstream = _FakeDownstream(200)
        observed = []
        with mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ), mock.patch.object(hub, "log", side_effect=observed.append):
            asyncio.run(
                hub._handle_transformed_messages(
                    request,
                    cfg=hub.get_config(),
                    provider=provider,
                    payload={"model": "model", "messages": [], "stream": True},
                    alias="fast",
                    model_in="fast,model",
                    model_out="model",
                    started=0,
                )
            )

        self.assertTrue(
            any("HUB_DEGRADE_UNSIGNED_THINKING" in entry for entry in observed)
        )

    def _sse_frames(self, rendered):
        frames = []
        for frame in rendered.split(b"\n\n"):
            if not frame:
                continue
            event_line, data_line = frame.split(b"\n", 1)
            frames.append(
                (
                    event_line[len(b"event: ") :].decode(),
                    json.loads(data_line[len(b"data: ") :]),
                )
            )
        return frames

    def _transformed_json_upstream(self, upstream_payload):
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps(upstream_payload).encode()],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
            },
            session=_FakeSession(upstream),
        )
        provider = {
            "api_format": "openai_chat",
            "base_url": "https://upstream.invalid/v1",
            "token": "fixture-upstream-token",
        }
        return asyncio.run(
            hub._handle_transformed_messages(
                request,
                cfg=hub.get_config(),
                provider=provider,
                payload={
                    "model": "custom-model",
                    "messages": [{"role": "user", "content": "fixture"}],
                    "stream": True,
                },
                alias="fast",
                model_in="fast,custom-model",
                model_out="custom-model",
                started=0,
            )
        )

    def test_transformed_json_upstream_is_synthesized_into_sse_when_streaming(self):
        response = self._transformed_json_upstream(
            {
                "id": "chatcmpl_fixture",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-hub-channel"], "fast")
        self.assertEqual(response.headers["x-hub-model"], "custom-model")
        self.assertEqual(response.headers["x-hub-upstream-format"], "openai_chat")
        events = self._sse_frames(response.body)
        self.assertEqual(
            [name for name, _data in events],
            [
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ],
        )
        message = events[0][1]["message"]
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], [])
        self.assertEqual(
            message["usage"], {"input_tokens": 3, "output_tokens": 2}
        )
        self.assertEqual(
            events[1][1]["content_block"], {"type": "text", "text": ""}
        )
        self.assertEqual(
            events[2][1]["delta"], {"type": "text_delta", "text": "hello"}
        )
        self.assertEqual(events[2][1]["index"], 0)
        self.assertEqual(
            events[4][1]["delta"],
            {"stop_reason": "end_turn", "stop_sequence": None},
        )
        self.assertEqual(events[4][1]["usage"], {"output_tokens": 2})

    def test_synthesized_sse_does_not_fabricate_unobserved_usage(self):
        response = self._transformed_json_upstream(
            {
                "id": "chatcmpl_fixture",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
            }
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["content-type"], "text/event-stream")
        self.assertIn(
            "HUB_USAGE_PROVENANCE_UNAVAILABLE",
            response.headers["x-hub-protocol-warnings"],
        )
        self.assertNotIn(b'"usage"', response.body)

    def test_synthesized_sse_streams_tool_use_input_as_json_delta(self):
        response = self._transformed_json_upstream(
            {
                "id": "chatcmpl_fixture",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"a": 1}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4},
            }
        )

        self.assertEqual(response.status, 200)
        events = self._sse_frames(response.body)
        self.assertEqual(
            [name for name, _data in events],
            [
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ],
        )
        self.assertEqual(
            events[1][1]["content_block"],
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "get_weather",
                "input": {},
            },
        )
        self.assertEqual(events[1][1]["index"], 0)
        self.assertEqual(
            events[2][1]["delta"],
            {"type": "input_json_delta", "partial_json": '{"a":1}'},
        )
        self.assertEqual(
            events[4][1]["delta"]["stop_reason"], "tool_use"
        )

    def test_request_too_large_returns_anthropic_json_error(self):
        async def handler(_request):
            raise hub.web.HTTPRequestEntityTooLarge(max_size=1, actual_size=2)

        response = asyncio.run(
            hub.controlled_error_middleware(
                SimpleNamespace(method="POST", path="/v1/messages"), handler
            )
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(response.text)["error"]["type"], "invalid_request_error")

    def test_missing_upstream_session_returns_controlled_configuration_error(self):
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
            }
        )
        request.app = {}

        response = asyncio.run(
            hub.controlled_error_middleware(request, hub.handle_messages)
        )

        payload = json.loads(response.text)
        self.assertEqual(response.status, 503)
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error"]["type"], "api_error")
        self.assertIn("configuration is unavailable", payload["error"]["message"])

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

    def test_readyz_returns_token_bound_challenge_proof(self):
        challenge = "fixture_challenge_1234567890"
        request = SimpleNamespace(
            headers={"x-claude-hub-challenge": challenge}
        )
        response = asyncio.run(hub.handle_readyz(request))
        payload = json.loads(response.text)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["service"], "claude-hub")
        self.assertEqual(
            payload["proof"],
            hmac.digest(
                b"fixture-local-token",
                f"claude-hub-ready:v1:{hub.get_config()['port']}:{challenge}".encode(
                    "ascii"
                ),
                "sha256",
            ).hex(),
        )
        self.assertNotIn("identity_protocol", payload)
        bad = asyncio.run(
            hub.handle_readyz(
                SimpleNamespace(headers={"x-claude-hub-challenge": "short"})
            )
        )
        self.assertEqual(bad.status, 400)

    def test_readyz_uses_v2_identity_proof_for_named_hub_instance(self):
        instance_id = "fixture-hub_01"
        self._write_config(
            version=2,
            instance_id=instance_id,
            launch_slot="fable",
            model_slots={
                "fable": "fast,claude-opus-4",
                "opus": "fast,claude-opus-4",
                "sonnet": "fast,claude-sonnet-4",
                "haiku": "fast,claude-sonnet-4",
            },
            effort_by_slot={
                "fable": "xhigh",
                "opus": "high",
                "sonnet": "medium",
                "haiku": "low",
            },
        )
        hub.reset_caches()
        challenge = "fixture_challenge_1234567890"

        response = asyncio.run(
            hub.handle_readyz(
                SimpleNamespace(headers={"x-claude-hub-challenge": challenge})
            )
        )
        payload = json.loads(response.text)

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["identity_protocol"], 2)
        self.assertEqual(
            payload["proof"],
            hmac.digest(
                b"fixture-local-token",
                (
                    f"claude-hub-ready:v2:{instance_id}:"
                    f"{hub.get_config()['port']}:{challenge}"
                ).encode("ascii"),
                "sha256",
            ).hex(),
        )
        self.assertNotIn("instance_id", payload)
        self.assertNotIn("name", payload)

    def test_private_snapshot_is_opened_read_only_without_touching_source(self):
        source_bytes = self.db_file.read_bytes()
        source_mtime = self.db_file.stat().st_mtime_ns
        source_sidecars = hub._sqlite_sidecars(self.db_file)
        self.assertTrue(all(not path.exists() for path in source_sidecars))
        real_connect = sqlite3.connect
        with mock.patch.object(hub.sqlite3, "connect", wraps=real_connect) as connect:
            providers = hub.get_providers()

        self.assertIn("Fixture HTTPS", providers)
        connection_uri = connect.call_args.args[0]
        self.assertIn("mode=ro", connection_uri)
        self.assertNotIn("%3F%23%25", connection_uri)
        self.assertTrue(connect.call_args.kwargs["uri"])
        self.assertEqual(self.db_file.read_bytes(), source_bytes)
        self.assertEqual(self.db_file.stat().st_mtime_ns, source_mtime)
        self.assertTrue(all(not path.exists() for path in source_sidecars))

    def test_duplicate_provider_names_require_id_selectors(self):
        duplicate_db = self.root / "duplicates.db"
        connection = sqlite3.connect(duplicate_db)
        try:
            connection.execute(
                "CREATE TABLE providers ("
                "id TEXT, name TEXT, app_type TEXT, settings_config TEXT)"
            )
            settings = json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://fixture.invalid/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-token",
                    }
                }
            )
            connection.executemany(
                "INSERT INTO providers VALUES (?, 'Duplicated', 'claude', ?)",
                [("first", settings), ("second", settings)],
            )
            connection.commit()
        finally:
            connection.close()

        providers = hub._read_provider_rows(duplicate_db)

        self.assertNotIn("Duplicated", providers)
        self.assertIn("id:first", providers)
        self.assertIn("id:second", providers)

    def test_config_and_database_permissions_fail_closed(self):
        self.config_file.chmod(0o644)
        hub.reset_caches()
        with self.assertRaisesRegex(hub.ConfigError, "exceed 0600"):
            hub.get_config()

        self.config_file.chmod(0o600)
        self.db_file.chmod(0o640)
        hub.reset_caches()
        with self.assertRaisesRegex(hub.ProviderDatabaseError, "exceed 0600"):
            hub.get_providers()

    def test_database_errors_are_explicit_and_do_not_use_stale_cache(self):
        self.assertIn("Fixture HTTPS", hub.get_providers())
        self.db_file.write_bytes(b"not a sqlite database")
        self.db_file.chmod(0o600)
        hub.reset_caches()

        with self.assertRaises(hub.ProviderDatabaseError):
            hub.get_providers()

    def test_wal_commits_are_visible_without_resetting_provider_state(self):
        writer = sqlite3.connect(self.db_file)
        try:
            self.assertEqual(
                writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                "wal",
            )
            writer.execute("PRAGMA wal_autocheckpoint=0")
            initial = hub.get_providers()["Fixture HTTPS"]
            self.assertEqual(initial["token"], "fixture-upstream-token")

            updated_env = {
                "ANTHROPIC_BASE_URL": "https://updated.invalid/v1",
                        "ANTHROPIC_AUTH_TOKEN": "updated-wal-token",  # secret-guard: allow
            }
            writer.execute(
                "UPDATE providers SET settings_config=? WHERE name=?",
                (
                    json.dumps({"env": updated_env}),
                    "Fixture HTTPS",
                ),
            )
            writer.commit()
            sidecars = hub._sqlite_sidecars(self.db_file)
            for sidecar in sidecars:
                self.assertTrue(sidecar.exists())
                sidecar.chmod(0o600)

            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (self.db_file, *sidecars)
            }
            # Intentionally no reset_caches(): committed WAL data must be current.
            updated = hub.get_providers()["Fixture HTTPS"]
            self.assertEqual(updated["token"], "updated-wal-token")
            self.assertEqual(updated["base_url"], "https://updated.invalid")
            for path, (contents, mtime_ns) in before.items():
                self.assertEqual(path.read_bytes(), contents)
                self.assertEqual(path.stat().st_mtime_ns, mtime_ns)

            output = io.StringIO()
            with redirect_stdout(output):
                hub.cli_doctor()
            rendered = output.getvalue()
            for path, (contents, mtime_ns) in before.items():
                self.assertEqual(path.read_bytes(), contents)
                self.assertEqual(path.stat().st_mtime_ns, mtime_ns)
            self.assertIn("provider database opens read-only", rendered)
            self.assertIn("channel 'fast' ready", rendered)
            self.assertNotIn("updated-wal-token", rendered)
            self.assertNotIn("https://updated.invalid", rendered)
        finally:
            writer.close()

    def test_wal_snapshot_without_shm_never_creates_source_sidecar(self):
        writer = sqlite3.connect(self.db_file)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            updated_env = {
                "ANTHROPIC_BASE_URL": "https://wal-only.invalid/v1",
                "ANTHROPIC_AUTH_TOKEN": "wal-only-token",
            }
            writer.execute(
                "UPDATE providers SET settings_config=? WHERE name=?",
                (json.dumps({"env": updated_env}), "Fixture HTTPS"),
            )
            writer.commit()

            source = self.root / "wal-source.db"
            source_wal, source_shm = hub._sqlite_sidecars(source)
            shutil.copyfile(self.db_file, source)
            shutil.copyfile(
                self.db_file.with_name(self.db_file.name + "-wal"),
                source_wal,
            )
            source.chmod(0o600)
            source_wal.chmod(0o600)
            self.assertFalse(source_shm.exists())
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (source, source_wal)
            }
            os.environ["CLAUDE_HUB_DB"] = str(source)

            provider = hub.get_providers()["Fixture HTTPS"]

            self.assertEqual(provider["token"], "wal-only-token")
            self.assertEqual(provider["base_url"], "https://wal-only.invalid")
            self.assertFalse(source_shm.exists())
            for path, (contents, mtime_ns) in before.items():
                self.assertEqual(path.read_bytes(), contents)
                self.assertEqual(path.stat().st_mtime_ns, mtime_ns)
        finally:
            writer.close()

    def test_database_symlink_checks_real_sidecar_permissions(self):
        writer = sqlite3.connect(self.db_file)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "UPDATE providers SET settings_config=settings_config || ' ' "
                "WHERE name='Fixture HTTPS'"
            )
            writer.commit()
            wal_path, shm_path = hub._sqlite_sidecars(self.db_file)
            wal_path.chmod(0o644)
            shm_path.chmod(0o600)
            alias = self.root / "provider-alias.db"
            alias.symlink_to(self.db_file)
            os.environ["CLAUDE_HUB_DB"] = str(alias)

            with self.assertRaisesRegex(
                hub.ProviderDatabaseError,
                "-wal.*exceed 0600",
            ):
                hub.get_providers()

            output = io.StringIO()
            with redirect_stdout(output):
                status = hub.cli_doctor()
            self.assertEqual(status, 1)
            self.assertIn("-wal permissions 0644 exceed 0600", output.getvalue())
            self.assertFalse(
                alias.with_name(alias.name + "-wal").exists(),
            )
        finally:
            writer.close()

    def test_server_rejects_unsafe_wal_before_creating_app_or_binding(self):
        writer = sqlite3.connect(self.db_file)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute(
                "UPDATE providers SET settings_config=settings_config || ' ' "
                "WHERE name='Fixture HTTPS'"
            )
            writer.commit()
            wal_path, shm_path = hub._sqlite_sidecars(self.db_file)
            self.assertTrue(wal_path.exists())
            self.assertTrue(shm_path.exists())
            wal_path.chmod(0o644)
            shm_path.chmod(0o600)
            hub.reset_caches()

            with mock.patch.object(hub, "open_log"), mock.patch.object(
                hub, "create_app"
            ) as create_app:
                with self.assertRaisesRegex(
                    hub.ProviderDatabaseError,
                    "-wal.*exceed 0600",
                ):
                    asyncio.run(hub.run_server(fg=False))

            create_app.assert_not_called()
            output = io.StringIO()
            with redirect_stdout(output):
                status = hub.cli_doctor()
            self.assertEqual(status, 1)
            self.assertIn("-wal permissions 0644 exceed 0600", output.getvalue())

            wal_path.chmod(0o600)
            shm_path.chmod(0o644)
            with self.assertRaisesRegex(
                hub.ProviderDatabaseError,
                "-shm.*exceed 0600",
            ):
                hub.get_providers()
            output = io.StringIO()
            with redirect_stdout(output):
                status = hub.cli_doctor()
            self.assertEqual(status, 1)
            self.assertIn("-shm permissions 0644 exceed 0600", output.getvalue())
        finally:
            writer.close()

    def test_bind_failure_closes_the_upstream_client_session(self):
        sessions = []
        real_client_session = hub.aiohttp.ClientSession

        def create_session(*args, **kwargs):
            session = real_client_session(*args, **kwargs)
            sessions.append(session)
            return session

        async def scenario():
            with mock.patch.object(hub, "open_log"), mock.patch.object(
                hub.aiohttp,
                "ClientSession",
                side_effect=create_session,
            ), mock.patch.object(
                hub.web.TCPSite,
                "start",
                new=mock.AsyncMock(side_effect=OSError("fixture bind failure")),
            ):
                with self.assertRaisesRegex(OSError, "fixture bind failure"):
                    await hub.run_server(fg=False)

        asyncio.run(scenario())

        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].closed)

    def test_server_uses_the_inherited_loopback_listener(self):
        async def scenario(inherited_fd, port):
            sock_site = mock.Mock()
            sock_site.start = mock.AsyncMock(
                side_effect=OSError("fixture inherited listener stop")
            )
            inherited_address = []

            def use_inherited_listener(_runner, inherited):
                inherited_address.append(inherited.getsockname())
                return sock_site

            with mock.patch.dict(
                os.environ,
                {
                    hub.ENV_LISTEN_FD: str(inherited_fd),
                    hub.ENV_PORT: str(port),
                },
            ), mock.patch.object(hub, "open_log"), mock.patch.object(
                hub.web, "SockSite", side_effect=use_inherited_listener
            ), mock.patch.object(
                hub.web, "TCPSite"
            ) as tcp_site:
                with self.assertRaisesRegex(
                    OSError, "fixture inherited listener stop"
                ):
                    await hub.run_server(fg=False)

            tcp_site.assert_not_called()
            self.assertEqual(inherited_address, [listener.getsockname()])

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            inherited_fd = os.dup(listener.fileno())
            try:
                asyncio.run(scenario(inherited_fd, listener.getsockname()[1]))
            finally:
                try:
                    os.close(inherited_fd)
                except OSError:
                    pass

    def test_malformed_non_object_and_invalid_model_never_reach_upstream(self):
        cases = [
            (b"{", "valid JSON"),
            (b'{"model":"fast,x","temperature":NaN}', "valid JSON"),
            (b'{"model":"fast,x","temperature":1e400}', "valid JSON"),
            (b'{"model":"fast,x","metadata":"\\ud800"}', "valid JSON"),
            (b"null", "JSON object"),
            (b"[]", "JSON object"),
            ({}, "model"),
            ({"model": 7}, "model"),
            ({"model": "   "}, "model"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                session = _NeverSession()
                response = asyncio.run(
                    hub.handle_messages(self._request(body, session=session))
                )
                payload = json.loads(response.text)
                self.assertEqual(response.status, 400)
                self.assertEqual(payload["type"], "error")
                self.assertEqual(
                    payload["error"]["type"], "invalid_request_error"
                )
                self.assertIn(message, payload["error"]["message"])
                self.assertEqual(session.calls, [])

    def test_excessively_nested_json_returns_400_without_reaching_upstream(self):
        session = _NeverSession()
        nesting = 2_000
        body = (
            b'{"model":"fast,fixture-model","nested":'
            + b"[" * nesting
            + b"0"
            + b"]" * nesting
            + b"}"
        )

        response = asyncio.run(
            hub.handle_messages(self._request(body, session=session))
        )

        payload = json.loads(response.text)
        self.assertEqual(response.status, 400)
        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertEqual(payload["error"]["message"], "request body must be valid JSON")
        self.assertEqual(session.calls, [])

    def test_compressed_request_body_is_rejected_before_decode_or_forward(self):
        for headers in (
            {"content-encoding": "gzip"},
            CIMultiDict(
                [
                    ("content-encoding", "identity"),
                    ("content-encoding", "gzip"),
                ]
            ),
        ):
            with self.subTest(headers=list(headers.items())):
                session = _NeverSession()
                request = self._request(
                    b"pretend-this-was-decompressed-by-aiohttp",
                    session=session,
                    headers=headers,
                )

                response = asyncio.run(hub.handle_messages(request))

                self.assertEqual(response.status, 415)
                self.assertEqual(
                    json.loads(response.text)["error"]["type"],
                    "invalid_request_error",
                )
                self.assertEqual(session.calls, [])

    def test_real_loopback_gzip_request_returns_415_and_closes_session(self):
        async def scenario():
            app = hub.create_app()
            runner = hub.web.AppRunner(app, access_log=None)
            await runner.setup()
            upstream_session = app[hub.UPSTREAM_SESSION_KEY]
            self.assertFalse(upstream_session.closed)
            site = hub.web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            try:
                body = gzip.compress(
                    json.dumps({"model": "fast,custom-model"}).encode()
                )
                async with aiohttp.ClientSession() as client:
                    response = await client.post(
                        f"http://127.0.0.1:{port}/v1/messages",
                        data=body,
                        headers={
                            "authorization": "Bearer fixture-local-token",
                            "content-type": "application/json",
                            "content-encoding": "gzip",
                        },
                    )
                    self.assertEqual(response.status, 415)
                    payload = await response.json()
                    self.assertEqual(payload["type"], "error")
            finally:
                await runner.cleanup()
            self.assertTrue(upstream_session.closed)

        asyncio.run(scenario())

    def test_real_loopback_gzip_response_bytes_and_encoding_are_preserved(self):
        async def scenario():
            upstream_runner = None
            hub_runner = None
            expected_json = {
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "fixture",
                },
            }
            expected_body = gzip.compress(json.dumps(expected_json).encode())

            async def compressed_upstream(request):
                await request.read()
                return hub.web.Response(
                    status=429,
                    body=expected_body,
                    headers={
                        "content-type": "application/json",
                        "content-encoding": "gzip",
                    },
                )

            try:
                upstream_app = hub.web.Application()
                upstream_app.router.add_post(
                    "/v1/messages",
                    compressed_upstream,
                )
                upstream_runner = hub.web.AppRunner(upstream_app, access_log=None)
                await upstream_runner.setup()
                upstream_site = hub.web.TCPSite(
                    upstream_runner,
                    "127.0.0.1",
                    0,
                )
                await upstream_site.start()
                upstream_port = upstream_site._server.sockets[0].getsockname()[1]

                connection = sqlite3.connect(self.db_file)
                try:
                    updated_env = {
                        "ANTHROPIC_BASE_URL": (
                            f"http://127.0.0.1:{upstream_port}/v1"
                        ),
                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                    }
                    connection.execute(
                        "UPDATE providers SET settings_config=? WHERE name=?",
                        (
                            json.dumps({"env": updated_env}),
                            "Fixture HTTPS",
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
                self.db_file.chmod(0o600)

                hub_app = hub.create_app()
                hub_runner = hub.web.AppRunner(hub_app, access_log=None)
                await hub_runner.setup()
                hub_site = hub.web.TCPSite(hub_runner, "127.0.0.1", 0)
                await hub_site.start()
                hub_port = hub_site._server.sockets[0].getsockname()[1]

                async with aiohttp.ClientSession(
                    auto_decompress=False
                ) as client:
                    response = await client.post(
                        f"http://127.0.0.1:{hub_port}/v1/messages",
                        headers={
                            "authorization": "Bearer fixture-local-token",
                            "anthropic-version": "2023-06-01",
                        },
                        json={
                            "model": "fast,custom-model",
                            "max_tokens": 1,
                            "messages": [
                                {"role": "user", "content": "fixture"}
                            ],
                        },
                    )
                    body = await response.read()
                    self.assertEqual(response.status, 429)
                    self.assertEqual(
                        response.headers["content-encoding"],
                        "gzip",
                    )
                    self.assertEqual(body, expected_body)
                    self.assertEqual(
                        json.loads(gzip.decompress(body)),
                        expected_json,
                    )
            finally:
                if hub_runner is not None:
                    await hub_runner.cleanup()
                if upstream_runner is not None:
                    await upstream_runner.cleanup()

        asyncio.run(scenario())

    def test_runtime_config_and_database_errors_are_sanitized_json(self):
        request = SimpleNamespace(method="POST", path="/v1/messages")

        async def bad_config(_request):
            raise hub.ConfigError(
                "secret-token at https://private-upstream.invalid/config"
            )

        async def bad_database(_request):
            raise sqlite3.DatabaseError(
                "fixture-upstream-token https://private-upstream.invalid"
            )

        for handler, expected in (
            (bad_config, "configuration is unavailable"),
            (bad_database, "provider database is unavailable"),
        ):
            with self.subTest(handler=handler.__name__):
                response = asyncio.run(
                    hub.controlled_error_middleware(request, handler)
                )
                serialized = response.text
                self.assertEqual(response.status, 503)
                self.assertEqual(response.content_type, "application/json")
                self.assertIn(expected, serialized)
                self.assertNotIn("secret-token", serialized)
                self.assertNotIn("fixture-upstream-token", serialized)
                self.assertNotIn("private-upstream", serialized)

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
            query_string = "beta=true"

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
        self.assertEqual(response.headers["x-hub-token-count-source"], "estimate")
        self.assertEqual(response.headers["x-hub-token-count-exact"], "0")
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

    def test_native_count_tokens_marks_upstream_result_as_exact(self):
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [b'{"input_tokens":17}'],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
            path="/v1/messages/count_tokens",
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.headers["x-hub-token-count-source"], "upstream")
        self.assertEqual(
            response.headers["x-hub-token-count-method"],
            "anthropic_count_tokens",
        )
        self.assertEqual(response.headers["x-hub-token-count-exact"], "1")
        self.assertNotIn("x-hub-estimated", response.headers)

    def test_account_pool_round_robin_is_shared_across_requests(self):
        self._write_account_pool_db()
        self._write_account_pool_config()

        observed_tokens = []
        for _ in range(2):
            upstream = _FakeUpstream(
                200,
                {"Content-Type": "application/json"},
                [b'{"usage":{"input_tokens":1,"output_tokens":1}}'],
            )
            session = _FakeSession(upstream)
            request = self._request(
                {
                    "model": "fast,pooled-model",
                    "messages": [{"role": "user", "content": "fixture"}],
                },
                session=session,
            )
            with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
                response = asyncio.run(hub.handle_messages(request))
            self.assertEqual(response.status, 200)
            observed_tokens.append(session.calls[0][1]["headers"]["x-api-key"])

        self.assertEqual(
            observed_tokens,
            [
                "fixture-primary-account-token",
                "fixture-secondary-account-token",
            ],
        )
        self.assertTrue(self.account_pool_state.is_file())
        self.assertNotIn(
            b"fixture-primary-account-token",
            self.account_pool_state.read_bytes(),
        )
        if hub._usage_fp is not None:
            hub._usage_fp.flush()
        usage_rows = [
            json.loads(line)
            for line in self.usage_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [row["account"] for row in usage_rows[-2:]],
            ["id:primary", "id:secondary"],
        )

    def test_account_pool_429_retries_next_key_before_downstream_prepare(self):
        self._write_account_pool_db()
        self._write_account_pool_config()
        session = _SequencedFakeSession(
            [
                _FakeUpstream(
                    429,
                    {
                        "Content-Type": "application/json",
                        "Retry-After": "120",
                    },
                    [b'{"type":"error"}'],
                ),
                _FakeUpstream(
                    200,
                    {"Content-Type": "application/json"},
                    [b'{"usage":{"input_tokens":1,"output_tokens":1}}'],
                ),
            ]
        )
        request = self._request(
            {
                "model": "fast,pooled-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            [call[1]["headers"]["x-api-key"] for call in session.calls],
            [
                "fixture-primary-account-token",
                "fixture-secondary-account-token",
            ],
        )
        self.assertEqual(response.headers["x-hub-account"], "id:secondary")

        next_session = _FakeSession(
            _FakeUpstream(
                200,
                {"Content-Type": "application/json"},
                [b'{"usage":{}}'],
            )
        )
        next_request = self._request(
            {"model": "fast,pooled-model", "messages": []},
            session=next_session,
        )
        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            asyncio.run(hub.handle_messages(next_request))
        self.assertEqual(
            next_session.calls[0][1]["headers"]["x-api-key"],
            "fixture-secondary-account-token",
        )

    def test_account_pool_never_retries_a_stream_after_downstream_commit(self):
        self._write_account_pool_db()
        self._write_account_pool_config()
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [b'event: message_start\ndata: {"type":"message_start"}\n\n'],
            fail_after=True,
        )
        session = _FakeSession(upstream)
        request = self._request(
            {"model": "fast,pooled-model", "stream": True, "messages": []},
            session=session,
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        self.assertEqual(len(session.calls), 1)

    def test_account_pool_failover_also_wraps_openai_transformation(self):
        self._write_account_pool_db(api_format="openai_chat")
        self._write_account_pool_config()
        transformed = {
            "id": "pooled-response",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        session = _SequencedFakeSession(
            [
                _FakeUpstream(
                    429,
                    {"Content-Type": "application/json", "Retry-After": "60"},
                    [b'{"error":{"message":"limited"}}'],
                ),
                _FakeUpstream(
                    200,
                    {"Content-Type": "application/json"},
                    [json.dumps(transformed).encode("utf-8")],
                ),
            ]
        )
        request = self._request(
            {
                "model": "fast,pooled-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": False,
            },
            session=session,
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(
            [call[1]["headers"]["authorization"] for call in session.calls],
            [
                "Bearer fixture-primary-account-token",
                "Bearer fixture-secondary-account-token",
            ],
        )
        self.assertEqual(response.headers["x-hub-account"], "id:secondary")

    def test_transformed_final_429_preserves_retry_after_and_account(self):
        self._write_account_pool_db(api_format="openai_chat")
        self._write_account_pool_config()
        session = _SequencedFakeSession(
            [
                _FakeUpstream(
                    429,
                    {"Content-Type": "application/json", "Retry-After": "30"},
                    [b'{"error":{"message":"limited-primary"}}'],
                ),
                _FakeUpstream(
                    429,
                    {"Content-Type": "application/json", "Retry-After": "45"},
                    [b'{"error":{"message":"limited-secondary"}}'],
                ),
            ]
        )
        request = self._request(
            {"model": "fast,pooled-model", "messages": []},
            session=session,
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers["retry-after"], "45")
        self.assertEqual(response.headers["x-hub-account"], "id:secondary")

    def test_channel_api_format_override_does_not_break_single_account(self):
        self._write_account_pool_db(api_format="anthropic")
        self._write_config(
            channels={
                "fast": {
                    "provider": "id:primary",
                    "models": ["pooled-model"],
                    "api_format": "openai_chat",
                }
            },
            default_channel="fast",
        )
        transformed = {
            "id": "override-response",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        session = _FakeSession(
            _FakeUpstream(
                200,
                {"Content-Type": "application/json"},
                [json.dumps(transformed).encode("utf-8")],
            )
        )
        request = self._request(
            {"model": "fast,pooled-model", "messages": []},
            session=session,
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(len(session.calls), 1)

    def test_auth_disabled_pool_is_not_reported_as_rate_limit(self):
        response = hub._account_pool_error(
            hub.PoolExhausted(reason="auth_disabled")
        )

        self.assertEqual(response.status, 503)
        self.assertNotIn("retry-after", response.headers)
        self.assertIn("credentials", response.text)

    def test_account_pool_member_with_different_endpoint_fails_closed(self):
        self._write_account_pool_db()
        self._write_account_pool_config()
        connection = sqlite3.connect(self.db_file)
        try:
            settings = json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://different.invalid/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-secondary-account-token",
                    }
                }
            )
            connection.execute(
                "UPDATE providers SET settings_config=? WHERE id='secondary'",
                (settings,),
            )
            connection.commit()
        finally:
            connection.close()
        session = _NeverSession()
        request = self._request(
            {"model": "fast,pooled-model", "messages": []},
            session=session,
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 502)
        self.assertIn("account pool", response.text)

    def test_anthropic_full_url_provider_uses_the_complete_endpoint(self):
        endpoint = "http://127.0.0.1:19090/custom/messages"
        self._set_provider_endpoint("Fixture HTTPS", endpoint, "anthropic")

        upstream = _FakeUpstream(
            429,
            {"Content-Type": "application/json"},
            [b'{"type":"error","error":{"type":"rate_limit_error"}}'],
        )
        session = _FakeSession(upstream)
        request = self._request(
            {"model": "fast,custom-model", "messages": []},
            session=session,
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            asyncio.run(hub.handle_messages(request))

        self.assertEqual(session.calls[0][0], endpoint)

    def test_anthropic_full_url_count_tokens_is_estimated_locally(self):
        endpoint = "http://127.0.0.1:19090/v1/messages"
        self._set_provider_endpoint("Fixture HTTPS", endpoint, "anthropic")
        session = _NeverSession()
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
            path="/v1/messages/count_tokens",
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["x-hub-estimated"], "1")
        self.assertEqual(response.headers["x-hub-token-count-source"], "estimate")
        self.assertEqual(
            response.headers["x-hub-token-count-method"],
            "json_utf8_bytes_div_4",
        )
        self.assertEqual(response.headers["x-hub-token-count-exact"], "0")
        self.assertEqual(response.headers["x-hub-token-count-error-bound"], "unbounded")
        self.assertEqual(session.calls, [])

    def test_transformed_count_tokens_validates_payload_before_estimating(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        session = _NeverSession()
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "future_native_block", "opaque": True}],
                    }
                ],
            },
            session=session,
            path="/v1/messages/count_tokens",
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 400)
        self.assertEqual(
            response.headers["x-hub-protocol-code"],
            "HUB_UNSUPPORTED_CONTENT_BLOCK",
        )
        body = json.loads(response.text)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertIn("$.messages[0].content[0]", body["error"]["message"])
        self.assertEqual(session.calls, [])

    def test_transformed_count_tokens_estimate_marks_channel_headers(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        session = _NeverSession()
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
            path="/v1/messages/count_tokens",
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["x-hub-channel"], "fast")
        self.assertEqual(response.headers["x-hub-model"], "custom-model")
        self.assertEqual(response.headers["x-hub-token-count-source"], "estimate")
        self.assertEqual(response.headers["x-hub-token-count-exact"], "0")
        self.assertGreater(json.loads(response.text)["input_tokens"], 0)
        self.assertEqual(session.calls, [])

    def test_native_count_tokens_estimate_fallback_marks_channel_headers(self):
        upstream = _FakeUpstream(
            404,
            {"Content-Type": "application/json"},
            [b'{"type":"error","error":{"type":"not_found_error","message":"nope"}}'],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
            path="/v1/messages/count_tokens",
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["x-hub-channel"], "fast")
        self.assertEqual(response.headers["x-hub-model"], "custom-model")
        self.assertEqual(response.headers["x-hub-token-count-source"], "estimate")
        self.assertEqual(response.headers["x-hub-token-count-exact"], "0")

    def test_transformed_upstream_error_carries_request_protocol_warnings(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            429,
            {"Content-Type": "application/json"},
            [
                b'{"error":{"message":"slow down",'
                b'"type":"rate_limit_error","code":"rate_limit"}}'
            ],
        )
        session = _FakeSession(upstream)
        request = self._request(
            {
                "model": "fast,custom-model",
                "system": [
                    {
                        "type": "text",
                        "text": "fixture system",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 429)
        self.assertEqual(
            response.headers["x-hub-protocol-warnings"],
            "HUB_DEGRADE_SYSTEM_METADATA_DROPPED",
        )
        self.assertEqual(response.headers["x-hub-channel"], "fast")
        self.assertEqual(response.headers["x-hub-model"], "custom-model")
        body = json.loads(response.text)
        self.assertEqual(body["error"]["type"], "rate_limit_error")

    def test_full_url_provider_rejects_request_query_strings(self):
        endpoint = "http://127.0.0.1:19090/v1/messages"
        self._set_provider_endpoint("Fixture HTTPS", endpoint, "anthropic")
        session = _NeverSession()
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
            query="?beta=true",
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 400)
        body = json.loads(response.text)
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertIn("query string", body["error"]["message"])
        self.assertEqual(session.calls, [])

    def test_transformed_full_url_provider_uses_the_complete_endpoint(self):
        endpoint = "http://127.0.0.1:19090/custom/chat"
        self._set_provider_endpoint("Fixture HTTPS", endpoint, "openai_chat")
        upstream_payload = {
            "id": "fixture-response",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
        }
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps(upstream_payload).encode()],
        )
        session = _FakeSession(upstream)
        request = self._request(
            {"model": "fast,custom-model", "messages": []},
            session=session,
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(session.calls[0][0], endpoint)

    def test_check_uses_a_full_url_providers_complete_endpoint(self):
        endpoint = "http://127.0.0.1:19090/custom/messages"
        self._set_provider_endpoint("Fixture HTTPS", endpoint, "anthropic")

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

        class FakeSession:
            def __init__(self):
                self.calls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc, _traceback):
                return False

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse()

        session = FakeSession()
        with mock.patch.object(
            hub.aiohttp, "ClientSession", return_value=session
        ), redirect_stdout(io.StringIO()):
            asyncio.run(hub.cli_check("fast"))

        self.assertEqual(session.calls[0][0], endpoint)

    def test_check_uses_each_openai_protocols_endpoint_headers_and_payload(self):
        connection = sqlite3.connect(self.db_file)
        try:
            connection.execute("ALTER TABLE providers ADD COLUMN meta TEXT")
            connection.commit()
        finally:
            connection.close()

        cases = (
            (
                "openai_chat",
                "http://127.0.0.1:19090",
                False,
                "http://127.0.0.1:19090/v1/chat/completions",
                "messages",
            ),
            (
                "openai_responses",
                "http://127.0.0.1:19090/custom/responses",
                True,
                "http://127.0.0.1:19090/custom/responses",
                "input",
            ),
        )

        for api_format, base_url, is_full_url, expected_url, payload_key in cases:
            with self.subTest(api_format=api_format):
                connection = sqlite3.connect(self.db_file)
                try:
                    connection.execute(
                        "UPDATE providers SET settings_config=?, meta=? WHERE name=?",
                        (
                            json.dumps(
                                {
                                    "env": {
                                        "ANTHROPIC_BASE_URL": base_url,
                                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                                    }
                                }
                            ),
                            json.dumps(
                                {
                                    "isFullUrl": is_full_url,
                                    "apiFormat": api_format,
                                }
                            ),
                            "Fixture HTTPS",
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
                hub.reset_caches()

                class FakeResponse:
                    status = 200

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, _exc_type, _exc, _traceback):
                        return False

                class FakeSession:
                    def __init__(self):
                        self.calls = []

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, _exc_type, _exc, _traceback):
                        return False

                    def post(self, url, **kwargs):
                        self.calls.append((url, kwargs))
                        return FakeResponse()

                session = FakeSession()
                with mock.patch.object(
                    hub.aiohttp, "ClientSession", return_value=session
                ), redirect_stdout(io.StringIO()):
                    asyncio.run(hub.cli_check("fast"))

                url, kwargs = session.calls[0]
                self.assertEqual(url, expected_url)
                self.assertIn(payload_key, kwargs["json"])
                self.assertEqual(
                    kwargs["headers"]["authorization"],
                    "Bearer fixture-upstream-token",
                )
                self.assertNotIn("x-api-key", kwargs["headers"])

    def test_forwarding_preserves_protocol_fields_headers_query_and_error_body(self):
        chunks = [b'{"type":"error",', b'"error":{"type":"rate_limit_error"}}']
        upstream = _FakeUpstream(
            429,
            CIMultiDict(
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Encoding", "identity"),
                    ("X-Upstream-Trace", "fixture-trace-a"),
                    ("X-Upstream-Trace", "fixture-trace-b"),
                    ("Connection", "X-Remove-Me"),
                    ("X-Remove-Me", "hop-by-hop"),
                ]
            ),
            chunks,
        )
        session = _FakeSession(upstream)
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "metadata": {"unknown_future_field": True},
                "another_unknown_field": [1, 2, 3],
            },
            session=session,
            query="?beta=true&limit=1",
            headers=CIMultiDict(
                [
                    ("anthropic-version", "2023-06-01"),
                    ("anthropic-beta", "claude-code-20250219"),
                    ("anthropic-beta", "context-management-2025-06-27"),
                    ("x-claude-code-version", "2.1.207"),
                    ("x-claude-code-feature", "future-a"),
                    ("x-claude-code-feature", "future-b"),
                    ("x-custom-forwarded", "kept"),
                    ("content-encoding", "identity"),
                    ("x-api-key", "fixture-local-token"),
                    ("host", "127.0.0.1:18787"),
                    ("connection", "x-request-hop"),
                    ("x-request-hop", "remove-me"),
                ]
            ),
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(
            url,
            "https://upstream.invalid/v1/messages?beta=true&limit=1",
        )
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(
            kwargs["headers"]["x-claude-code-version"], "2.1.207"
        )
        self.assertEqual(
            kwargs["headers"].getall("x-claude-code-feature"),
            ["future-a", "future-b"],
        )
        self.assertEqual(
            kwargs["headers"].getall("anthropic-beta"),
            [
                "claude-code-20250219",
                "context-management-2025-06-27",
            ],
        )
        self.assertEqual(kwargs["headers"]["x-custom-forwarded"], "kept")
        self.assertNotIn(
            "content-encoding",
            {key.lower() for key in kwargs["headers"]},
        )
        self.assertNotIn("host", {key.lower() for key in kwargs["headers"]})
        self.assertNotIn(
            "x-request-hop",
            {key.lower() for key in kwargs["headers"]},
        )
        self.assertEqual(
            kwargs["headers"]["authorization"],
            "Bearer fixture-upstream-token",
        )
        self.assertEqual(
            kwargs["headers"]["x-api-key"], "fixture-upstream-token"
        )
        forwarded = json.loads(kwargs["data"])
        self.assertEqual(forwarded["model"], "custom-model")
        self.assertEqual(
            forwarded["metadata"], {"unknown_future_field": True}
        )
        self.assertEqual(forwarded["another_unknown_field"], [1, 2, 3])

        self.assertEqual(response.status, 429)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/json; charset=utf-8",
        )
        self.assertEqual(response.headers["Content-Encoding"], "identity")
        self.assertEqual(
            response.headers.getall("X-Upstream-Trace"),
            ["fixture-trace-a", "fixture-trace-b"],
        )
        self.assertNotIn("X-Remove-Me", response.headers)
        self.assertEqual(response.writes, chunks)
        self.assertEqual(b"".join(response.writes), b"".join(chunks))
        self.assertTrue(response.eof)

    def test_native_forwarding_promotes_system_role_for_strict_upstreams(self):
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [b'{"type":"message","content":[]}'],
        )
        session = _FakeSession(upstream)
        request = self._request(
            {
                "model": "fast,strict-model",
                "system": [
                    {
                        "type": "text",
                        "text": "top-level",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [
                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "text",
                                "text": "machine context",
                                "cache_control": {"type": "ephemeral", "ttl": "1h"},
                            }
                        ],
                    },
                    {"role": "user", "content": "hello"},
                ],
                "future_native_extension": {"opaque": True},
            },
            session=session,
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            asyncio.run(hub.handle_messages(request))

        forwarded = json.loads(session.calls[0][1]["data"])
        self.assertEqual(
            forwarded["system"],
            [
                {
                    "type": "text",
                    "text": "top-level",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": "machine context",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
            ],
        )
        self.assertEqual(forwarded["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(forwarded["future_native_extension"], {"opaque": True})

    def test_ambiguous_upstream_representation_headers_fail_before_prepare(self):
        cases = {
            "duplicate content type": CIMultiDict(
                [
                    ("Content-Type", "application/json"),
                    ("Content-Type", "text/event-stream"),
                ]
            ),
            "comma folded content type": CIMultiDict(
                [
                    (
                        "Content-Type",
                        "application/json, text/event-stream",
                    )
                ]
            ),
            "connection content type": CIMultiDict(
                [
                    ("Content-Type", "text/event-stream"),
                    ("Connection", "Content-Type"),
                ]
            ),
            "connection content encoding": CIMultiDict(
                [
                    ("Content-Type", "text/event-stream"),
                    ("Content-Encoding", "gzip"),
                    ("Connection", "Content-Encoding"),
                ]
            ),
        }
        for label, headers in cases.items():
            with self.subTest(label=label):
                upstream = _FakeUpstream(200, headers, [b"fixture"])
                request = self._request(
                    {
                        "model": "fast,custom-model",
                        "messages": [
                            {"role": "user", "content": "fixture"}
                        ],
                    },
                    session=_FakeSession(upstream),
                )
                with mock.patch.object(
                    hub.web,
                    "StreamResponse",
                    side_effect=AssertionError("downstream was prepared"),
                ):
                    response = asyncio.run(hub.handle_messages(request))

                self.assertEqual(response.status, 502)
                self.assertEqual(upstream.content.events, [])

    def test_request_connection_cannot_remove_representation_headers(self):
        session = _NeverSession()
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
            headers=CIMultiDict(
                [
                    ("authorization", "Bearer fixture-local-token"),
                    ("content-type", "application/json"),
                    ("connection", "Content-Type"),
                ]
            ),
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 400)
        self.assertEqual(session.calls, [])

    def test_sse_chunks_are_forwarded_immediately_and_never_replayed(self):
        chunks = [b"event: message_start\n", b"data: one\n\n"]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
            fail_after=True,
        )
        session = _FakeSession(upstream)
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
        )

        downstream = _FakeDownstream(200)
        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(downstream.writes, chunks)
        self.assertFalse(downstream.eof)
        self.assertTrue(request.transport.aborted)
        self.assertEqual(upstream.content.events, ["yield", "yield", "raise"])

    def test_sse_abort_log_includes_content_free_stream_metrics(self):
        secret = b"secret-response-fragment"
        chunks = [b"event: message_start\n", secret]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
            fail_after=True,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=_FakeDownstream(200),
        ), mock.patch.object(hub, "log") as write_log:
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        rendered_log = "\n".join(
            call.args[0] for call in write_log.call_args_list
        )
        self.assertIn("first_chunk_ms=", rendered_log)
        self.assertIn("max_gap_ms=", rendered_log)
        self.assertIn("chunks=2", rendered_log)
        self.assertIn(
            f"upstream_bytes={sum(map(len, chunks))}",
            rendered_log,
        )
        self.assertIn("terminal=error", rendered_log)
        self.assertIn("error=ClientPayloadError", rendered_log)
        self.assertNotIn(secret.decode(), rendered_log)

    def test_sse_success_log_includes_stream_metrics_and_terminal_state(self):
        chunks = [
            b"event: message_start\ndata: {}\n\n",
            b"event: message_stop\ndata: {}\n\n",
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ), mock.patch.object(hub, "log") as write_log:
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        rendered_log = "\n".join(
            call.args[0] for call in write_log.call_args_list
        )
        self.assertIn("first_chunk_ms=", rendered_log)
        self.assertIn("max_gap_ms=", rendered_log)
        self.assertIn("chunks=2", rendered_log)
        self.assertIn("terminal=complete", rendered_log)

    def test_sse_missing_terminal_log_preserves_stream_metrics(self):
        chunks = [b"event: message_start\ndata: {}\n\n"]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=_FakeDownstream(200),
        ), mock.patch.object(hub, "log") as write_log:
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        rendered_log = "\n".join(
            call.args[0] for call in write_log.call_args_list
        )
        self.assertIn("first_chunk_ms=", rendered_log)
        self.assertIn("max_gap_ms=", rendered_log)
        self.assertIn("upstream_bytes=31", rendered_log)
        self.assertIn("downstream_bytes=31", rendered_log)
        self.assertIn("terminal=missing", rendered_log)

    def test_sse_clean_eof_without_terminal_event_is_aborted(self):
        chunks = [
            b"event: message_start\ndata: {}\n\n",
            b"event: content_block_delta\ndata: {\"delta\":\"partial\"}\n\n",
            b"event: message_stop\n",
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "Text/Event-Stream; charset=utf-8"},
            chunks,
        )
        session = _FakeSession(upstream)
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(downstream.writes, chunks)
        self.assertFalse(downstream.eof)
        self.assertTrue(request.transport.aborted)

    def test_sse_terminal_tracker_bounds_oversized_lines(self):
        tracker = hub._SSETerminalTracker()

        tracker.feed(b"data: " + b"x" * (hub.SSE_LINE_LIMIT + 1))
        self.assertEqual(len(tracker._line), 0)
        self.assertTrue(tracker._discarding_line)
        tracker.feed(b"\nevent: message_stop\ndata: {}\n\n")

        self.assertTrue(tracker.terminal)
        self.assertFalse(tracker._discarding_line)

    def test_sse_terminal_event_requires_dispatching_blank_line(self):
        tracker = hub._SSETerminalTracker()

        tracker.feed(b"event: message_stop\ndata: {}\n")
        self.assertFalse(tracker.terminal)
        tracker.feed(b"\n")

        self.assertTrue(tracker.terminal)

    def test_sse_event_without_data_is_not_dispatched(self):
        tracker = hub._SSETerminalTracker()

        tracker.feed(b"event: message_stop\n\n")
        tracker.finish()

        self.assertFalse(tracker.terminal)
        self.assertFalse(tracker.complete)

    def test_sse_oversized_event_field_overrides_terminal_candidate(self):
        tracker = hub._SSETerminalTracker()

        tracker.feed(b"event: message_stop\ndata: {}\n")
        tracker.feed(
            b"event: " + b"x" * (hub.SSE_LINE_LIMIT + 1) + b"\n\n"
        )
        tracker.finish()

        self.assertFalse(tracker.terminal)
        self.assertFalse(tracker.complete)

    def test_sse_empty_event_field_overrides_terminal_candidate(self):
        tracker = hub._SSETerminalTracker()

        tracker.feed(
            b"event: message_stop\ndata: {}\nevent\ndata: {}\n\n"
        )
        tracker.finish()

        self.assertFalse(tracker.terminal)
        self.assertFalse(tracker.complete)

    def test_sse_rejects_protocol_events_after_terminal_but_allows_comments(self):
        valid = b"event: message_stop\ndata: {}\n\n"

        continued = hub._SSETerminalTracker()
        continued.feed(
            valid + b"event: content_block_delta\ndata: {}\n\n"
        )
        continued.finish()
        self.assertTrue(continued.terminal)
        self.assertTrue(continued.protocol_error)
        self.assertFalse(continued.complete)

        commented = hub._SSETerminalTracker()
        commented.feed(valid + b": provider heartbeat\n\n")
        commented.finish()
        self.assertTrue(commented.complete)

    def test_native_sse_protocol_error_is_aborted_before_the_bad_chunk_is_written(self):
        invalid = (
            b"event: message_stop\ndata: {}\n\n"
            b"event: content_block_delta\ndata: {}\n\n"
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [invalid],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(hub.web, "StreamResponse", return_value=downstream):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        self.assertEqual(downstream.writes, [])
        self.assertFalse(downstream.eof)
        self.assertTrue(request.transport.aborted)

    def test_sse_invalid_or_incomplete_utf8_is_not_complete(self):
        invalid = hub._SSETerminalTracker()
        invalid.feed(b"event: message_stop\ndata: \xff\n\n")
        invalid.finish()
        self.assertFalse(invalid.complete)

        incomplete = hub._SSETerminalTracker()
        incomplete.feed(
            b"event: message_stop\ndata: {}\n\n: trailing \xe2"
        )
        incomplete.finish()
        self.assertFalse(incomplete.complete)

    def test_sse_allows_one_initial_utf8_bom_across_chunks(self):
        tracker = hub._SSETerminalTracker()

        tracker.feed(codecs.BOM_UTF8[:1])
        tracker.feed(
            codecs.BOM_UTF8[1:]
            + b"event: error\ndata: {}\n\n"
        )
        tracker.finish()

        self.assertTrue(tracker.complete)

    def test_sse_message_stop_and_error_are_valid_terminal_events(self):
        for terminal in ("message_stop", "error"):
            with self.subTest(terminal=terminal):
                chunks = [
                    b"event: message_start\ndata: {}\n\n",
                    f"event: {terminal[:4]}".encode(),
                    f"{terminal[4:]}\r\ndata: {{}}\r\n\r\n".encode(),
                ]
                upstream = _FakeUpstream(
                    200,
                    {
                        "Content-Type": (
                            'text/event-stream; note="quoted,parameter"'
                        )
                    },
                    chunks,
                )
                request = self._request(
                    {
                        "model": "fast,custom-model",
                        "messages": [{"role": "user", "content": "fixture"}],
                    },
                    session=_FakeSession(upstream),
                )
                downstream = _FakeDownstream(200)

                with mock.patch.object(
                    hub.web,
                    "StreamResponse",
                    return_value=downstream,
                ):
                    response = asyncio.run(hub.handle_messages(request))

                self.assertIs(response, downstream)
                self.assertEqual(downstream.writes, chunks)
                self.assertTrue(downstream.eof)
                self.assertFalse(request.transport.aborted)

    def test_sse_cr_only_line_endings_dispatch_terminal_event(self):
        chunks = [
            b"event: message_start\rdata: {}\r\r",
            b"event: message_",
            b"stop\rdata: {}\r\r",
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertEqual(downstream.writes, chunks)
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)

    def test_sse_unsupported_content_encoding_fails_before_prepare(self):
        upstream = _FakeUpstream(
            200,
            {
                "Content-Type": "text/event-stream",
                "Content-Encoding": "br",
            },
            [b"compressed fixture"],
        )
        session = _FakeSession(upstream)
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=session,
        )

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            side_effect=AssertionError("downstream was prepared"),
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 502)
        self.assertEqual(upstream.content.events, [])

    def test_deflate_sse_is_forwarded_raw_and_validated(self):
        payload = (
            b"event: message_start\ndata: {}\n\n"
            b"event: message_stop\ndata: {}\n\n"
        )
        compressed = zlib.compress(payload)
        chunks = [compressed[:7], compressed[7:19], compressed[19:]]
        upstream = _FakeUpstream(
            200,
            {
                "Content-Type": "text/event-stream",
                "Content-Encoding": "deflate",
            },
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertEqual(downstream.writes, chunks)
        self.assertEqual(zlib.decompress(b"".join(chunks)), payload)
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)

    def test_truncated_gzip_sse_is_aborted_even_after_terminal_text(self):
        payload = (
            b"event: message_start\ndata: {}\n\n"
            b"event: message_stop\ndata: {}\n\n"
        )
        truncated = gzip.compress(payload, mtime=0)[:-4]
        upstream = _FakeUpstream(
            200,
            {
                "Content-Type": "text/event-stream",
                "Content-Encoding": "gzip",
            },
            [truncated],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        self.assertEqual(downstream.writes, [truncated])
        self.assertFalse(downstream.eof)
        self.assertTrue(request.transport.aborted)

    def test_gzip_member_after_terminal_cannot_hide_unfinished_event(self):
        terminal_member = gzip.compress(
            b"event: message_stop\ndata: {}\n\n",
            mtime=0,
        )
        trailing_member = gzip.compress(
            b"event: content_block_delta\ndata: {\"delta\":\"partial\"}\n\n",
            mtime=0,
        )
        compressed = terminal_member + trailing_member
        upstream = _FakeUpstream(
            200,
            {
                "Content-Type": "text/event-stream",
                "Content-Encoding": "gzip",
            },
            [compressed],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        # The decoded chunk contains protocol data after the terminal event.
        # Fail closed before forwarding the corresponding raw bytes.
        self.assertEqual(downstream.writes, [])
        self.assertFalse(downstream.eof)
        self.assertTrue(request.transport.aborted)

    def test_sse_decoder_bounds_chunks_expansion_total_and_member_count(self):
        expanded = b"\n" * (8 * 1024 * 1024)
        decoder = hub._SSEContentDecoder("gzip")
        largest = 0
        with self.assertRaisesRegex(zlib.error, "expansion limit"):
            for part in decoder.feed(gzip.compress(expanded, mtime=0)):
                largest = max(largest, len(part))
        self.assertLessEqual(largest, hub.SSE_DECODE_CHUNK)

        decoder = hub._SSEContentDecoder("gzip")
        with mock.patch.object(hub, "SSE_DECODE_TOTAL_LIMIT", 1024):
            with self.assertRaisesRegex(zlib.error, "size limit"):
                list(
                    decoder.feed(
                        gzip.compress(b"x" * 2048, mtime=0)
                    )
                )

        member = gzip.compress(b": heartbeat\n\n", mtime=0)
        decoder = hub._SSEContentDecoder("gzip")
        with self.assertRaisesRegex(zlib.error, "too many gzip members"):
            list(decoder.feed(member * (hub.SSE_GZIP_MEMBER_LIMIT + 1)))

    def test_compressed_sse_yields_control_between_decoded_chunks(self):
        payload = (
            b":" + b"x" * (hub.SSE_DECODE_CHUNK * 2) + b"\n\n"
            b"event: message_stop\ndata: {}\n\n"
        )
        compressed = gzip.compress(payload, mtime=0)
        upstream = _FakeUpstream(
            200,
            {
                "Content-Type": "text/event-stream",
                "Content-Encoding": "gzip",
            },
            [compressed],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web,
            "StreamResponse",
            return_value=downstream,
        ), mock.patch.object(
            hub.asyncio,
            "sleep",
            new=mock.AsyncMock(),
        ) as yield_control:
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertGreaterEqual(yield_control.await_count, 2)
        self.assertTrue(downstream.eof)

    def test_real_loopback_client_observes_midstream_transfer_failure_once(self):
        async def scenario():
            upstream_calls = 0
            upstream_runner = None
            hub_runner = None
            client = None
            hub_session = None

            async def broken_upstream(request):
                nonlocal upstream_calls
                upstream_calls += 1
                await request.read()
                response = hub.web.StreamResponse(
                    status=200,
                    headers={"content-type": "text/event-stream"},
                )
                await response.prepare(request)
                await response.write(b"event: message_start\n")
                await asyncio.sleep(0.01)
                await response.write(b"data: fixture-before-break\n\n")
                await asyncio.sleep(0.03)
                request.transport.abort()
                return response

            try:
                upstream_app = hub.web.Application()
                upstream_app.router.add_post("/v1/messages", broken_upstream)
                upstream_runner = hub.web.AppRunner(upstream_app, access_log=None)
                await upstream_runner.setup()
                upstream_site = hub.web.TCPSite(
                    upstream_runner,
                    "127.0.0.1",
                    0,
                )
                await upstream_site.start()
                upstream_port = upstream_site._server.sockets[0].getsockname()[1]

                connection = sqlite3.connect(self.db_file)
                try:
                    updated_env = {
                        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{upstream_port}/v1",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                    }
                    connection.execute(
                        "UPDATE providers SET settings_config=? WHERE name=?",
                        (
                            json.dumps({"env": updated_env}),
                            "Fixture HTTPS",
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
                self.db_file.chmod(0o600)

                hub_app = hub.create_app()
                hub_runner = hub.web.AppRunner(hub_app, access_log=None)
                await hub_runner.setup()
                hub_session = hub_app[hub.UPSTREAM_SESSION_KEY]
                self.assertFalse(hub_session.closed)
                hub_site = hub.web.TCPSite(hub_runner, "127.0.0.1", 0)
                await hub_site.start()
                hub_port = hub_site._server.sockets[0].getsockname()[1]

                client = aiohttp.ClientSession(auto_decompress=False)
                response = await client.post(
                    f"http://127.0.0.1:{hub_port}/v1/messages",
                    headers={
                        "authorization": "Bearer fixture-local-token",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "fast,custom-model",
                        "max_tokens": 16,
                        "messages": [
                            {"role": "user", "content": "fixture"}
                        ],
                    },
                )
                self.assertEqual(response.status, 200)
                received = bytearray()
                transfer_error = None

                async def consume():
                    nonlocal transfer_error
                    try:
                        async for chunk in response.content.iter_any():
                            received.extend(chunk)
                    except aiohttp.ClientError as exc:
                        transfer_error = exc

                await asyncio.wait_for(consume(), timeout=3)
                response.close()

                self.assertEqual(upstream_calls, 1)
                self.assertIn(b"fixture-before-break", bytes(received))
                self.assertIsNotNone(transfer_error)
                self.assertIsInstance(
                    transfer_error,
                    (
                        aiohttp.ClientPayloadError,
                        aiohttp.ServerDisconnectedError,
                        aiohttp.ClientConnectionError,
                    ),
                )
            finally:
                if client is not None:
                    await client.close()
                if hub_runner is not None:
                    await hub_runner.cleanup()
                if upstream_runner is not None:
                    await upstream_runner.cleanup()
                if hub_session is not None:
                    self.assertTrue(hub_session.closed)

        asyncio.run(scenario())

    def test_real_loopback_clean_sse_eof_without_terminal_is_failure(self):
        async def scenario():
            upstream_calls = 0
            upstream_runner = None
            hub_runner = None
            client = None

            async def incomplete_upstream(request):
                nonlocal upstream_calls
                upstream_calls += 1
                await request.read()
                response = hub.web.StreamResponse(
                    status=200,
                    headers={"content-type": "text/event-stream"},
                )
                await response.prepare(request)
                await response.write(
                    b"event: message_start\ndata: {}\n\n"
                )
                await response.write(
                    b"event: content_block_delta\ndata: {\"delta\":\"partial\"}\n\n"
                )
                await response.write(b"event: message_stop\n")
                await response.write_eof()
                return response

            try:
                upstream_app = hub.web.Application()
                upstream_app.router.add_post("/v1/messages", incomplete_upstream)
                upstream_runner = hub.web.AppRunner(upstream_app, access_log=None)
                await upstream_runner.setup()
                upstream_site = hub.web.TCPSite(
                    upstream_runner,
                    "127.0.0.1",
                    0,
                )
                await upstream_site.start()
                upstream_port = upstream_site._server.sockets[0].getsockname()[1]

                connection = sqlite3.connect(self.db_file)
                try:
                    updated_env = {
                        "ANTHROPIC_BASE_URL": (
                            f"http://127.0.0.1:{upstream_port}/v1"
                        ),
                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                    }
                    connection.execute(
                        "UPDATE providers SET settings_config=? WHERE name=?",
                        (
                            json.dumps({"env": updated_env}),
                            "Fixture HTTPS",
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
                self.db_file.chmod(0o600)

                hub_app = hub.create_app()
                hub_runner = hub.web.AppRunner(hub_app, access_log=None)
                await hub_runner.setup()
                hub_site = hub.web.TCPSite(hub_runner, "127.0.0.1", 0)
                await hub_site.start()
                hub_port = hub_site._server.sockets[0].getsockname()[1]

                client = aiohttp.ClientSession(auto_decompress=False)
                response = await client.post(
                    f"http://127.0.0.1:{hub_port}/v1/messages",
                    headers={
                        "authorization": "Bearer fixture-local-token",
                        "anthropic-version": "2023-06-01",
                    },
                    json={
                        "model": "fast,custom-model",
                        "max_tokens": 16,
                        "messages": [
                            {"role": "user", "content": "fixture"}
                        ],
                    },
                )
                self.assertEqual(response.status, 200)
                received = bytearray()
                transfer_error = None
                try:
                    async for chunk in response.content.iter_any():
                        received.extend(chunk)
                except aiohttp.ClientError as exc:
                    transfer_error = exc
                finally:
                    response.close()

                self.assertEqual(upstream_calls, 1)
                self.assertIn(b"partial", bytes(received))
                self.assertIn(b"event: message_stop\n", bytes(received))
                self.assertIsNotNone(transfer_error)
            finally:
                if client is not None:
                    await client.close()
                if hub_runner is not None:
                    await hub_runner.cleanup()
                if upstream_runner is not None:
                    await upstream_runner.cleanup()

        asyncio.run(asyncio.wait_for(scenario(), timeout=3))

    def test_real_loopback_gzip_sse_is_forwarded_raw_and_validated(self):
        async def scenario():
            upstream_runner = None
            hub_runner = None
            client = None
            seen_accept_encoding = []
            payload = (
                b"event: message_start\ndata: {}\n\n"
                b"event: message_stop\ndata: {}\n\n"
            )
            compressed = gzip.compress(payload, mtime=0)

            async def compressed_upstream(request):
                seen_accept_encoding.extend(
                    request.headers.getall("Accept-Encoding", [])
                )
                await request.read()
                return hub.web.Response(
                    status=200,
                    body=compressed,
                    headers={
                        "content-type": "text/event-stream",
                        "content-encoding": "gzip",
                    },
                )

            try:
                upstream_app = hub.web.Application()
                upstream_app.router.add_post(
                    "/v1/messages",
                    compressed_upstream,
                )
                upstream_runner = hub.web.AppRunner(
                    upstream_app,
                    access_log=None,
                )
                await upstream_runner.setup()
                upstream_site = hub.web.TCPSite(
                    upstream_runner,
                    "127.0.0.1",
                    0,
                )
                await upstream_site.start()
                upstream_port = upstream_site._server.sockets[0].getsockname()[1]

                connection = sqlite3.connect(self.db_file)
                try:
                    updated_env = {
                        "ANTHROPIC_BASE_URL": (
                            f"http://127.0.0.1:{upstream_port}/v1"
                        ),
                        "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
                    }
                    connection.execute(
                        "UPDATE providers SET settings_config=? WHERE name=?",
                        (
                            json.dumps({"env": updated_env}),
                            "Fixture HTTPS",
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()
                self.db_file.chmod(0o600)

                hub_app = hub.create_app()
                hub_runner = hub.web.AppRunner(hub_app, access_log=None)
                await hub_runner.setup()
                hub_site = hub.web.TCPSite(hub_runner, "127.0.0.1", 0)
                await hub_site.start()
                hub_port = hub_site._server.sockets[0].getsockname()[1]

                client = aiohttp.ClientSession(
                    auto_decompress=False,
                    skip_auto_headers={"Accept-Encoding"},
                )
                response = await client.post(
                    f"http://127.0.0.1:{hub_port}/v1/messages",
                    headers={
                        "authorization": "Bearer fixture-local-token",
                        "anthropic-version": "2023-06-01",
                        # Claude Code advertises br; the hub must not forward it.
                        "accept-encoding": "br, gzip",
                    },
                    json={
                        "model": "fast,custom-model",
                        "max_tokens": 16,
                        "messages": [
                            {"role": "user", "content": "fixture"}
                        ],
                    },
                )
                body = await response.read()

                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["content-encoding"], "gzip")
                self.assertEqual(body, compressed)
                self.assertEqual(gzip.decompress(body), payload)
                self.assertEqual(seen_accept_encoding, ["identity"])
            finally:
                if client is not None:
                    await client.close()
                if hub_runner is not None:
                    await hub_runner.cleanup()
                if upstream_runner is not None:
                    await upstream_runner.cleanup()

        asyncio.run(asyncio.wait_for(scenario(), timeout=3))

    def test_doctor_is_read_only_offline_and_redacts_secrets_and_urls(self):
        self._write_config(
            default_channel="fast",
            channels={
                "fast": {
                    "provider": "Fixture HTTPS",
                    "models": ["claude-sonnet-4"],
                }
            },
        )
        hub.reset_caches()
        config_before = self.config_file.read_bytes()
        db_before = self.db_file.read_bytes()
        config_mtime = self.config_file.stat().st_mtime_ns
        db_mtime = self.db_file.stat().st_mtime_ns
        source_sidecars = hub._sqlite_sidecars(self.db_file)
        self.assertTrue(all(not path.exists() for path in source_sidecars))
        output = io.StringIO()

        with mock.patch.object(
            hub.aiohttp,
            "ClientSession",
            side_effect=AssertionError("doctor attempted network setup"),
        ), redirect_stdout(output):
            status = hub.cli_doctor()

        rendered = output.getvalue()
        self.assertEqual(status, 0)
        self.assertIn("Result: ready", rendered)
        self.assertIn("No provider connection was attempted", rendered)
        for forbidden in (
            "fixture-local-token",
            "fixture-upstream-token",
            "https://upstream.invalid",
            "http://remote.invalid",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(self.config_file.read_bytes(), config_before)
        self.assertEqual(self.db_file.read_bytes(), db_before)
        self.assertEqual(self.config_file.stat().st_mtime_ns, config_mtime)
        self.assertEqual(self.db_file.stat().st_mtime_ns, db_mtime)
        self.assertTrue(all(not path.exists() for path in source_sidecars))

    def test_doctor_reports_insecure_permissions_without_fixing_them(self):
        self.config_file.chmod(0o644)
        output = io.StringIO()

        with redirect_stdout(output):
            status = hub.cli_doctor()

        self.assertEqual(status, 1)
        self.assertIn("permissions 0644 exceed 0600", output.getvalue())
        self.assertEqual(self.config_file.stat().st_mode & 0o777, 0o644)

    def test_doctor_rejects_a_known_full_endpoint_for_the_wrong_format(self):
        self._write_config(
            default_channel="fast",
            channels={
                "fast": {
                    "provider": "Fixture HTTPS",
                    "models": ["claude-sonnet-4"],
                }
            },
        )
        hub.reset_caches()
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "http://127.0.0.1:19090/v1/messages",
            "openai_chat",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            status = hub.cli_doctor()

        self.assertEqual(status, 1)
        self.assertIn("full endpoint format mismatch", output.getvalue())


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
