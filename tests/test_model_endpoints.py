from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.model_endpoints import (  # noqa: E402
    MAX_MODEL_ENDPOINT_CANDIDATES,
    ModelEndpointError,
    ModelEndpointReasonCode,
    model_endpoint_candidates,
)


class ModelEndpointTests(unittest.TestCase):
    def assert_rejected(
        self,
        reason: ModelEndpointReasonCode,
        callback,
    ) -> ModelEndpointError:
        with self.assertRaises(ModelEndpointError) as raised:
            callback()
        self.assertEqual(raised.exception.reason_code, reason.value)
        return raised.exception

    def test_openai_candidates_are_bounded_ordered_and_path_aware(self) -> None:
        cases = (
            (
                "https://api.example.test",
                (
                    "https://api.example.test/v1/models",
                    "https://api.example.test/models",
                ),
            ),
            (
                "https://api.example.test/",
                (
                    "https://api.example.test/v1/models",
                    "https://api.example.test/models",
                ),
            ),
            (
                "https://api.example.test/v1",
                ("https://api.example.test/v1/models",),
            ),
            (
                "https://api.example.test/v1/",
                ("https://api.example.test/v1/models",),
            ),
            (
                "https://gateway.example.test/proxy/openai",
                (
                    "https://gateway.example.test/proxy/openai/v1/models",
                    "https://gateway.example.test/proxy/openai/models",
                ),
            ),
            (
                "https://gateway.example.test/proxy/openai/v2/",
                (
                    "https://gateway.example.test/proxy/openai/v2/models",
                ),
            ),
            (
                "https://api.example.test/v1/models",
                ("https://api.example.test/v1/models",),
            ),
        )

        for base_url, expected_urls in cases:
            with self.subTest(base_url=base_url):
                requests = model_endpoint_candidates(base_url, "openai")
                self.assertEqual(
                    tuple(request.url for request in requests),
                    expected_urls,
                )
                self.assertLessEqual(
                    len(requests),
                    MAX_MODEL_ENDPOINT_CANDIDATES,
                )
                self.assertTrue(
                    all(request.method == "GET" for request in requests)
                )

    def test_existing_protocol_names_map_to_deterministic_adapters(self) -> None:
        cases = (
            ("openai", "openai"),
            ("openai_chat", "openai"),
            ("openai_responses", "openai"),
            ("anthropic", "anthropic-compatible"),
            ("anthropic-compatible", "anthropic-compatible"),
        )

        for protocol, adapter in cases:
            with self.subTest(protocol=protocol):
                first = model_endpoint_candidates(
                    "https://gateway.example.test/reverse-proxy/",
                    protocol,
                )
                second = model_endpoint_candidates(
                    "https://gateway.example.test/reverse-proxy/",
                    protocol,
                )
                self.assertEqual(first, second)
                self.assertEqual(
                    tuple(request.adapter for request in first),
                    (adapter, adapter),
                )
                self.assertEqual(
                    tuple(request.url for request in first),
                    (
                        "https://gateway.example.test/reverse-proxy/v1/models",
                        "https://gateway.example.test/reverse-proxy/models",
                    ),
                )

    def test_every_candidate_is_same_origin_and_credential_free(self) -> None:
        requests = model_endpoint_candidates(
            "https://gateway.example.test:8443/proxy/v1/",
            "anthropic-compatible",
        )

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.origin, "https://gateway.example.test:8443")
        self.assertEqual(
            request.url,
            "https://gateway.example.test:8443/proxy/v1/models",
        )
        self.assertEqual(request.display_url, request.url)
        self.assertFalse(hasattr(request, "headers"))
        self.assertFalse(hasattr(request, "credential"))

    def test_unknown_protocol_and_abnormal_base_urls_fail_stably(self) -> None:
        self.assert_rejected(
            ModelEndpointReasonCode.UNSUPPORTED_PROTOCOL,
            lambda: model_endpoint_candidates(
                "https://api.example.test/v1",
                "future-protocol",
            ),
        )
        self.assert_rejected(
            ModelEndpointReasonCode.BASE_QUERY_FORBIDDEN,
            lambda: model_endpoint_candidates(
                "https://api.example.test/v1?tenant=fixture",
                "openai",
            ),
        )
        invalid = self.assert_rejected(
            ModelEndpointReasonCode.INVALID_BASE_URL,
            lambda: model_endpoint_candidates(
                "https://fixture:private@api.example.test/v1",
                "openai",
            ),
        )
        self.assertEqual(invalid.cause_code, "userinfo_forbidden")
        self.assertNotIn("private", str(invalid))
        self.assertNotIn("api.example.test", str(invalid))

    def test_endpoint_generation_never_resolves_or_sends_requests(self) -> None:
        with (
            mock.patch(
                "socket.getaddrinfo",
                side_effect=AssertionError("DNS access is forbidden"),
            ),
            mock.patch(
                "socket.create_connection",
                side_effect=AssertionError("network access is forbidden"),
            ),
        ):
            requests = model_endpoint_candidates(
                "https://api.example.test/v1/",
                "openai_chat",
            )

        self.assertEqual(
            tuple(request.url for request in requests),
            ("https://api.example.test/v1/models",),
        )


if __name__ == "__main__":
    unittest.main()
