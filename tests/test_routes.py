"""Fixture-generated tests for explicit provider route groups (phase D).

Every provider name, channel alias, model ID and route name below is a
fixture string; no assertion depends on a real provider or local path.
"""

import asyncio
import importlib.util
import ipaddress
import json
import os
import socket
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
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
        from urllib.parse import urlparse

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


class _FakeContent:
    def __init__(self, chunks, fail_after=False):
        self.chunks = list(chunks)
        self.fail_after = fail_after

    async def iter_any(self):
        for chunk in self.chunks:
            yield chunk
        if self.fail_after:
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


def _json_upstream(status, body, extra_headers=None):
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return _FakeUpstream(status, headers, [json.dumps(body).encode()])


class RouteGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_file = root / "hub.json"
        self.db_file = root / "fixture-routes.db"
        self.log_file = root / "logs" / "hub.log"
        self.usage_file = root / "logs" / "hub-usage.jsonl"
        self.errors_file = root / "logs" / "hub-errors.jsonl"
        self.account_pool_config = root / "account-pools.json"
        self.account_pool_state = root / "account-state.sqlite3"
        self._write_db()
        self._write_config()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "CLAUDE_HUB_CONFIG": str(self.config_file),
                "CLAUDE_HUB_DB": str(self.db_file),
                "CLAUDE_HUB_LOG": str(self.log_file),
                "CLAUDE_HUB_USAGE": str(self.usage_file),
                "CLAUDE_HUB_ERRORS": str(self.errors_file),
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

    def _write_db(self):
        connection = sqlite3.connect(self.db_file)
        try:
            connection.execute(
                "CREATE TABLE providers "
                "(name TEXT, app_type TEXT, settings_config TEXT, meta TEXT)"
            )
            for name, token, host, meta in (
                (
                    "Fixture Route A",
                    "fixture-route-token-a",
                    "https://fixture-a.invalid/v1",
                    {},
                ),
                (
                    "Fixture Route B",
                    "fixture-route-token-b",
                    "https://fixture-b.invalid/v1",
                    {},
                ),
                (
                    "Fixture Route C",
                    "fixture-route-token-c",
                    "https://fixture-c.invalid/v1",
                    {"apiFormat": "openai_chat"},
                ),
                (
                    "Fixture Route D",
                    "fixture-route-token-d",
                    "https://fixture-d.invalid/v1/messages",
                    {"isFullUrl": True},
                ),
            ):
                connection.execute(
                    "INSERT INTO providers VALUES (?, 'claude', ?, ?)",
                    (
                        name,
                        json.dumps(
                            {
                                "env": {
                                    "ANTHROPIC_BASE_URL": host,
                                    "ANTHROPIC_AUTH_TOKEN": token,
                                }
                            }
                        ),
                        json.dumps(meta),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        self.db_file.chmod(0o600)

    def _write_config(self, **updates):
        config = {
            "port": 18787,
            "default_channel": "alpha",
            "channels": {
                "alpha": {
                    "provider": "Fixture Route A",
                    "models": ["fixture-model-a"],
                },
                "beta": {
                    "provider": "Fixture Route B",
                    "models": ["fixture-model-b"],
                },
                "gamma": {
                    "provider": "Fixture Route C",
                    "models": ["fixture-model-c"],
                },
                "delta": {
                    "provider": "Fixture Route D",
                    "models": ["fixture-model-d"],
                },
            },
        }
        config.update(updates)
        self.config_file.write_text(json.dumps(config), encoding="utf-8")
        self.config_file.chmod(0o600)
        hub.reset_caches()

    def _route_config(self, routes, **updates):
        updates["routes"] = routes
        self._write_config(**updates)

    def _fixture_route(self):
        return {
            "fixture-route": [
                {"channel": "alpha", "model": "fixture-model-a"},
                {"channel": "beta", "model": "fixture-model-b"},
            ]
        }

    def _request(self, body, *, session, path="/v1/messages"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()

        class FakeRequest:
            method = "POST"
            path_qs = path

            def __init__(self):
                self.path = path
                self.query_string = ""
                self.headers = CIMultiDict(
                    {"authorization": "Bearer fixture-local-token"}
                )
                self.app = {"session": session}
                self.transport = _FakeTransport()

            async def read(self):
                return body

        return FakeRequest()

    def _run(self, body, session, path="/v1/messages"):
        request = self._request(body, session=session, path=path)
        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            return asyncio.run(hub.handle_messages(request))

    # ------------------------------------------------------------ validation

    def test_routes_roundtrip_through_validated_config(self):
        self._route_config(self._fixture_route())

        cfg = hub.get_config()

        self.assertEqual(
            cfg["routes"],
            {
                "fixture-route": {
                    "targets": [
                        {"channel": "alpha", "model": "fixture-model-a"},
                        {"channel": "beta", "model": "fixture-model-b"},
                    ],
                    "requires": [],
                }
            },
        )

    def test_routes_reject_target_with_unknown_channel(self):
        self._route_config(
            {"fixture-route": [{"channel": "missing", "model": "fixture-model-a"}]}
        )

        with self.assertRaisesRegex(
            hub.ConfigError,
            r"routes\.fixture-route\[0\]\.channel references unknown channel "
            "'missing'",
        ):
            hub.get_config()

    def test_routes_reject_target_with_undeclared_model(self):
        self._route_config(
            {"fixture-route": [{"channel": "alpha", "model": "fixture-other-model"}]}
        )

        with self.assertRaisesRegex(
            hub.ConfigError,
            r"routes\.fixture-route\[0\]\.model 'fixture-other-model' is not "
            r"declared in channels\.alpha\.models",
        ):
            hub.get_config()

    def test_routes_reject_malformed_entries(self):
        cases = [
            ("not-an-object", "routes must be an object"),
            ({"fixture-route": []}, "non-empty list of targets"),
            (
                {"fixture-route": ["fixture-model-a"]},
                r"routes\.fixture-route\[0\] must be an object",
            ),
            (
                {"fixture-route": [{"channel": "alpha"}]},
                r"routes\.fixture-route\[0\]\.model must be a non-empty string",
            ),
            (
                {"Fixture Route": [{"channel": "alpha", "model": "fixture-model-a"}]},
                "must be lowercase",
            ),
        ]
        for routes, pattern in cases:
            with self.subTest(routes=routes):
                self._route_config(routes)
                with self.assertRaisesRegex(hub.ConfigError, pattern):
                    hub.get_config()

    def test_routes_reject_unknown_required_capability(self):
        self._route_config(
            {
                "fixture-route": {
                    "targets": [{"channel": "alpha", "model": "fixture-model-a"}],
                    "requires": ["fixture-capability"],
                }
            }
        )

        with self.assertRaisesRegex(
            hub.ConfigError, "unknown capability 'fixture-capability'"
        ):
            hub.get_config()

    def test_routes_reject_capability_the_target_adapter_rejects(self):
        # server_tool is reject for the openai_chat adapter; the channel
        # override pins the declared format used by startup validation.
        self._route_config(
            {
                "fixture-route": {
                    "targets": [{"channel": "beta", "model": "fixture-model-b"}],
                    "requires": ["server_tool"],
                }
            },
            channels={
                "alpha": {
                    "provider": "Fixture Route A",
                    "models": ["fixture-model-a"],
                },
                "beta": {
                    "provider": "Fixture Route B",
                    "models": ["fixture-model-b"],
                    "api_format": "openai_chat",
                },
            },
        )

        with self.assertRaisesRegex(
            hub.ConfigError,
            r"routes\.fixture-route\[0\] cannot satisfy required capability "
            r"'server_tool'",
        ):
            hub.get_config()

    def test_requires_resolve_format_from_provider_meta_without_override(self):
        # Channel gamma declares no api_format override; the effective format
        # comes from provider DB meta (openai_chat), which rejects server_tool.
        # Startup validation must resolve it exactly like runtime dispatch.
        self._route_config(
            {
                "fixture-route": {
                    "targets": [{"channel": "gamma", "model": "fixture-model-c"}],
                    "requires": ["server_tool"],
                }
            }
        )

        with self.assertRaisesRegex(
            hub.ConfigError,
            r"routes\.fixture-route\[0\] cannot satisfy required capability "
            r"'server_tool': the openai_chat adapter rejects it",
        ):
            hub.get_config()

    def test_routes_accept_capability_the_target_adapter_carries(self):
        self._route_config(
            {
                "fixture-route": {
                    "targets": [{"channel": "alpha", "model": "fixture-model-a"}],
                    "requires": ["image", "client_tool"],
                }
            }
        )

        cfg = hub.get_config()

        self.assertEqual(
            cfg["routes"]["fixture-route"]["requires"], ["image", "client_tool"]
        )

    def test_config_without_routes_behaves_as_before(self):
        cfg = hub.get_config()
        self.assertEqual(cfg["routes"], {})

        session = _SequencedFakeSession(
            [_json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}})]
        )
        response = self._run(
            {
                "model": "alpha,fixture-model-a",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["x-hub-channel"], "alpha")
        self.assertNotIn("x-hub-route", response.headers)

    # ------------------------------------------------------------- dispatch

    def test_unknown_route_name_is_a_client_error(self):
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession([])

        response = self._run(
            {"model": "route:fixture-missing", "messages": []}, session
        )

        self.assertEqual(response.status, 400)
        self.assertEqual(len(session.calls), 0)

    def test_safe_rejection_moves_to_the_next_target_in_order(self):
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [
                _json_upstream(401, {"error": {"message": "fixture denied a"}}),
                _json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
            ]
        )

        response = self._run(
            {
                "model": "route:fixture-route",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://fixture-a.invalid/v1/messages",
                "https://fixture-b.invalid/v1/messages",
            ],
        )
        # Each target forwards its own declared model ID, never the route name.
        forwarded_models = [
            json.loads(call[1]["data"].decode())["model"] for call in session.calls
        ]
        self.assertEqual(forwarded_models, ["fixture-model-a", "fixture-model-b"])
        self.assertEqual(response.headers["x-hub-channel"], "beta")
        self.assertEqual(response.headers["x-hub-model"], "fixture-model-b")
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")

    def _write_pool_db(self):
        """Two pool members on one endpoint plus a fallback provider."""
        self.db_file.unlink(missing_ok=True)
        connection = sqlite3.connect(self.db_file)
        try:
            connection.execute(
                "CREATE TABLE providers ("
                "id TEXT, name TEXT, app_type TEXT, settings_config TEXT, meta TEXT)"
            )
            for provider_id, token, base_url in (
                ("primary", "fixture-pool-token-a", "https://fixture-pool-a.invalid/v1"),
                ("secondary", "fixture-pool-token-b", "https://fixture-pool-a.invalid/v1"),
                ("fallback", "fixture-pool-token-c", "https://fixture-pool-b.invalid/v1"),
            ):
                connection.execute(
                    "INSERT INTO providers VALUES (?, ?, 'claude', ?, '{}')",
                    (
                        provider_id,
                        f"Fixture Pool {provider_id}",
                        json.dumps(
                            {
                                "env": {
                                    "ANTHROPIC_BASE_URL": base_url,
                                    "ANTHROPIC_AUTH_TOKEN": token,
                                }
                            }
                        ),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        self.db_file.chmod(0o600)

    def _write_pool_config(self, enabled=True):
        self.account_pool_config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "id:primary": {
                            "strategy": "round_robin",
                            "cooldown_seconds": 60,
                            "max_cooldown_seconds": 3600,
                            "members": [
                                {
                                    "provider": "id:primary",
                                    "weight": 1,
                                    "priority": 0,
                                    "enabled": enabled,
                                },
                                {
                                    "provider": "id:secondary",
                                    "weight": 1,
                                    "priority": 0,
                                    "enabled": enabled,
                                },
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.account_pool_config.chmod(0o600)

    def _pool_route_config(self, targets):
        self._route_config(
            {"fixture-pool-route": targets},
            channels={
                "alpha": {"provider": "id:primary", "models": ["pool-model-a"]},
                "beta": {"provider": "id:fallback", "models": ["pool-model-b"]},
            },
        )

    def _seed_pool_cooldown(self, seconds):
        """Pre-seed shared pool state with both members in cooldown."""
        connection = sqlite3.connect(self.account_pool_state)
        try:
            # Mirror the current v2 scheduler schema, stamped with its
            # version so the pool accepts the pre-seeded file.
            connection.execute("PRAGMA user_version=2")
            connection.execute(
                "CREATE TABLE pool_cursor ("
                "pool TEXT NOT NULL, priority INTEGER NOT NULL, "
                "config_hash TEXT NOT NULL, cursor INTEGER NOT NULL, "
                "PRIMARY KEY (pool, priority))"
            )
            connection.execute(
                "CREATE TABLE member_state ("
                "pool TEXT NOT NULL, member TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "disabled INTEGER NOT NULL DEFAULT 0, "
                "cooldown_until REAL NOT NULL DEFAULT 0, "
                "last_status INTEGER, "
                "PRIMARY KEY (pool, member, fingerprint))"
            )
            cooldown_until = time.time() + seconds
            for member, token in (
                ("id:primary", "fixture-pool-token-a"),
                ("id:secondary", "fixture-pool-token-b"),
            ):
                connection.execute(
                    "INSERT INTO member_state VALUES (?, ?, ?, 0, ?, NULL)",
                    (
                        "id:primary",
                        member,
                        hub.credential_fingerprint(token),
                        cooldown_until,
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        self.account_pool_state.chmod(0o600)

    def test_target_account_pool_is_exhausted_before_the_next_target(self):
        # Target alpha owns a two-account pool; both accounts reject with 401
        # before the route moves on to target beta.
        self._write_pool_db()
        self._write_pool_config()
        self._pool_route_config(
            [
                {"channel": "alpha", "model": "pool-model-a"},
                {"channel": "beta", "model": "pool-model-b"},
            ]
        )
        session = _SequencedFakeSession(
            [
                _json_upstream(401, {"error": {"message": "fixture denied a"}}),
                _json_upstream(401, {"error": {"message": "fixture denied b"}}),
                _json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
            ]
        )

        response = self._run(
            {"model": "route:fixture-pool-route", "messages": []}, session
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [call[1]["headers"]["x-api-key"] for call in session.calls],
            [
                "fixture-pool-token-a",
                "fixture-pool-token-b",
                "fixture-pool-token-c",
            ],
        )
        self.assertEqual(response.headers["x-hub-channel"], "beta")
        self.assertEqual(response.headers["x-hub-route"], "fixture-pool-route")

    def test_429_exhaustion_preserves_retry_after_from_the_last_target(self):
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [
                _json_upstream(
                    429, {"error": {"message": "fixture limited a"}}, {"Retry-After": "30"}
                ),
                _json_upstream(
                    429, {"error": {"message": "fixture limited b"}}, {"Retry-After": "45"}
                ),
            ]
        )

        response = self._run(
            {"model": "route:fixture-route", "messages": []}, session
        )

        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers["retry-after"], "45")
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")
        self.assertEqual(len(session.calls), 2)

    def test_exhaustion_names_the_last_upstream_reason_without_leaking_secrets(self):
        # The whole point of the evidence plumbing: a route that burns through
        # every target must still tell the client what the upstream said, so a
        # quota message does not die in the hub log.
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [
                _json_upstream(429, {"error": {"message": "fixture limited a"}}),
                _json_upstream(
                    429,
                    {
                        "error": {
                            "code": "1302",
                            "message": (
                                "5 小时限额已用完 token=fixture-secret "
                                "https://vendor.invalid/quota"
                            ),
                        }
                    },
                ),
            ]
        )

        response = self._run(
            {"model": "route:fixture-route", "messages": []}, session
        )

        self.assertEqual(response.status, 429)
        payload = json.loads(response.text)
        message = payload["error"]["message"]
        self.assertIn("exhausted all 2 targets", message)
        self.assertIn("last 'beta' upstream 429", message)
        self.assertIn("(1302)", message)
        self.assertIn("5 小时限额已用完", message)
        self.assertNotIn("fixture-secret", message)
        self.assertNotIn("vendor.invalid", message)
        self.assertNotIn("fixture limited a", message)

        row = json.loads(self.errors_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["phase"], "route")
        self.assertEqual(row["channel"], "beta")
        self.assertEqual((row["status"], row["code"]), (429, "1302"))
        self.assertEqual(row["route"], "fixture-route")
        self.assertNotIn("fixture-secret", row["message"])

    def test_pool_exhaustion_reason_survives_into_the_route_error(self):
        # Local cooldown is also a real reason; it must not collapse to a bare
        # 429 with no explanation.
        self._write_pool_db()
        self._write_pool_config()
        self._pool_route_config([{"channel": "alpha", "model": "pool-model-a"}])
        self._seed_pool_cooldown(30)
        session = _SequencedFakeSession([])

        response = self._run(
            {"model": "route:fixture-pool-route", "messages": []}, session
        )

        self.assertEqual(response.status, 429)
        message = json.loads(response.text)["error"]["message"]
        self.assertIn("last 'alpha' upstream 429", message)
        self.assertIn("all provider accounts are unavailable", message)
        self.assertEqual(len(session.calls), 0)

    def test_5xx_does_not_move_to_the_next_target(self):
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [_json_upstream(500, {"error": {"message": "fixture boom"}})]
        )

        response = self._run(
            {"model": "route:fixture-route", "messages": []}, session
        )

        self.assertEqual(response.status, 500)
        self.assertEqual(len(session.calls), 1)

    def test_started_response_does_not_move_to_the_next_target(self):
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [
                _FakeUpstream(
                    200,
                    {"Content-Type": "text/event-stream"},
                    [b'event: message_start\ndata: {"type":"message_start"}\n\n'],
                    fail_after=True,
                )
            ]
        )
        request = self._request(
            {"model": "route:fixture-route", "stream": True, "messages": []},
            session=session,
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        self.assertEqual(len(session.calls), 1)

    def test_transformed_target_failover_keeps_per_target_model_ids(self):
        # Target gamma translates to openai_chat; its 401 is a safe
        # pre-commit rejection, so the route continues to anthropic alpha.
        self._route_config(
            {
                "fixture-route": [
                    {"channel": "gamma", "model": "fixture-model-c"},
                    {"channel": "alpha", "model": "fixture-model-a"},
                ]
            }
        )
        session = _SequencedFakeSession(
            [
                _json_upstream(401, {"error": {"message": "fixture denied c"}}),
                _json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
            ]
        )

        response = self._run(
            {
                "model": "route:fixture-route",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(session.calls), 2)
        forwarded_models = [
            json.loads(call[1]["data"].decode())["model"] for call in session.calls
        ]
        self.assertEqual(forwarded_models, ["fixture-model-c", "fixture-model-a"])
        self.assertEqual(response.headers["x-hub-channel"], "alpha")
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")

    def test_403_moves_to_the_next_target(self):
        # Transport is pinned to direct so the fixture owns exactly one
        # candidate; once every transport of the target rejects with 403 the
        # route moves on (design doc section 4).
        self._route_config(self._fixture_route(), transport={"mode": "direct"})
        session = _SequencedFakeSession(
            [
                _json_upstream(403, {"error": {"message": "fixture forbidden a"}}),
                _json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
            ]
        )

        response = self._run(
            {"model": "route:fixture-route", "messages": []}, session
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://fixture-a.invalid/v1/messages",
                "https://fixture-b.invalid/v1/messages",
            ],
        )
        self.assertEqual(response.headers["x-hub-channel"], "beta")
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")

    def test_last_error_wins_drops_an_earlier_retry_after(self):
        # Target alpha rejects 429 with Retry-After, target beta rejects 401;
        # the final response reflects only the last target's rejection.
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [
                _json_upstream(
                    429, {"error": {"message": "fixture limited a"}}, {"Retry-After": "30"}
                ),
                _json_upstream(401, {"error": {"message": "fixture denied b"}}),
            ]
        )

        response = self._run(
            {"model": "route:fixture-route", "messages": []}, session
        )

        self.assertEqual(response.status, 401)
        self.assertNotIn("retry-after", response.headers)
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")
        self.assertEqual(len(session.calls), 2)

    def test_route_prefix_without_routes_falls_back_to_legacy_routing(self):
        # With no routes configured the route: prefix carries no routing
        # meaning; a route_unknown_to_default channel forwards the model
        # name unchanged, exactly as before route groups existed.
        self._write_config(
            channels={
                "alpha": {
                    "provider": "Fixture Route A",
                    "models": ["fixture-model-a"],
                    "route_unknown_to_default": True,
                }
            }
        )
        session = _SequencedFakeSession(
            [_json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}})]
        )

        response = self._run(
            {
                "model": "route:fixture-unlisted",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            json.loads(session.calls[0][1]["data"].decode())["model"],
            "route:fixture-unlisted",
        )
        self.assertEqual(response.headers["x-hub-channel"], "alpha")
        self.assertNotIn("x-hub-route", response.headers)

    def test_pool_disabled_before_any_upstream_moves_to_the_next_target(self):
        # Every pool member of target alpha is disabled in the pool config,
        # so the initial acquire fails before a single byte goes upstream
        # and the route lands directly on target beta.
        self._write_pool_db()
        self._write_pool_config(enabled=False)
        self._pool_route_config(
            [
                {"channel": "alpha", "model": "pool-model-a"},
                {"channel": "beta", "model": "pool-model-b"},
            ]
        )
        session = _SequencedFakeSession(
            [_json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}})]
        )

        response = self._run(
            {"model": "route:fixture-pool-route", "messages": []}, session
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [call[0] for call in session.calls],
            ["https://fixture-pool-b.invalid/v1/messages"],
        )
        self.assertEqual(response.headers["x-hub-channel"], "beta")
        self.assertEqual(response.headers["x-hub-route"], "fixture-pool-route")

    def test_pool_cooldown_maps_to_429_and_preserves_retry_after(self):
        # Every pool member is cooling down from earlier requests, so the
        # initial acquire raises PoolExhausted(cooldown); with no other
        # target the route surfaces 429 with the remaining cooldown.
        self._write_pool_db()
        self._write_pool_config()
        self._seed_pool_cooldown(300)
        self._pool_route_config([{"channel": "alpha", "model": "pool-model-a"}])
        session = _SequencedFakeSession([])

        response = self._run(
            {"model": "route:fixture-pool-route", "messages": []}, session
        )

        self.assertEqual(response.status, 429)
        retry_after = int(response.headers["retry-after"])
        self.assertTrue(1 <= retry_after <= 300)
        self.assertEqual(response.headers["x-hub-route"], "fixture-pool-route")
        self.assertEqual(len(session.calls), 0)

    def test_count_tokens_route_estimates_when_upstream_lacks_the_endpoint(self):
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [_json_upstream(404, {"error": {"message": "fixture no endpoint"}})]
        )

        response = self._run(
            {
                "model": "route:fixture-route",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session,
            path="/v1/messages/count_tokens",
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["x-hub-token-count-source"], "estimate")
        self.assertEqual(response.headers["x-hub-channel"], "alpha")
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")
        self.assertEqual(
            [call[0] for call in session.calls],
            ["https://fixture-a.invalid/v1/messages/count_tokens"],
        )

    def test_count_tokens_route_failover_keeps_target_order(self):
        self._route_config(self._fixture_route())
        session = _SequencedFakeSession(
            [
                _json_upstream(401, {"error": {"message": "fixture denied a"}}),
                _json_upstream(200, {"input_tokens": 42}),
            ]
        )

        response = self._run(
            {
                "model": "route:fixture-route",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session,
            path="/v1/messages/count_tokens",
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://fixture-a.invalid/v1/messages/count_tokens",
                "https://fixture-b.invalid/v1/messages/count_tokens",
            ],
        )
        self.assertEqual(response.headers["x-hub-token-count-source"], "upstream")
        self.assertEqual(response.headers["x-hub-channel"], "beta")
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")

    def test_full_url_channel_participates_in_route_failover(self):
        # Target delta is a full-url channel; its endpoint is used unchanged
        # and its 401 is still a safe pre-commit rejection.
        self._route_config(
            {
                "fixture-route": [
                    {"channel": "delta", "model": "fixture-model-d"},
                    {"channel": "alpha", "model": "fixture-model-a"},
                ]
            }
        )
        session = _SequencedFakeSession(
            [
                _json_upstream(401, {"error": {"message": "fixture denied d"}}),
                _json_upstream(200, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
            ]
        )

        response = self._run(
            {
                "model": "route:fixture-route",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session,
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [call[0] for call in session.calls],
            [
                "https://fixture-d.invalid/v1/messages",
                "https://fixture-a.invalid/v1/messages",
            ],
        )
        forwarded_models = [
            json.loads(call[1]["data"].decode())["model"] for call in session.calls
        ]
        self.assertEqual(forwarded_models, ["fixture-model-d", "fixture-model-a"])
        self.assertEqual(response.headers["x-hub-channel"], "alpha")
        self.assertEqual(response.headers["x-hub-route"], "fixture-route")


if __name__ == "__main__":
    unittest.main()
