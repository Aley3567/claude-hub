import asyncio
import codecs
import gzip
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
                "ANTHROPIC_AUTH_TOKEN": "updated-wal-token",
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

        self.assertEqual(downstream.writes, [compressed])
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
                self.assertEqual(seen_accept_encoding, [])
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
