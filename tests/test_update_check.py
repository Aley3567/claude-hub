from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.update_check import (  # noqa: E402
    AdviceKind,
    FileUpdateCache,
    HttpRequest,
    HttpResponse,
    InstallKind,
    PIP_UPGRADE_COMMAND,
    RELEASES_API_URL,
    RELEASE_PAGE_PREFIX,
    ReleaseChannel,
    UpdateCheckSettings,
    UpdateChecker,
    UpdateStatus,
)


def _release(
    tag: str,
    *,
    prerelease: bool = False,
    draft: bool = False,
    **private_metadata: object,
) -> dict[str, object]:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        **private_metadata,
    }


class _FakeHttpClient:
    def __init__(
        self,
        response: HttpResponse | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class _FakeCache:
    def __init__(self, entry: dict[str, object] | None = None) -> None:
        self.entry = entry
        self.loads = 0
        self.stores: list[dict[str, object]] = []

    def load(self) -> dict[str, object] | None:
        self.loads += 1
        return self.entry

    def store(self, entry) -> None:
        copied = dict(entry)
        self.stores.append(copied)
        self.entry = copied


def _response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
    )


class UpdateCheckerTests(unittest.TestCase):
    def test_defaults_are_enabled_stable_low_frequency_checks(self) -> None:
        settings = UpdateCheckSettings()

        self.assertTrue(settings.enabled)
        self.assertTrue(settings.cache_enabled)
        self.assertEqual(settings.cache_ttl_seconds, 24 * 60 * 60)
        self.assertIs(settings.channel, ReleaseChannel.STABLE)
        self.assertIs(settings.install_kind, InstallKind.PIP)

    def test_explicit_disable_touches_no_clock_cache_or_network(self) -> None:
        client = _FakeHttpClient(error=AssertionError("network used"))

        class _ExplodingCache:
            def load(self):
                raise AssertionError("cache read")

            def store(self, entry):
                raise AssertionError("cache write")

        def exploding_clock() -> float:
            raise AssertionError("clock used")

        checker = UpdateChecker(
            http_client=client,
            clock=exploding_clock,
            cache=_ExplodingCache(),
            settings=UpdateCheckSettings(enabled=False),
        )

        result = checker.check("0.1.0")

        self.assertIs(result.status, UpdateStatus.DISABLED)
        self.assertEqual(client.requests, [])
        self.assertFalse(result.from_cache)

    def test_request_is_fixed_anonymous_get_and_result_omits_metadata(self) -> None:
        sensitive = "fixture-" + "secret" + "-material"
        private_path = str(pathlib.Path("/private") / "fixture" / "config.json")
        client = _FakeHttpClient(
            _response(
                [
                    _release(
                        "v0.2.0",
                        author={"login": sensitive},
                        body=f"{private_path}: {sensitive}",
                        assets=[{"name": sensitive}],
                    )
                ]
            )
        )
        checker = UpdateChecker(
            http_client=client,
            settings=UpdateCheckSettings(cache_enabled=False),
        )

        result = checker.check("0.1.0")

        self.assertIs(result.status, UpdateStatus.AVAILABLE)
        self.assertEqual(result.latest_version, "0.2.0")
        self.assertEqual(result.advice.kind, AdviceKind.COMMAND)
        self.assertEqual(result.advice.value, PIP_UPGRADE_COMMAND)
        request = client.requests[0]
        self.assertEqual(request.url, RELEASES_API_URL)
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.body)
        self.assertEqual(
            dict(request.headers),
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "claude-hub-update-check",
            },
        )
        request_text = repr(request)
        public_result = json.dumps(result.to_public_dict())
        for forbidden in (
            sensitive,
            private_path,
            "Authorization",
            "provider",
            "device",
            "credential",
        ):
            self.assertNotIn(forbidden, request_text + public_result)

    def test_stable_channel_ignores_prerelease_and_draft_releases(self) -> None:
        client = _FakeHttpClient(
            _response(
                [
                    _release("v9.0.0", draft=True),
                    _release("v2.0.0-beta.1", prerelease=True),
                    _release("v1.0.0"),
                ]
            )
        )
        checker = UpdateChecker(
            http_client=client,
            settings=UpdateCheckSettings(cache_enabled=False),
        )

        result = checker.check("1.0.0")

        self.assertIs(result.status, UpdateStatus.UP_TO_DATE)
        self.assertEqual(result.latest_version, "1.0.0")
        self.assertIsNone(result.advice)

    def test_preview_channel_uses_semver_prerelease_order(self) -> None:
        client = _FakeHttpClient(
            _response(
                [
                    _release("v1.1.0-beta.2", prerelease=True),
                    _release("v1.1.0-beta.10", prerelease=True),
                    _release("v1.0.0"),
                ]
            )
        )
        checker = UpdateChecker(
            http_client=client,
            settings=UpdateCheckSettings(
                cache_enabled=False,
                channel=ReleaseChannel.PREVIEW,
            ),
        )

        result = checker.check("1.1.0-beta.1")

        self.assertIs(result.status, UpdateStatus.AVAILABLE)
        self.assertEqual(result.latest_version, "1.1.0-beta.10")

    def test_package_install_gets_release_page_and_never_an_update_binary(self) -> None:
        client = _FakeHttpClient(_response([_release("release-value")]))
        checker = UpdateChecker(
            http_client=client,
            settings=UpdateCheckSettings(
                cache_enabled=False,
                install_kind=InstallKind.PACKAGE,
            ),
        )
        unavailable = checker.check("0.1.0")
        self.assertIs(unavailable.status, UpdateStatus.UNAVAILABLE)

        client.response = _response([_release("v0.2.0")])
        result = checker.check("0.1.0")

        self.assertIs(result.status, UpdateStatus.AVAILABLE)
        self.assertEqual(result.advice.kind, AdviceKind.RELEASE_PAGE)
        self.assertEqual(
            result.advice.value,
            RELEASE_PAGE_PREFIX + "v0.2.0",
        )
        self.assertNotIn("download", json.dumps(result.to_public_dict()).casefold())
        self.assertNotIn("asset", json.dumps(result.to_public_dict()).casefold())

    def test_offline_rate_limits_and_malformed_responses_are_nonfatal(self) -> None:
        cases = (
            _FakeHttpClient(error=OSError("offline private detail")),
            _FakeHttpClient(_response({}, status=403)),
            _FakeHttpClient(_response({}, status=429)),
            _FakeHttpClient(HttpResponse(status=200, body=b"not-json")),
            _FakeHttpClient(_response({"tag_name": "v2.0.0"})),
            _FakeHttpClient(_response([{"tag_name": "v2.0.0"}])),
            _FakeHttpClient(
                HttpResponse(status=200, body=b"x" * (128 * 1024 + 1))
            ),
        )
        for client in cases:
            with self.subTest(client=client):
                result = UpdateChecker(
                    http_client=client,
                    settings=UpdateCheckSettings(cache_enabled=False),
                ).check("0.1.0")

                self.assertIs(result.status, UpdateStatus.UNAVAILABLE)
                self.assertIsNone(result.advice)

    def test_valid_cache_suppresses_requests_until_ttl_and_is_single_entry(
        self,
    ) -> None:
        now = [1000.0]
        cache = _FakeCache()
        client = _FakeHttpClient(_response([_release("v0.2.0")]))
        checker = UpdateChecker(
            http_client=client,
            clock=lambda: now[0],
            cache=cache,
            settings=UpdateCheckSettings(cache_ttl_seconds=60),
        )

        first = checker.check("0.1.0")
        now[0] = 1059.0
        second = checker.check("0.1.0")
        now[0] = 1060.0
        third = checker.check("0.1.0")

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertFalse(third.from_cache)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(len(cache.stores), 2)
        self.assertLess(
            len(json.dumps(cache.entry, separators=(",", ":")).encode()),
            4096,
        )

    def test_cache_can_be_disabled_and_cache_failures_do_not_escape(self) -> None:
        client = _FakeHttpClient(_response([_release("v0.2.0")]))

        class _ExplodingCache:
            def load(self):
                raise RuntimeError("private cache path")

            def store(self, entry):
                raise RuntimeError("private cache path")

        disabled = UpdateChecker(
            http_client=client,
            cache=_ExplodingCache(),
            settings=UpdateCheckSettings(cache_enabled=False),
        )
        first = disabled.check("0.1.0")
        second = disabled.check("0.1.0")

        self.assertIs(first.status, UpdateStatus.AVAILABLE)
        self.assertIs(second.status, UpdateStatus.AVAILABLE)
        self.assertEqual(len(client.requests), 2)

        enabled = UpdateChecker(
            http_client=client,
            cache=_ExplodingCache(),
        )
        self.assertIs(enabled.check("0.1.0").status, UpdateStatus.AVAILABLE)

    def test_invalid_current_version_does_not_request_or_echo_it(self) -> None:
        sensitive = str(pathlib.Path("/private") / "fixture" / "config.json")
        client = _FakeHttpClient(error=AssertionError("network used"))
        checker = UpdateChecker(http_client=client)

        result = checker.check(sensitive)

        self.assertIs(result.status, UpdateStatus.UNAVAILABLE)
        self.assertIsNone(result.current_version)
        self.assertNotIn(sensitive, json.dumps(result.to_public_dict()))
        self.assertEqual(client.requests, [])


class FileUpdateCacheTests(unittest.TestCase):
    def test_cache_file_is_private_bounded_and_rejects_insecure_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "private" / "update.json"
            cache = FileUpdateCache(path, max_bytes=256)
            entry = {"schema": 1, "value": "public-release-data"}

            cache.store(entry)

            self.assertEqual(cache.load(), entry)
            self.assertLessEqual(path.stat().st_size, 256)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                path.chmod(0o644)
                self.assertIsNone(cache.load())

            path.write_bytes(b"x" * 257)
            if os.name != "nt":
                path.chmod(0o600)
            self.assertIsNone(cache.load())

    def test_cache_rejects_oversized_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = pathlib.Path(raw_directory) / "cache.json"
            cache = FileUpdateCache(path, max_bytes=64)

            with self.assertRaisesRegex(ValueError, "size limit"):
                cache.store({"value": "x" * 100})

            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
