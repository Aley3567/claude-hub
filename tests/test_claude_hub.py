import asyncio
import codecs
import copy
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
import threading
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest import mock

import aiohttp
from multidict import CIMultiDict

import claude1_usage_report as usage_report


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
        self.errors_file = root / "logs" / "hub-errors.jsonl"
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

    def test_usage_log_records_degrade_codes(self):
        hub.record_usage(
            "fast",
            "fixture-model",
            "openai_chat",
            {"input_tokens": 11, "output_tokens": 3},
            degrade_codes=("HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",),
        )

        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED"],
        )

    def test_usage_log_omits_degrade_field_for_clean_turns(self):
        hub.record_usage(
            "fast",
            "fixture-model",
            "openai_chat",
            {"input_tokens": 11, "output_tokens": 3},
        )

        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertNotIn("deg", row)

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

    def test_error_journal_keeps_only_sanitized_fields_and_stays_private(self):
        hub.record_error(
            phase="response",
            channel="fast",
            model="fixture-model",
            api_format="openai_chat",
            status=429,
            code="1302",
            message="5 小时限额已用完",
            route="fixture-route",
        )

        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["phase"], "response")
        self.assertEqual(row["channel"], "fast")
        self.assertEqual(row["model"], "fixture-model")
        self.assertEqual(row["format"], "openai_chat")
        self.assertEqual((row["status"], row["code"]), (429, "1302"))
        self.assertEqual(row["message"], "5 小时限额已用完")
        self.assertEqual(row["route"], "fixture-route")
        self.assertIsInstance(row["ts"], int)
        # Absent fields are omitted rather than written as null, and request or
        # response payloads never reach the journal at all.
        self.assertNotIn("exc", row)
        self.assertNotIn("payload", row)
        self.assertNotIn("upstream_body", row)
        if os.name == "posix":
            self.assertEqual(self.errors_file.stat().st_mode & 0o777, 0o600)

    def test_error_journal_records_unique_degrade_codes(self):
        hub.record_error(
            phase="response",
            channel="fast",
            model="fixture-model",
            api_format="openai_chat",
            status=500,
            code="upstream_error",
            message="upstream failed",
            degrade_codes=(
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED",
            ),
        )

        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(
            row["deg"],
            [
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED",
            ],
        )

    def test_error_journal_omits_degrade_codes_for_clean_errors(self):
        hub.record_error(
            phase="response",
            channel="fast",
            model="fixture-model",
            api_format="openai_chat",
            status=500,
        )

        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertNotIn("deg", row)

    def test_error_journal_rotates_after_crossing_its_size_limit(self):
        with mock.patch.object(hub, "ERRORS_LOG_MAX_BYTES", 1):
            hub.record_error(phase="stream", channel="fast", exc_type="IncompleteSSE")

        rotated = self.errors_file.with_name(self.errors_file.name + ".1")
        row = json.loads(rotated.read_text(encoding="utf-8"))
        self.assertEqual((row["phase"], row["exc"]), ("stream", "IncompleteSSE"))
        self.assertEqual(self.errors_file.read_text(encoding="utf-8"), "")
        if os.name == "posix":
            self.assertEqual(self.errors_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(rotated.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW unavailable")
    def test_error_journal_does_not_follow_symlinks(self):
        self.errors_file.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / "must-not-be-written-by-errors"
        target.write_text("unchanged", encoding="utf-8")
        self.errors_file.symlink_to(target)

        hub.record_error(phase="route", status=503)

        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
        self.assertIsNone(hub._errors_fp)

    def test_error_journal_never_breaks_the_forwarding_path(self):
        # A journal that cannot be written must not turn into a request-path
        # failure: the record is dropped, not raised.
        self.errors_file.parent.mkdir(parents=True, exist_ok=True)
        self.errors_file.mkdir()

        hub.record_error(phase="response", status=500)

        self.assertIsNone(hub._errors_fp)

    def test_errors_path_is_a_sibling_of_the_hub_log_it_belongs_to(self):
        # Named hubs each get their own journal without the launcher passing a
        # second environment variable.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_HUB_ERRORS", None)
            os.environ["CLAUDE_HUB_LOG"] = str(self.root / "logs" / "hubs" / "kimi.log")
            self.assertEqual(
                hub.errors_path(),
                self.root / "logs" / "hubs" / "kimi-errors.jsonl",
            )

    def test_cli_errors_renders_reasons_and_skips_stale_debug_rows(self):
        self.errors_file.parent.mkdir(parents=True, exist_ok=True)
        self.errors_file.write_text(
            "\n".join(
                [
                    # Pre-journal debug shape: no phase, and it carried payloads.
                    json.dumps(
                        {
                            "ts": "2026-08-15T19:58:13",
                            "channel": "legacy",
                            "payload": {"messages": [{"role": "user"}]},
                        }
                    ),
                    "not-json",
                    json.dumps(
                        {
                            "ts": 1755000000,
                            "phase": "response",
                            "channel": "fast",
                            "model": "fixture-model",
                            "format": "openai_chat",
                            "status": 429,
                            "code": "1302",
                            "message": "5 小时限额已用完",
                            "route": "fixture-route",
                        }
                    ),
                    json.dumps(
                        {"ts": 1755000100, "phase": "stream", "exc": "IncompleteSSE"}
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.errors_file.chmod(0o600)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hub.cli_errors([])
        rendered = buffer.getvalue()

        self.assertIn("5 小时限额已用完", rendered)
        self.assertIn("fast/fixture-model (openai_chat)", rendered)
        self.assertIn("429", rendered)
        self.assertIn("[route fixture-route]", rendered)
        self.assertIn("IncompleteSSE", rendered)
        self.assertNotIn("legacy", rendered)
        self.assertNotIn("messages", rendered)
        self.assertIn("2 stale record(s) skipped", rendered)

        tail = io.StringIO()
        with redirect_stdout(tail):
            hub.cli_errors(["-n", "1"])
        self.assertIn("IncompleteSSE", tail.getvalue())
        self.assertNotIn("5 小时限额已用完", tail.getvalue())

    def test_error_journal_reads_past_non_utf8_bytes(self):
        """R6: one torn byte used to truncate the journal without a word.

        The reader decoded strictly, and the UnicodeDecodeError raised from
        the line iterator was swallowed by the outer handler — so every row
        from the failing 8KB decode block onward vanished while `skipped`
        still reported 0. A small journal full of real errors rendered as
        "no error records yet". The writer uses ensure_ascii=False, so a
        Chinese reason is stored as bare multi-byte UTF-8 and a torn write
        (ENOSPC, SIGKILL, two hubs sharing one path) lands exactly here.
        """

        def row_bytes(index):
            return json.dumps(
                {
                    "ts": 1755000000 + index,
                    "phase": "response",
                    "channel": f"c{index}",
                }
            ).encode("utf-8")

        # A lone 0xE9: the lead byte of a three-byte CJK sequence, cut short.
        damaged = (
            b'{"ts":9,"phase":"response","channel":"c-bad","message":"\xe9"}'
        )

        for label, count, bad_at in (("small", 3, 1), ("multi-block", 202, 100)):
            with self.subTest(journal=label):
                lines = [
                    damaged if index == bad_at else row_bytes(index)
                    for index in range(count)
                ]
                self.errors_file.parent.mkdir(parents=True, exist_ok=True)
                self.errors_file.write_bytes(b"\n".join(lines) + b"\n")
                if label == "multi-block":
                    # Larger than one TextIOWrapper decode block, which is
                    # what made the old failure lose 62 rows instead of all.
                    self.assertGreater(self.errors_file.stat().st_size, 8192)

                rows, skipped = hub._load_error_rows(self.errors_file, 10_000)

                # The bad byte becomes U+FFFD, which leaves the row valid
                # JSON: lossy but usable, which is the repo's default gear.
                self.assertEqual(len(rows), count)
                self.assertEqual(skipped, 0)
                channels = [row["channel"] for row in rows]
                self.assertEqual(channels[0], "c0")
                self.assertEqual(channels[-1], f"c{count - 1}")
                self.assertEqual(channels[bad_at], "c-bad")
                self.assertIn("�", rows[bad_at]["message"])

    def test_error_journal_counts_a_torn_row_as_skipped(self):
        """A write cut mid-row stays unparseable after byte replacement.

        That is what `skipped` exists for, and it has to stay visible in the
        CLI rather than quietly shrinking the report.
        """
        intact = json.dumps(
            {"ts": 1755000000, "phase": "response", "channel": "before"}
        ).encode("utf-8")
        after = json.dumps(
            {"ts": 1755000200, "phase": "stream", "exc": "IncompleteSSE"}
        ).encode("utf-8")
        torn = b'{"ts":1755000100,"phase":"resp\xe9onse","channel"'

        self.errors_file.parent.mkdir(parents=True, exist_ok=True)
        self.errors_file.write_bytes(b"\n".join([intact, torn, after]) + b"\n")
        self.errors_file.chmod(0o600)

        rows, skipped = hub._load_error_rows(self.errors_file, 10_000)
        self.assertEqual([row.get("channel") for row in rows], ["before", None])
        self.assertEqual(skipped, 1)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hub.cli_errors([])
        rendered = buffer.getvalue()
        self.assertIn("IncompleteSSE", rendered)
        self.assertIn("1 stale record(s) skipped", rendered)

        with self.assertRaises(SystemExit):
            hub.cli_errors(["-n", "zero"])

    def test_cli_errors_renders_degrade_codes_and_ignores_malformed_values(self):
        self.errors_file.parent.mkdir(parents=True, exist_ok=True)
        self.errors_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "ts": 1755000200,
                            "phase": "response",
                            "channel": "fast",
                            "model": "fixture-model",
                            "deg": [
                                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                                "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED",
                            ],
                        }
                    ),
                    json.dumps(
                        {"ts": 1755000201, "phase": "stream", "exc": "IncompleteSSE"}
                    ),
                    json.dumps(
                        {
                            "ts": 1755000202,
                            "phase": "response",
                            "deg": {"bad": "HUB_DEGRADE_NOT_A_LIST"},
                        }
                    ),
                    json.dumps(
                        {
                            "ts": 1755000203,
                            "phase": "response",
                            "deg": [None, 7, "", "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"],
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.errors_file.chmod(0o600)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hub.cli_errors([])
        rendered = buffer.getvalue()

        self.assertIn(
            "deg=HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED,HUB_DEGRADE_SYSTEM_ROLE_PROMOTED",
            rendered,
        )
        self.assertEqual(
            rendered.count("HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED"),
            1,
        )
        self.assertEqual(
            rendered.count("HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"),
            2,
        )
        self.assertNotIn("HUB_DEGRADE_NOT_A_LIST", rendered)
        self.assertIn("IncompleteSSE", rendered)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_cli_errors_warns_while_the_journal_is_still_world_readable(self):
        self.errors_file.parent.mkdir(parents=True, exist_ok=True)
        self.errors_file.write_text("", encoding="utf-8")
        self.errors_file.chmod(0o644)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hub.cli_errors([])

        self.assertIn("group/other-readable", buffer.getvalue())

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
        # 下游 payload 仍按 schema 发 0 占位;记账视图直接来自 receipt,
        # 看不到这些占位,也不再需要靠 plan 反查剥零。
        self.assertEqual(
            prepared.payload["usage"],
            {"input_tokens": 0, "output_tokens": 0},
        )
        self.assertEqual(prepared.usage_for_accounting(), {})
        self.assertEqual(prepared.receipt.source, "unavailable")

        partial = hub.prepare_response(
            {
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 9},
            },
            "openai_responses",
        )
        self.assertEqual(partial.payload["usage"]["output_tokens"], 0)
        self.assertEqual(partial.usage_for_accounting(), {"input_tokens": 9})
        self.assertEqual(partial.receipt.source, "upstream")

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

    def _configure_provider_and_channel(
        self, alias, model, endpoint, api_format
    ):
        """Set a provider's endpoint/api_format and a channel's model list."""
        connection = sqlite3.connect(self.db_file)
        try:
            try:
                connection.execute("ALTER TABLE providers ADD COLUMN meta TEXT")
            except sqlite3.OperationalError:
                pass
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
                    "Fixture HTTPS",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        raw = json.loads(self.config_file.read_text(encoding="utf-8"))
        raw["channels"][alias]["models"] = [model]
        self.config_file.write_text(json.dumps(raw), encoding="utf-8")
        hub.reset_caches()

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

    def test_hub_v2_bare_slot_uses_the_configured_slot_mapping(self):
        cfg = hub.get_config()
        cfg["model_slots"] = {
            "fable": "fast,claude-opus-4",
            "opus": "local,local-model",
            "sonnet": "fast,remote-model",
            "haiku": "local,remote-model",
        }

        self.assertEqual(
            hub.route("haiku", cfg),
            ("local", "remote-model"),
        )

    def test_official_style_model_id_uses_the_matching_hub_slot(self):
        cfg = hub.get_config()
        cfg["model_slots"] = {
            "fable": "fast,claude-opus-4",
            "opus": "local,local-model",
            "sonnet": "fast,remote-model",
            "haiku": "local,remote-model",
        }

        self.assertEqual(
            hub.route("claude-haiku-4-5-20251001", cfg),
            ("local", "remote-model"),
        )
        self.assertEqual(
            hub.route("claude-sonnet-5", cfg),
            ("fast", "remote-model"),
        )

    def test_unknown_model_error_names_the_available_slots(self):
        cfg = hub.get_config()
        cfg["model_slots"] = {
            "fable": "fast,claude-opus-4",
            "opus": "local,local-model",
            "sonnet": "fast,remote-model",
            "haiku": "local,remote-model",
        }

        with self.assertRaisesRegex(
            hub.RouteError,
            "available model slot: fable, opus, sonnet, haiku",
        ):
            hub.route("made-up-model", cfg)

    def test_official_style_id_does_not_alias_without_hub_slots(self):
        cfg = hub.get_config()
        providers = {
            "Fixture HTTPS": {
                "model_map": {"fable": "upstream-fable"},
            }
        }

        with self.assertRaisesRegex(hub.RouteError, "unknown model"):
            hub.route("claude-fable-5", cfg, providers)

    def test_bare_model_matches_declared_1m_variant(self):
        cfg = {
            "channels": {
                "fast": {"models": ["claude-fable-5[1M]"]},
                "local": {"models": ["local-model"]},
            },
            "default_channel": "local",
        }

        self.assertEqual(
            hub.route("claude-fable-5", cfg),
            ("fast", "claude-fable-5[1M]"),
        )

    def test_1m_bare_match_works_for_non_official_model_names(self):
        cfg = {
            "channels": {
                "fast": {"models": ["upstream-sonnet[1m]"]},
                "local": {"models": ["local-model"]},
            },
            "default_channel": "local",
        }

        self.assertEqual(
            hub.route("upstream-sonnet", cfg),
            ("fast", "upstream-sonnet[1m]"),
        )

    def test_ambiguous_bare_1m_variant_requires_a_channel(self):
        cfg = {
            "channels": {
                "fast": {"models": ["claude-fable-5[1M]"]},
                "local": {"models": ["claude-fable-5[1m]"]},
            },
            "default_channel": "local",
        }

        with self.assertRaisesRegex(hub.RouteError, "ambiguous model"):
            hub.route("claude-fable-5", cfg)

    def test_1m_variant_beats_official_slot_fallback(self):
        # An official-style bare name must still resolve back to the declared
        # 1M model before the generic official-slot fallback routes it to a
        # non-1M mapping; otherwise the context is silently dropped.
        cfg = {
            "channels": {
                "fast": {"models": ["claude-fable-5[1M]"]},
                "local": {"models": ["local-model"]},
            },
            "model_slots": {"fable": "local,local-model"},
            "default_channel": "local",
        }

        self.assertEqual(
            hub.route("claude-fable-5", cfg),
            ("fast", "claude-fable-5[1M]"),
        )

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

    def test_route_unknown_to_default_remaps_official_claude_id_to_provider_tier(self):
        self._write_config(
            default_channel="direct",
            channels={
                "direct": {
                    "provider": "Fixture HTTPS",
                    "models": ["GLM-5.2", "Deepseek-v4-flash"],
                    "route_unknown_to_default": True,
                }
            },
        )
        hub.reset_caches()
        cfg = hub.get_config()
        providers = {
            "Fixture HTTPS": {
                "model_map": {
                    "fable": "Deepseek-v4-flash",
                    "opus": "GLM-5.2",
                    "sonnet": "GLM-5.2",
                    "haiku": "Deepseek-v4-flash",
                },
            }
        }

        # An official Claude ID is remapped to the provider's tier model
        # instead of being forwarded unchanged (which a name/case-sensitive
        # upstream rejects with 400 "invalid model").
        self.assertEqual(
            hub.route("claude-fable-5", cfg, providers),
            ("direct", "Deepseek-v4-flash"),
        )
        self.assertEqual(
            hub.route("claude-opus-5", cfg, providers),
            ("direct", "GLM-5.2"),
        )
        # A non-official unlisted model still passes through unchanged.
        self.assertEqual(
            hub.route("some-future-model", cfg, providers),
            ("direct", "some-future-model"),
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

    def test_upstream_model_id_strips_suffix_only_for_anthropic(self):
        cases = [
            # (api_format, model, expected)
            ("anthropic", "claude-sonnet-4[1m]", "claude-sonnet-4"),
            ("anthropic", "claude-sonnet-4[1M]", "claude-sonnet-4"),
            ("anthropic", "x[1m]", "x"),
            ("anthropic", "[1m]", "[1m]"),
            ("anthropic", "[1M]", "[1M]"),
            ("anthropic", "[1m]extra", "[1m]extra"),
            ("anthropic", "", ""),
            ("anthropic", "claude-sonnet-4", "claude-sonnet-4"),
            ("openai_chat", "glm-5.3[1M]", "glm-5.3[1M]"),
            ("openai_chat", "glm-5.3[1m]", "glm-5.3[1m]"),
            ("openai_chat", "glm-5.3", "glm-5.3"),
            ("openai_responses", "moonshotai/Kimi-K3[1M]", "moonshotai/Kimi-K3[1M]"),
            ("openai_responses", "moonshotai/Kimi-K3[1m]", "moonshotai/Kimi-K3[1m]"),
        ]
        for api_format, model, expected in cases:
            with self.subTest(api_format=api_format, model=model):
                self.assertEqual(hub.upstream_model_id(model, api_format), expected)

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

    def test_transformed_stream_type_errors_return_a_terminal_sse_error(self):
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
            "name": "Fixture HTTPS",
        }
        with mock.patch.object(hub, "AnthropicStreamBridge", TypeFailingBridge), mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ), mock.patch.object(hub, "log") as write_log:
            response = asyncio.run(
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
        self.assertIs(response, downstream)
        self.assertFalse(request.transport.aborted)
        self.assertTrue(downstream.eof)
        self.assertEqual(len(session.calls), 1)
        rendered = b"".join(downstream.writes)
        error_payload = json.loads(
            rendered.rsplit(b"event: error\ndata: ", 1)[1].split(b"\n\n", 1)[0]
        )
        self.assertIn(
            "HUB_STREAM_TRANSLATION_FAILED",
            error_payload["error"]["message"],
        )
        self.assertIn("TypeError", error_payload["error"]["message"])
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

    def test_translated_stream_error_keeps_code_after_long_provider_name(self):
        exc = hub.ProtocolTransformError(
            "upstream usage conflicts with its base counters",
            code="HUB_UPSTREAM_USAGE_INVALID",
            path="$.usage.total_tokens",
        )

        code, message = hub.translated_stream_error_evidence(
            exc,
            provider_name="provider-" + ("x" * 900),
            api_format="openai_chat",
        )

        self.assertEqual(code, "HUB_UPSTREAM_USAGE_INVALID")
        self.assertIn("HUB_UPSTREAM_USAGE_INVALID", message)
        self.assertIn("$.usage.total_tokens", message)
        self.assertLessEqual(len(message), 512)

    def test_transformed_stream_aborts_only_if_terminal_error_cannot_be_written(self):
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [b"data: fixture\n\n"],
        )
        request = self._request(
            {"model": "fast,model", "messages": [], "stream": True},
            session=_FakeSession(upstream),
        )

        class TypeFailingBridge:
            def __init__(self, _api_format):
                pass

            def feed(self, _event, _data):
                raise TypeError("invalid upstream usage")

        class ErrorWriteFailingDownstream(_FakeDownstream):
            async def write(self, chunk):
                if chunk.startswith(b"event: error\n"):
                    raise aiohttp.ClientConnectionError("downstream closed")
                await super().write(chunk)

        downstream = ErrorWriteFailingDownstream(200)
        provider = {
            "api_format": "openai_chat",
            "base_url": "https://upstream.invalid/v1",
            "token": "fixture-upstream-token",
            "name": "Fixture HTTPS",
        }
        with mock.patch.object(
            hub, "AnthropicStreamBridge", TypeFailingBridge
        ), mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ):
            with self.assertRaisesRegex(
                hub.UpstreamStreamAborted,
                "downstream closed while receiving a protocol error",
            ):
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
        self.assertFalse(downstream.eof)

    def test_transformed_protocol_error_is_returned_as_terminal_sse_error(self):
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
                b'"finish_reason":null}]}\n\n',
                b'data: {"choices":[],"usage":{"prompt_tokens":3,'
                b'"completion_tokens":1,"total_tokens":5}}\n\n',
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
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)
        rendered = b"".join(downstream.writes)
        self.assertIn(b'"text":"partial"', rendered)
        self.assertIn(b"event: error\n", rendered)
        error_payload = json.loads(
            rendered.rsplit(b"event: error\ndata: ", 1)[1].split(b"\n\n", 1)[0]
        )
        message = error_payload["error"]["message"]
        self.assertEqual(error_payload["type"], "error")
        self.assertEqual(error_payload["error"]["type"], "api_error")
        self.assertIn("Fixture HTTPS", message)
        self.assertIn("HUB_UPSTREAM_USAGE_INVALID", message)
        self.assertIn("$.usage.total_tokens", message)
        self.assertIn("conflicts with its base usage counters", message)

        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["phase"], "stream")
        self.assertEqual(row["format"], "openai_chat")
        self.assertEqual(row["code"], "HUB_UPSTREAM_USAGE_INVALID")
        self.assertIn("Fixture HTTPS", row["message"])
        self.assertIn("$.usage.total_tokens", row["message"])

    def test_responses_output_error_is_returned_as_terminal_sse_error(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/responses",
            "openai_responses",
        )
        events = [
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {"id": "resp_fixture", "model": "fixture-model"},
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "partial",
                },
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {
                        "type": "message",
                        "id": 7,
                        "role": "assistant",
                        "content": [],
                        "status": "in_progress",
                    },
                },
            ),
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                hub.sse_event(event, payload)
                for event, payload in events
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
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)
        rendered = b"".join(downstream.writes)
        self.assertIn(b'"text":"partial"', rendered)
        error_payload = json.loads(
            rendered.rsplit(b"event: error\ndata: ", 1)[1].split(b"\n\n", 1)[0]
        )
        message = error_payload["error"]["message"]
        self.assertIn("Fixture HTTPS", message)
        self.assertIn("openai_responses", message)
        self.assertIn("HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED", message)
        self.assertIn("$.item.id", message)

        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["format"], "openai_responses")
        self.assertEqual(row["code"], "HUB_UPSTREAM_OUTPUT_BLOCK_UNSUPPORTED")
        self.assertIn("$.item.id", row["message"])

    def test_translated_error_event_is_journaled_as_failure_not_usage(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [b'data: {"error":{"vendor_detail":"opaque"}}\n\n'],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
                "future_request_field": True,
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
        rendered = b"".join(downstream.writes)
        self.assertIn(b"event: error\n", rendered)
        self.assertTrue(downstream.eof)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertIn("HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED", row["deg"])
        self.assertIn("HUB_DEGRADE_UPSTREAM_ERROR_DETAIL_DROPPED", row["deg"])
        self.assertFalse(self.usage_file.exists())

    def test_trailing_done_keeps_the_success_turn_and_its_usage(self):
        """A relay's repeated [DONE] must not turn a good turn into an error.

        Regression for R1: the second [DONE] used to raise HUB_SSE_LATE_EVENT,
        which skipped the accounting exit entirely (usage lost) and appended a
        second terminal after the client had already received message_stop.
        """
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":11,"completion_tokens":3}}\n\n',
                b"data: [DONE]\n\n",
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
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        rendered = b"".join(downstream.writes)
        self.assertIn(b"event: message_stop\n", rendered)
        self.assertNotIn(b"event: error\n", rendered)
        self.assertTrue(downstream.eof)
        self.assertFalse(self.errors_file.exists())
        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(row["in"], 11)
        self.assertIn("HUB_DEGRADE_DUPLICATE_TERMINAL_SKIPPED", row["deg"])

    def test_trailing_done_after_error_frame_keeps_the_upstream_reason(self):
        """The most common relay order must not destroy the real reason.

        Regression for R2: {"error": ...} then [DONE] used to journal
        HUB_SSE_LATE_EVENT instead of the upstream's own quota reason, and sent
        the client two terminals.
        """
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
                b'data: {"error":{"type":"rate_limit_error",'
                b'"message":"quota exhausted for org",'
                b'"code":"insufficient_quota"}}\n\n',
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
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        rendered = b"".join(downstream.writes)
        self.assertEqual(rendered.count(b"event: error\n"), 1)
        self.assertNotIn(b"event: message_stop\n", rendered)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertNotEqual(row.get("code"), "HUB_SSE_LATE_EVENT")
        self.assertIn("quota exhausted for org", row["message"])
        self.assertIn("HUB_DEGRADE_DUPLICATE_TERMINAL_SKIPPED", row["deg"])
        self.assertFalse(self.usage_file.exists())

    def test_translated_error_event_preserves_safe_error_evidence_without_usage(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b'data: {"error":{"code":"quota_exhausted",'
                b'"message":"fixture quota exhausted"}}\n\n'
            ],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
                "future_request_field": True,
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
        rendered = b"".join(downstream.writes)
        self.assertIn(b"event: error\n", rendered)
        self.assertIn(b"quota_exhausted", rendered)
        self.assertIn(b"fixture quota exhausted", rendered)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["code"], "quota_exhausted")
        self.assertEqual(row["message"], "fixture quota exhausted")
        self.assertIn("HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED", row["deg"])
        self.assertFalse(self.usage_file.exists())

    def test_transformed_upstream_disconnect_is_returned_as_terminal_sse_error(self):
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
            fail_after=True,
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
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)
        rendered = b"".join(downstream.writes)
        self.assertIn(b'"text":"partial"', rendered)
        error_payload = json.loads(
            rendered.rsplit(b"event: error\ndata: ", 1)[1].split(b"\n\n", 1)[0]
        )
        message = error_payload["error"]["message"]
        self.assertIn("Fixture HTTPS", message)
        self.assertIn("HUB_UPSTREAM_STREAM_INTERRUPTED", message)
        self.assertIn("ClientPayloadError", message)

        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["code"], "HUB_UPSTREAM_STREAM_INTERRUPTED")
        self.assertEqual(row["exc"], "ClientPayloadError")
        self.assertIn("Fixture HTTPS", row["message"])

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

    def test_stream_accounting_records_the_downstream_usage_view(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        # nested carrier 证明 prompt_tokens 已含 cached tokens，下游拿到扣减
        # 后的 60；账本一度直接读 bridge 累计属性记成 100，同一次请求的两个
        # 出口就此分岔。记账必须来自下游同一张 receipt。
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b'data: {"id":"chatcmpl_fixture","model":"fixture-model",'
                b'"choices":[{"delta":{"content":"partial"},'
                b'"finish_reason":null}]}\n\n',
                b'data: {"id":"chatcmpl_fixture","model":"fixture-model",'
                b'"choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":100,"completion_tokens":20,'
                b'"prompt_tokens_details":{"cached_tokens":40}}}\n\n',
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
        ), mock.patch.object(hub, "log"), mock.patch.object(
            hub, "record_usage"
        ) as record:
            asyncio.run(hub.handle_messages(request))

        rendered = b"".join(downstream.writes).decode()
        self.assertIn('"input_tokens":60', rendered)
        recorded_usage = record.call_args.args[3]
        self.assertEqual(recorded_usage.get("input_tokens"), 60)
        self.assertEqual(recorded_usage.get("cache_read_input_tokens"), 40)

    def test_complete_accounting_records_the_downstream_receipt_view(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        # 与流式出口同一规则:nested carrier 证明 prompt_tokens 含 cached,
        # 下游 payload 与账本都必须拿到扣减后的 60,不能再各算各的。
        transformed = {
            "id": "chatcmpl_fixture",
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fixture"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        }
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps(transformed).encode("utf-8")],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(hub, "record_usage") as record:
            response = asyncio.run(hub.handle_messages(request))

        body = json.loads(response.text)
        self.assertEqual(body["usage"]["input_tokens"], 60)
        self.assertEqual(body["usage"]["cache_read_input_tokens"], 40)
        recorded_usage = record.call_args.args[3]
        self.assertEqual(recorded_usage.get("input_tokens"), 60)
        self.assertEqual(recorded_usage.get("cache_read_input_tokens"), 40)
        self.assertEqual(record.call_args.kwargs["source"], "upstream")

    def test_native_connect_failure_persists_request_degrade(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "http://127.0.0.1:19090/v1/messages",
            "anthropic",
        )

        class FailingSession:
            def post(self, _url, **_kwargs):
                raise aiohttp.ClientConnectionError("fixture native connect failure")

        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "fixture"},
                ],
            },
            session=FailingSession(),
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 502)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["phase"], "response")
        self.assertEqual(row["exc"], "ClientConnectionError")
        self.assertEqual(row["deg"], ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"])

    def test_nonstream_transformed_turn_persists_request_and_response_degrades(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        transformed = {
            "id": "chatcmpl_fixture",
            "model": "fixture-model",
            "service_tier": "default",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fixture"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps(transformed).encode("utf-8")],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "future_request_field": True,
            },
            session=_FakeSession(upstream),
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(
            row["deg"],
            [
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
            ],
        )

    def test_transformed_response_failure_persists_request_degrade(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps({"future_response_field": True}).encode()],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "future_request_field": True,
            },
            session=_FakeSession(upstream),
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 502)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["phase"], "response")
        self.assertEqual(row["code"], "HUB_UPSTREAM_RESPONSE_INVALID")
        self.assertIn("$", row["message"])
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED"],
        )
        self.assertFalse(self.usage_file.exists())

    def test_transformed_connect_failure_persists_request_degrade(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )

        class FailingSession:
            def post(self, _url, **_kwargs):
                raise aiohttp.ClientConnectionError("fixture connect failure")

        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "future_request_field": True,
            },
            session=FailingSession(),
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 502)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["phase"], "response")
        self.assertEqual(row["exc"], "ClientConnectionError")
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED"],
        )

    def test_nonstream_transformed_upstream_error_persists_request_degrade(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream_body = {
            "error": {
                "code": "fixture_upstream_error",
                "message": "fixture upstream failure",
            }
        }
        upstream = _FakeUpstream(
            500,
            {"Content-Type": "application/json", "x-upstream": "kept-out"},
            [json.dumps(upstream_body).encode("utf-8")],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "future_request_field": True,
            },
            session=_FakeSession(upstream),
        )

        response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 500)
        self.assertEqual(
            json.loads(response.text),
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": (
                        "upstream HTTP 500 (fixture_upstream_error): "
                        "fixture upstream failure"
                    ),
                },
            },
        )
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(
            {key: row.get(key) for key in (
                "phase",
                "channel",
                "model",
                "format",
                "status",
                "code",
                "message",
            )},
            {
                "phase": "response",
                "channel": "fast",
                "model": "fixture-model",
                "format": "openai_chat",
                "status": 500,
                "code": "fixture_upstream_error",
                "message": "fixture upstream failure",
            },
        )
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED"],
        )
        self.assertNotIn("payload", row)
        self.assertNotIn("upstream_body", row)

    def test_unwritable_usage_journal_does_not_break_transformed_turn(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [
                json.dumps(
                    {
                        "id": "chatcmpl_fixture",
                        "model": "fixture-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "fixture",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ).encode("utf-8")
            ],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(
            hub,
            "_open_usage_log",
            side_effect=OSError("fixture journal unavailable"),
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.text)["content"][0]["text"], "fixture")
        self.assertIsNone(hub._usage_fp)

    def test_complete_accounting_without_usage_records_unavailable(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        transformed = {
            "id": "chatcmpl_fixture",
            "model": "fixture-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fixture"},
                    "finish_reason": "stop",
                }
            ],
        }
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps(transformed).encode("utf-8")],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(hub, "record_usage") as record:
            response = asyncio.run(hub.handle_messages(request))

        # 下游 payload 仍携带 schema 要求的 0 占位,账本一个计数器都不落。
        body = json.loads(response.text)
        self.assertEqual(
            body["usage"], {"input_tokens": 0, "output_tokens": 0}
        )
        self.assertEqual(record.call_args.args[3], {})
        self.assertEqual(record.call_args.kwargs["source"], "unavailable")

    def test_native_json_empty_usage_object_is_not_upstream_evidence(self):
        self._set_provider_endpoint(
            "Fixture HTTPS", "http://127.0.0.1:19090/v1/messages", "anthropic"
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [b'{"id":"msg_fixture","type":"message","role":"assistant",'
             b'"content":[],"model":"fixture-model","usage":{}}'],
        )
        request = self._request(
            {"model": "fast,custom-model", "messages": []},
            session=_FakeSession(upstream),
        )

        with mock.patch.object(
            hub.web, "StreamResponse", _FakeDownstream
        ), mock.patch.object(hub, "record_usage") as record:
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        # 空 usage 对象不含任何已观测计数器;这个出口曾经只凭 key 存在就记
        # upstream,与其余三个记账出口的非空判定分岔。
        self.assertEqual(record.call_args.args[3], {})
        self.assertEqual(record.call_args.kwargs["source"], "unavailable")

    def test_native_json_usage_keeps_upstream_source(self):
        self._set_provider_endpoint(
            "Fixture HTTPS", "http://127.0.0.1:19090/v1/messages", "anthropic"
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [b'{"id":"msg_fixture","type":"message","role":"assistant",'
             b'"content":[],"model":"fixture-model",'
             b'"usage":{"input_tokens":7,"output_tokens":2}}'],
        )
        request = self._request(
            {"model": "fast,custom-model", "messages": []},
            session=_FakeSession(upstream),
        )

        with mock.patch.object(
            hub.web, "StreamResponse", _FakeDownstream
        ), mock.patch.object(hub, "record_usage") as record:
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(
            record.call_args.args[3],
            {"input_tokens": 7, "output_tokens": 2},
        )
        self.assertEqual(record.call_args.kwargs["source"], "upstream")

    def test_native_nonstream_success_persists_request_degrade_without_rewriting_response(self):
        self._set_provider_endpoint(
            "Fixture HTTPS", "http://127.0.0.1:19090/v1/messages", "anthropic"
        )
        response_bytes = (
            b'{"id":"msg_fixture","type":"message","role":"assistant",'
            b'"content":[],"model":"fixture-model",'
            b'"usage":{"input_tokens":7,"output_tokens":2}}'
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json", "X-Upstream-Trace": "kept"},
            [response_bytes],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "max_tokens": 16,
                "messages": [
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "hello"},
                ],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Upstream-Trace"], "kept")
        self.assertEqual(response.writes, [response_bytes])
        self.assertTrue(response.eof)
        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(row["source"], "upstream")
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"],
        )

    def test_native_stream_success_persists_request_degrade_and_forwards_real_terminal(self):
        self._set_provider_endpoint(
            "Fixture HTTPS", "http://127.0.0.1:19090/v1/messages", "anthropic"
        )
        chunks = [
            (
                b'event: message_start\ndata: {"type":"message_start",'
                b'"message":{"id":"msg_fixture","type":"message",'
                b'"role":"assistant","content":[],"model":"fixture-model",'
                b'"usage":{"input_tokens":7,"output_tokens":0}}}\n\n'
            ),
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream", "X-Upstream-Trace": "kept"},
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "max_tokens": 16,
                "stream": True,
                "messages": [
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "hello"},
                ],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertEqual(response.headers["X-Upstream-Trace"], "kept")
        self.assertEqual(response.writes, chunks)
        self.assertEqual(response.writes[-1], chunks[-1])
        self.assertTrue(response.eof)
        self.assertFalse(request.transport.aborted)
        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(row["source"], "upstream")
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"],
        )

    def test_transformed_stream_clean_eof_without_terminal_returns_sse_error(self):
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
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        rendered = b"".join(downstream.writes)
        self.assertIn(b'"text":"partial"', rendered)
        self.assertNotIn(b"event: message_delta\n", rendered)
        self.assertNotIn(b"event: message_stop\n", rendered)
        self.assertIn(b"event: error\n", rendered)
        error_payload = json.loads(
            rendered.rsplit(b"event: error\ndata: ", 1)[1].split(b"\n\n", 1)[0]
        )
        self.assertIn(
            "HUB_SSE_MISSING_TERMINAL",
            error_payload["error"]["message"],
        )
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)

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

    def test_transformed_stream_persists_request_and_runtime_degrades(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        events = [
            {
                "id": "chatcmpl_fixture",
                "model": "fixture-model",
                "choices": [
                    {
                        "delta": {"reasoning_content": "unsigned thought"},
                        "finish_reason": None,
                    }
                ],
                "usage": {"prompt_tokens": 3},
            },
            {
                "choices": [
                    {"delta": {"content": "answer"}, "finish_reason": None}
                ]
            },
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 2},
            },
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [
                b"data:"
                + json.dumps(event, separators=(",", ":")).encode()
                + b"\n\n"
                for event in events
            ]
            + [b"data: [DONE]\n\n"],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
                "future_request_field": True,
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertTrue(downstream.eof)
        rendered = b"".join(downstream.writes)
        self.assertIn(b"event: message_stop\n", rendered)
        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(
            row["deg"],
            [
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_DEGRADE_UNSIGNED_THINKING",
            ],
        )

    def test_transformed_stream_failure_persists_observed_degrades(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        events = [
            {
                "id": "chatcmpl_fixture",
                "model": "fixture-model",
                "choices": [
                    {
                        "delta": {"reasoning_content": "unsigned thought"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {"delta": {"content": "partial"}, "finish_reason": None}
                ]
            },
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
            fail_after=True,
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
                "future_request_field": True,
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertTrue(downstream.eof)
        self.assertFalse(request.transport.aborted)
        rendered = b"".join(downstream.writes)
        self.assertIn(b"event: error\n", rendered)
        self.assertNotIn(b"event: message_stop\n", rendered)
        error_payload = json.loads(
            rendered.rsplit(b"event: error\ndata: ", 1)[1].split(b"\n\n", 1)[0]
        )
        self.assertIn(
            "HUB_UPSTREAM_STREAM_INTERRUPTED",
            error_payload["error"]["message"],
        )
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(
            row["deg"],
            [
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_DEGRADE_UNSIGNED_THINKING",
            ],
        )
        self.assertFalse(self.usage_file.exists())

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

    def test_synthesized_sse_persists_request_and_response_degrades(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "https://upstream.invalid/v1/chat/completions",
            "openai_chat",
        )
        transformed = {
            "id": "chatcmpl_fixture",
            "model": "fixture-model",
            "service_tier": "default",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [json.dumps(transformed).encode()],
        )
        request = self._request(
            {
                "model": "fast,fixture-model",
                "messages": [{"role": "user", "content": "fixture"}],
                "stream": True,
                "future_request_field": True,
            },
            session=_FakeSession(upstream),
        )

        response = asyncio.run(hub.handle_messages(request))

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
        row = json.loads(self.usage_file.read_text(encoding="utf-8"))
        self.assertEqual(
            row["deg"],
            [
                "HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED",
                "HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED",
            ],
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

    def test_provider_snapshot_cache_hit_does_not_copy(self):
        first = hub.get_providers()
        self.assertIn("Fixture HTTPS", first)

        with mock.patch.object(hub.shutil, "copyfile") as copyfile:
            second = hub.get_providers()

        copyfile.assert_not_called()
        self.assertIs(second, first)

    def test_provider_snapshot_cache_singleflight(self):
        real_read = hub._read_provider_snapshot
        calls = []
        results = []
        errors = []
        barrier = threading.Barrier(8)

        def spy(path):
            calls.append(path)
            return real_read(path)

        def worker():
            try:
                barrier.wait()
                results.append(hub.get_providers())
            except Exception as exc:  # noqa: BLE001 - report, don't hide
                errors.append(exc)

        with mock.patch.object(hub, "_read_provider_snapshot", side_effect=spy):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(calls), 1)
        for result in results:
            self.assertIs(result, results[0])

    def test_provider_snapshot_cache_db_replace_visible_without_reset(self):
        initial = hub.get_providers()
        self.assertEqual(
            initial["Fixture HTTPS"]["token"], "fixture-upstream-token"
        )
        original_inode = self.db_file.stat().st_ino

        replacement = self.root / "replacement.db"
        connection = sqlite3.connect(replacement)
        try:
            connection.execute(
                "CREATE TABLE providers "
                "(name TEXT, app_type TEXT, settings_config TEXT)"
            )
            connection.execute(
                "INSERT INTO providers VALUES (?, 'claude', ?)",
                (
                    "Fixture HTTPS",
                    json.dumps(
                        {
                            "env": {
                                "ANTHROPIC_BASE_URL": "https://replaced.invalid/v1",
                                "ANTHROPIC_AUTH_TOKEN": "replaced-token",
                            }
                        }
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        replacement.chmod(0o600)
        os.replace(replacement, self.db_file)
        self.assertNotEqual(self.db_file.stat().st_ino, original_inode)

        # Intentionally no reset_caches(): a replaced DB must be re-read.
        providers = hub.get_providers()

        self.assertIsNot(providers, initial)
        self.assertEqual(providers["Fixture HTTPS"]["token"], "replaced-token")
        self.assertEqual(
            providers["Fixture HTTPS"]["base_url"], "https://replaced.invalid"
        )

    def test_provider_snapshot_cache_permission_widening_fails_closed(self):
        cached = hub.get_providers()
        self.assertIn("Fixture HTTPS", cached)

        # No reset_caches(): the warm cache must not bypass the permission
        # check on the hit path.
        self.db_file.chmod(0o644)

        with self.assertRaisesRegex(hub.ProviderDatabaseError, "exceed 0600"):
            hub.get_providers()

    def test_provider_snapshot_cache_deleted_database_fails_closed(self):
        cached = hub.get_providers()
        self.assertIn("Fixture HTTPS", cached)

        # No reset_caches(): a deleted database must not be served from the
        # warm cache.
        self.db_file.unlink()

        with self.assertRaisesRegex(
            hub.ProviderDatabaseError, "cannot be resolved"
        ):
            hub.get_providers()

    def test_snapshot_state_oserror_is_wrapped_as_database_error(self):
        cached = hub.get_providers()
        self.assertIn("Fixture HTTPS", cached)

        # _database_snapshot_state runs before the snapshot read. A bare
        # OSError (EACCES/ENOTDIR/ESTALE/EIO) escaping get_providers() would
        # bypass controlled_error_middleware and degrade the controlled 503
        # JSON into an aiohttp HTML 500.
        with mock.patch.object(
            hub,
            "_database_snapshot_state",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            with self.assertRaisesRegex(
                hub.ProviderDatabaseError, "provider database could not be read"
            ):
                hub.get_providers()

        # The warm entry must survive the failed revision check.
        self.assertIs(hub.get_providers(), cached)

    def test_provider_snapshot_waiter_accepts_concurrent_refresh(self):
        cached = hub.get_providers()
        self.assertIn("Fixture HTTPS", cached)

        fresh = {"Fresh HTTPS": {"token": "fresh-token"}}
        real_lock = hub._snapshot_lock

        class SwapOnEnter:
            """Simulate a thread that refreshed while this one waited."""

            def __enter__(self):
                real_lock.__enter__()
                hub._snapshot_entry = ((0, 0, 0, 0, 0), fresh)
                return self

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        # Force a miss with a revision that matches neither the warm entry
        # nor the swapped-in one. The waiter must accept the concurrently
        # refreshed entry instead of serializing a refresh of its own.
        with mock.patch.object(
            hub,
            "_database_snapshot_state",
            side_effect=lambda path: ((1, 1, 1, 1, 1), None),
        ), mock.patch.object(
            hub, "_snapshot_lock", SwapOnEnter()
        ), mock.patch.object(
            hub, "_read_provider_snapshot"
        ) as read:
            result = hub.get_providers()

        self.assertIs(result, fresh)
        read.assert_not_called()

    def test_provider_snapshot_refresh_failure_is_counted_and_logged(self):
        hub.open_log()
        hub.get_providers()  # warm cache: misses=1, refreshes=1

        with mock.patch.object(
            hub,
            "_database_snapshot_state",
            side_effect=lambda path: ((0, 0, 0, 0, 0), None),
        ), mock.patch.object(
            hub,
            "_read_provider_snapshot",
            side_effect=hub.ProviderDatabaseError("boom"),
        ):
            with self.assertRaisesRegex(hub.ProviderDatabaseError, "boom"):
                hub.get_providers()

        # miss counted at entrance, refresh only on success: no longer equal.
        self.assertEqual(hub._snapshot_misses, 2)
        self.assertEqual(hub._snapshot_refreshes, 1)
        self.assertEqual(hub._snapshot_refresh_failures, 1)

        failure_lines = [
            line
            for line in self.log_file.read_text(encoding="utf-8").splitlines()
            if "refresh_failed" in line
        ]
        self.assertEqual(len(failure_lines), 1, failure_lines)
        self.assertIn("failures=1", failure_lines[0])
        self.assertIn("error=ProviderDatabaseError", failure_lines[0])
        # Sanitized: no paths, provider names, or payload.
        self.assertNotIn(str(self.db_file), failure_lines[0])
        self.assertNotIn("Fixture", failure_lines[0])

    def test_provider_snapshot_cache_hit_path_still_checks_permissions(self):
        cached = hub.get_providers()
        self.assertIn("Fixture HTTPS", cached)

        # Database untouched, so the fingerprint still matches the warm
        # entry. If the permission check were ever moved after the cache
        # lookup, this call would silently return the cached dict instead
        # of raising.
        with mock.patch.object(
            hub,
            "_require_private_database",
            side_effect=hub.ProviderDatabaseError("boom"),
        ):
            with self.assertRaisesRegex(hub.ProviderDatabaseError, "boom"):
                hub.get_providers()

    def test_provider_snapshot_cache_refresh_failure_keeps_entry(self):
        cached = hub.get_providers()
        self.assertIn("Fixture HTTPS", cached)

        # Force a miss with a fake revision and a failing refresh. The warm
        # entry must survive: a poisoned entry would serve None, a cleared
        # entry would re-read and produce a new object below.
        with mock.patch.object(
            hub,
            "_database_snapshot_state",
            side_effect=lambda path: ((0, 0, 0, 0, 0), None),
        ), mock.patch.object(
            hub,
            "_read_provider_snapshot",
            side_effect=hub.ProviderDatabaseError("boom"),
        ):
            with self.assertRaisesRegex(hub.ProviderDatabaseError, "boom"):
                hub.get_providers()

        self.assertIs(hub.get_providers(), cached)

    def test_provider_snapshot_cache_error_does_not_poison_or_clear(self):
        initial = hub.get_providers()
        self.assertEqual(
            initial["Fixture HTTPS"]["token"], "fixture-upstream-token"
        )

        self.db_file.unlink()
        with self.assertRaises(hub.ProviderDatabaseError):
            hub.get_providers()

        replacement = self.root / "restored.db"
        connection = sqlite3.connect(replacement)
        try:
            connection.execute(
                "CREATE TABLE providers "
                "(name TEXT, app_type TEXT, settings_config TEXT)"
            )
            connection.execute(
                "INSERT INTO providers VALUES (?, 'claude', ?)",
                (
                    "Fixture HTTPS",
                    json.dumps(
                        {
                            "env": {
                                "ANTHROPIC_BASE_URL": "https://restored.invalid/v1",
                                "ANTHROPIC_AUTH_TOKEN": "restored-token",
                            }
                        }
                    ),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        replacement.chmod(0o600)
        os.replace(replacement, self.db_file)

        real_read = hub._read_provider_snapshot
        calls = []

        def spy(path):
            calls.append(path)
            return real_read(path)

        with mock.patch.object(hub, "_read_provider_snapshot", side_effect=spy):
            providers = hub.get_providers()

        # Recovery is a fresh read of the restored database, not the
        # pre-failure cache and not a poisoned entry written by the error.
        self.assertEqual(len(calls), 1)
        self.assertIsNot(providers, initial)
        self.assertEqual(providers["Fixture HTTPS"]["token"], "restored-token")
        self.assertEqual(
            providers["Fixture HTTPS"]["base_url"], "https://restored.invalid"
        )

    def test_reset_caches_clears_provider_snapshot(self):
        first = hub.get_providers()
        self.assertIn("Fixture HTTPS", first)

        real_read = hub._read_provider_snapshot
        calls = []

        def spy(path):
            calls.append(path)
            return real_read(path)

        with mock.patch.object(hub, "_read_provider_snapshot", side_effect=spy):
            hub.reset_caches()
            self.assertIsNone(hub._snapshot_entry)
            second = hub.get_providers()

        self.assertEqual(len(calls), 1)
        self.assertIsNot(second, first)
        self.assertIn("Fixture HTTPS", second)

    def test_cli_doctor_reads_source_directly(self):
        initial = hub.get_providers()
        self.assertIn("Fixture HTTPS", initial)

        writer = sqlite3.connect(self.db_file)
        try:
            self.assertEqual(
                writer.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                "wal",
            )
            writer.execute("PRAGMA wal_autocheckpoint=0")
            new_env = {
                "ANTHROPIC_BASE_URL": "https://doctor-wal.invalid/v1",
                "ANTHROPIC_AUTH_TOKEN": "fixture-upstream-token",
            }
            writer.execute(
                "INSERT INTO providers VALUES (?, 'claude', ?)",
                ("Doctor WAL", json.dumps({"env": new_env})),
            )
            writer.commit()
            sidecars = hub._sqlite_sidecars(self.db_file)
            for sidecar in sidecars:
                self.assertTrue(sidecar.exists())
                sidecar.chmod(0o600)

            # Intentionally no reset_caches(): the doctor path must read the
            # source directly rather than be masked by the warm cache.
            providers, _verified = hub._read_provider_snapshot(self.db_file)
            self.assertIn("Doctor WAL", providers)
            self.assertEqual(
                providers["Doctor WAL"]["base_url"],
                "https://doctor-wal.invalid",
            )

            # End-to-end: cli_doctor itself must not consult the cache.
            with mock.patch.object(hub, "get_providers") as cached_read:
                out = io.StringIO()
                with redirect_stdout(out):
                    hub.cli_doctor()
            cached_read.assert_not_called()
        finally:
            writer.close()

    def test_provider_snapshot_refresh_logs_sanitized_metrics(self):
        hub.open_log()
        hub.get_providers()

        log_text = self.log_file.read_text(encoding="utf-8")
        snapshot_lines = [
            line for line in log_text.splitlines() if "provider_snapshot" in line
        ]
        self.assertEqual(len(snapshot_lines), 1, log_text)
        line = snapshot_lines[0]

        fields = {}
        for token in line.split():
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key] = value

        for key in (
            "refresh_ms",
            "hits",
            "misses",
            "refreshes",
            "p50_ms",
            "p95_ms",
        ):
            self.assertIn(key, fields, f"missing {key} in: {line}")
            self.assertTrue(
                fields[key].lstrip("-").isdigit(),
                f"{key} is not an integer: {fields[key]}",
            )
            self.assertGreaterEqual(
                int(fields[key]), 0, f"{key} is negative: {fields[key]}"
            )

        # Sanitization: the line carries exactly the six expected numeric
        # fields — no room for paths, provider names, tokens, or raw
        # fingerprint components.
        self.assertEqual(
            set(fields),
            {"refresh_ms", "hits", "misses", "refreshes", "p50_ms", "p95_ms"},
            line,
        )
        self.assertNotIn(str(self.db_file), line)
        self.assertNotIn("Fixture HTTPS", line)
        self.assertNotIn("Fixture HTTP", line)
        self.assertNotIn("Fixture Loopback", line)
        self.assertNotIn("fixture-upstream-token", line)
        self.assertNotIn("fixture-local-token", line)

    def test_format_protocol_warnings_aggregates_by_code(self):
        details = [
            "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED@$.messages[3].role",
            "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED@$.messages[6].role",
            "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED@$.messages[9].role",
            "HUB_DEGRADE_DROPPED_CACHE_CONTROL@$.messages[1]",
        ]

        rendered = hub._format_protocol_warnings(details)

        # Bounded length: the raw list grows per turn, the aggregate does not.
        self.assertEqual(
            rendered,
            "HUB_DEGRADE_SYSTEM_ROLE_PROMOTED x3 (first: $.messages[3].role)"
            ",HUB_DEGRADE_DROPPED_CACHE_CONTROL@$.messages[1]",
        )
        # One locating sample per code, counts preserved.
        self.assertIn("x3", rendered)
        self.assertIn("$.messages[3].role", rendered)
        self.assertNotIn("$.messages[6].role", rendered)

    def test_format_protocol_warnings_bare_codes_and_singletons(self):
        self.assertEqual(
            hub._format_protocol_warnings(["CODE_A", "CODE_A", "CODE_B"]),
            "CODE_A x2,CODE_B",
        )
        self.assertEqual(hub._format_protocol_warnings([]), "")
        self.assertEqual(
            hub._format_protocol_warnings(["CODE_A@$.x"]), "CODE_A@$.x"
        )

    def test_provider_snapshot_cache_hit_does_not_log(self):
        hub.open_log()
        hub.get_providers()

        before = self.log_file.read_text(encoding="utf-8")
        snapshot_lines_before = [
            line for line in before.splitlines() if "provider_snapshot" in line
        ]
        self.assertEqual(len(snapshot_lines_before), 1)

        hub.get_providers()

        after = self.log_file.read_text(encoding="utf-8")
        snapshot_lines_after = [
            line for line in after.splitlines() if "provider_snapshot" in line
        ]
        self.assertEqual(len(snapshot_lines_after), 1)
        self.assertEqual(snapshot_lines_after[0], snapshot_lines_before[0])

    def test_provider_snapshot_reset_caches_clears_metrics(self):
        hub.open_log()
        hub.get_providers()
        hub.get_providers()  # cache hit
        self.assertEqual(hub._snapshot_refreshes, 1)
        self.assertEqual(hub._snapshot_hits, 1)
        self.assertEqual(len(hub._snapshot_refresh_samples), 1)

        hub.reset_caches()

        self.assertEqual(hub._snapshot_hits, 0)
        self.assertEqual(hub._snapshot_misses, 0)
        self.assertEqual(hub._snapshot_refreshes, 0)
        self.assertEqual(len(hub._snapshot_refresh_samples), 0)

    def test_snapshot_percentile_boundaries(self):
        self.assertEqual(hub._snapshot_percentile([], 50), 0)
        self.assertEqual(hub._snapshot_percentile([5], 95), 5)
        self.assertEqual(hub._snapshot_percentile([1, 2], 50), 1)
        self.assertEqual(hub._snapshot_percentile([1, 2, 3, 4], 50), 2)
        self.assertEqual(hub._snapshot_percentile([1, 2, 3, 4], 95), 4)

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
        self.assertEqual(forwarded["model"], "upstream-sonnet")

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

    def test_streaming_count_tokens_is_not_accounted_as_a_turn(self):
        """A streaming count probe stays transparent without a usage row."""
        chunks = [
            (
                b'event: message_start\ndata: {"type":"message_start",'
                b'"message":{"id":"count_fixture","type":"message",'
                b'"role":"assistant","content":[],"usage":{"input_tokens":11,'
                b'"output_tokens":0}}}\n\n'
            ),
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        probe = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "stream": True,
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=_FakeSession(probe),
            path="/v1/messages/count_tokens",
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertEqual(response.writes, chunks)
        self.assertTrue(response.eof)
        self.assertFalse(self.usage_file.exists())

    def test_streaming_count_tokens_error_terminal_is_still_journaled(self):
        """Suppressing the probe's usage row must not suppress its failure.

        The `not is_count` guard sits on the usage branch only, and the error
        branch is tested nowhere else — moving the guard up to the shared
        condition, or reordering the two branches, would drop the failure
        silently and leave the operator with a count probe that fails with no
        journal row at all. Errors attribute; usage counts.
        """
        chunks = [
            b'event: message_start\ndata: {"type":"message_start",'
            b'"message":{"id":"count_fixture","type":"message",'
            b'"role":"assistant","content":[],"usage":{"input_tokens":11,'
            b'"output_tokens":0}}}\n\n',
            b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
            b'"message":"fixture count failure"}}\n\n',
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "stream": True,
                "messages": [
                    # Promoting this system turn is what puts a code in `deg`.
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "fixture"},
                ],
            },
            session=_FakeSession(upstream),
            path="/v1/messages/count_tokens",
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        # The probe is still forwarded byte for byte; only accounting differs.
        self.assertEqual(downstream.writes, chunks)
        self.assertTrue(downstream.eof)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["exc"], "UpstreamSSEError")
        self.assertEqual(row["deg"], ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"])
        # The message_start above reports tokens; a probe still never counts.
        self.assertFalse(self.usage_file.exists())

    def test_count_tokens_preflight_is_not_accounted_as_a_turn(self):
        """R3: a count_tokens probe must not reach the usage log.

        Claude Code sends one count_tokens before every real turn. The probe
        answers with `{"input_tokens": N}` rather than a `usage` object, so
        the row it used to append carried no token counter at all — only a
        timestamp and the request's `deg` list. That doubled both the request
        total and every per-turn degrade count in `claude1 usage`.
        """
        payload = {
            "model": "fast,custom-model",
            "messages": [
                # Promoting this system turn is what puts a code in `deg`.
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "fixture"},
            ],
        }
        turn = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [b'{"usage":{"input_tokens":11,"output_tokens":3}}'],
        )
        probe = _FakeUpstream(
            200,
            {"Content-Type": "application/json"},
            [b'{"input_tokens":11}'],
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            asyncio.run(
                hub.handle_messages(
                    self._request(payload, session=_FakeSession(turn))
                )
            )
            asyncio.run(
                hub.handle_messages(
                    self._request(
                        payload,
                        session=_FakeSession(probe),
                        path="/v1/messages/count_tokens",
                    )
                )
            )

        rows = [
            json.loads(line)
            for line in self.usage_file.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["in"], 11)
        self.assertEqual(rows[0]["deg"], ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"])
        # The contract is the reported number, not just the row count.
        self.assertEqual(
            usage_report._degrade_counts(rows),
            {"HUB_DEGRADE_SYSTEM_ROLE_PROMOTED": 1},
        )

    def test_count_tokens_failure_still_carries_its_degrade_codes(self):
        """A failed probe is one failure to attribute, not a counted turn.

        The journal names why a single request failed, so suppressing `deg`
        there would hide the request-side degradation without protecting any
        counter — usage is where per-turn counting happens, and the probe no
        longer writes there at all.
        """
        probe = _FakeUpstream(
            500,
            {"Content-Type": "application/json"},
            [b'{"error":{"message":"upstream exploded"}}'],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "fixture"},
                ],
            },
            session=_FakeSession(probe),
            path="/v1/messages/count_tokens",
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            asyncio.run(hub.handle_messages(request))

        self.assertFalse(self.usage_file.exists())
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["deg"], ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"])
        self.assertEqual(row["status"], 500)

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

    def test_native_json_error_body_is_journaled_with_its_real_reason(self):
        # 原生路径是字节透传,错误体以前不进缓冲,于是"用户额度不足"只出现在
        # 上游响应里,事后无处可查。现在它必须落进 journal。
        self._set_provider_endpoint(
            "Fixture HTTPS", "http://127.0.0.1:19090/v1/messages", "anthropic"
        )
        upstream = _FakeUpstream(
            403,
            {"Content-Type": "application/json"},
            [
                json.dumps(
                    {
                        "error": {
                            "code": "",
                            "message": "用户额度不足",
                            "type": "insufficient_quota",
                        }
                    }
                ).encode("utf-8")
            ],
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "messages": [
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "hello"},
                ],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            response = asyncio.run(hub.handle_messages(request))

        self.assertEqual(response.status, 403)
        row = json.loads(self.errors_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((row["phase"], row["status"]), ("response", 403))
        self.assertEqual(row["channel"], "fast")
        self.assertEqual(row["message"], "用户额度不足")
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"],
        )
        # 空 code 不是证据,不该写进 journal 冒充上游代码。
        self.assertNotIn("code", row)

    def test_stream_abort_is_journaled_so_mid_response_stays_findable(self):
        # 用户看到的 "mid-response" 就是这条路径:下游已提交,只能 abort。
        # 至少要在 journal 里留下是哪个渠道、哪种异常。
        self._set_provider_endpoint(
            "Fixture HTTPS", "http://127.0.0.1:19090/v1/messages", "anthropic"
        )
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            [b'event: message_start\ndata: {"type":"message_start"}\n\n'],
            fail_after=True,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "stream": True,
                "messages": [
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "hello"},
                ],
            },
            session=_FakeSession(upstream),
        )

        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            with self.assertRaises(hub.UpstreamStreamAborted):
                asyncio.run(hub.handle_messages(request))

        row = json.loads(self.errors_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["phase"], "stream")
        self.assertEqual(row["channel"], "fast")
        self.assertEqual(row["format"], "anthropic")
        self.assertEqual(row["exc"], "ClientPayloadError")
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"],
        )
        self.assertFalse(self.usage_file.exists())

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

    def test_provider_snapshot_is_read_only_across_request_path(self):
        self._write_account_pool_db()
        self._write_account_pool_config()
        # Give every pooled provider a nested transport dict so the deep
        # comparison below covers nested mutable values, not just scalars.
        connection = sqlite3.connect(self.db_file)
        try:
            rows = connection.execute(
                "SELECT id, settings_config FROM providers"
            ).fetchall()
            for provider_id, raw_settings in rows:
                settings = json.loads(raw_settings)
                settings["transport"] = {
                    "mode": "proxy",
                    "proxies": ["http://127.0.0.1:7897"],
                }
                # Distinct names so each record is aliased by both its name
                # and its id key — the shared-identity shape of §2.
                connection.execute(
                    "UPDATE providers SET name = ?, settings_config = ? "
                    "WHERE id = ?",
                    (
                        f"Pooled account {provider_id}",
                        json.dumps(settings),
                        provider_id,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

        snapshot = hub.get_providers()
        self.assertIn("id:primary", snapshot)
        self.assertIn("id:secondary", snapshot)
        self.assertIs(snapshot["Pooled account primary"], snapshot["id:primary"])
        self.assertIs(
            snapshot["Pooled account secondary"], snapshot["id:secondary"]
        )
        baseline = copy.deepcopy(snapshot)

        # Successful request: round-robin acquires the primary account.
        session = _FakeSession(
            _FakeUpstream(
                200,
                {"Content-Type": "application/json"},
                [b'{"usage":{"input_tokens":1,"output_tokens":1}}'],
            )
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
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0][1]["headers"]["x-api-key"],
            "fixture-primary-account-token",
        )

        # Failover request: the round-robin cursor advanced by the first
        # request, so the secondary account 429s first and the hub retries
        # the primary account within the same request.
        failover_session = _SequencedFakeSession(
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
        failover_request = self._request(
            {
                "model": "fast,pooled-model",
                "messages": [{"role": "user", "content": "fixture"}],
            },
            session=failover_session,
        )
        with mock.patch.object(hub.web, "StreamResponse", _FakeDownstream):
            failover_response = asyncio.run(hub.handle_messages(failover_request))
        self.assertEqual(failover_response.status, 200)
        self.assertEqual(
            [call[1]["headers"]["x-api-key"] for call in failover_session.calls],
            [
                "fixture-secondary-account-token",
                "fixture-primary-account-token",
            ],
        )
        self.assertEqual(
            failover_response.headers["x-hub-account"], "id:primary"
        )

        # The full request path (routing, account-pool acquire/failover,
        # forwarding) must leave the shared snapshot object untouched.
        self.assertIs(hub.get_providers(), snapshot)
        self.assertEqual(snapshot, baseline)
        # Redundant with the deep assertEqual above; kept so a violation
        # names the exact nested key that was mutated.
        for selector, record in snapshot.items():
            self.assertEqual(record["token"], baseline[selector]["token"])
            self.assertEqual(
                record["model_map"], baseline[selector]["model_map"]
            )
            self.assertEqual(
                record["transport"], baseline[selector]["transport"]
            )

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
        self.assertEqual(response.headers["x-hub-upstream-code"], "rate_limit")
        body = json.loads(response.text)
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        self.assertEqual(
            body["error"]["message"],
            "upstream HTTP 429 (rate_limit): slow down",
        )
        # 同一份证据同时进 journal,事后 `claude-hub errors` 才查得到。
        row = json.loads(self.errors_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((row["phase"], row["status"]), ("response", 429))
        self.assertEqual(row["format"], "openai_chat")
        self.assertEqual((row["code"], row["message"]), ("rate_limit", "slow down"))

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

    def test_forwarding_1m_selector_is_format_aware(self):
        cases = [
            (
                "anthropic",
                "http://127.0.0.1:19090/v1/messages",
                "claude-sonnet-4[1m]",
                "claude-sonnet-4",
                [b'{"type":"message","content":[]}'],
            ),
            (
                "openai_chat",
                "http://127.0.0.1:19090/v1/chat/completions",
                "glm-5.3[1M]",
                "glm-5.3[1M]",
                [
                    json.dumps(
                        {
                            "id": "chat_1",
                            "model": "glm-5.3[1M]",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "hi",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ).encode()
                ],
            ),
            (
                "openai_responses",
                "http://127.0.0.1:19090/custom/responses",
                "custom-model[1m]",
                "custom-model[1m]",
                [
                    json.dumps(
                        {
                            "id": "resp_1",
                            "model": "custom-model[1m]",
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "content": [
                                        {"type": "output_text", "text": "hi"}
                                    ],
                                }
                            ],
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": 1,
                            },
                        }
                    ).encode()
                ],
            ),
        ]

        for api_format, endpoint, model, expected_upstream_model, chunks in cases:
            with self.subTest(api_format=api_format, model=model):
                self._configure_provider_and_channel(
                    "fast", model, endpoint, api_format
                )

                upstream = _FakeUpstream(
                    200,
                    {"Content-Type": "application/json"},
                    chunks,
                )
                session = _FakeSession(upstream)
                request = self._request(
                    {
                        "model": f"fast,{model}",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    session=session,
                )

                with mock.patch.object(
                    hub.web, "StreamResponse", _FakeDownstream
                ):
                    response = asyncio.run(hub.handle_messages(request))

                self.assertEqual(response.status, 200)
                self.assertEqual(len(session.calls), 1)
                url, kwargs = session.calls[0]
                self.assertEqual(url, endpoint)
                payload = json.loads(kwargs["data"])
                self.assertEqual(payload["model"], expected_upstream_model)

                if api_format == "anthropic":
                    self.assertIn(
                        "context-1m-2025-08-07",
                        kwargs["headers"]["anthropic-beta"],
                    )
                else:
                    self.assertNotIn("anthropic-beta", kwargs["headers"])

    def test_check_and_serve_agree_on_upstream_model_for_1m_selector(self):
        cases = [
            (
                "anthropic",
                "http://127.0.0.1:19090/v1/messages",
                "claude-sonnet-4[1m]",
                "claude-sonnet-4",
            ),
            (
                "openai_chat",
                "http://127.0.0.1:19090/v1/chat/completions",
                "glm-5.3[1M]",
                "glm-5.3[1M]",
            ),
            (
                "openai_responses",
                "http://127.0.0.1:19090/custom/responses",
                "custom-model[1m]",
                "custom-model[1m]",
            ),
        ]

        for api_format, endpoint, model, expected_upstream_model in cases:
            with self.subTest(api_format=api_format, model=model):
                self._configure_provider_and_channel(
                    "fast", model, endpoint, api_format
                )

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

                self.assertEqual(len(session.calls), 1)
                url, kwargs = session.calls[0]
                self.assertEqual(url, endpoint)
                self.assertEqual(kwargs["json"]["model"], expected_upstream_model)

                if api_format == "anthropic":
                    self.assertIn(
                        "context-1m-2025-08-07",
                        kwargs["headers"]["anthropic-beta"],
                    )
                else:
                    self.assertNotIn("anthropic-beta", kwargs["headers"])

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
                "messages": [
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "fixture"},
                ],
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
        row = json.loads(self.errors_file.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["exc"], "IncompleteSSE")
        self.assertEqual(
            row["deg"],
            ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"],
        )

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

    def test_native_sse_error_terminal_is_journaled_as_failure_not_usage(self):
        self._set_provider_endpoint(
            "Fixture HTTPS",
            "http://127.0.0.1:19090/v1/messages",
            "anthropic",
        )
        chunks = [
            b'event: message_start\ndata: {"type":"message_start"}\n\n',
            b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
            b'"message":"fixture native failure"}}\n\n',
        ]
        upstream = _FakeUpstream(
            200,
            {"Content-Type": "text/event-stream"},
            chunks,
        )
        request = self._request(
            {
                "model": "fast,custom-model",
                "stream": True,
                "messages": [
                    {"role": "system", "content": "system context"},
                    {"role": "user", "content": "fixture"},
                ],
            },
            session=_FakeSession(upstream),
        )
        downstream = _FakeDownstream(200)

        with mock.patch.object(
            hub.web, "StreamResponse", return_value=downstream
        ):
            response = asyncio.run(hub.handle_messages(request))

        self.assertIs(response, downstream)
        self.assertEqual(downstream.writes, chunks)
        self.assertTrue(downstream.eof)
        row = json.loads(self.errors_file.read_text(encoding="utf-8"))
        self.assertEqual(row["exc"], "UpstreamSSEError")
        self.assertEqual(row["deg"], ["HUB_DEGRADE_SYSTEM_ROLE_PROMOTED"])
        self.assertFalse(self.usage_file.exists())

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
