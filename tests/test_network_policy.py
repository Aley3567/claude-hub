from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from claude_hub.network_policy import (  # noqa: E402
    NetworkPolicyError,
    NetworkReasonCode,
    authorize_target,
    confirm_private_target,
    normalize_provider_url,
    validate_peer,
    validate_pre_connect,
    validate_redirect,
)


class NetworkPolicyTestCase(unittest.TestCase):
    def assert_rejected(self, reason: NetworkReasonCode, callback) -> None:
        with self.assertRaises(NetworkPolicyError) as raised:
            callback()
        self.assertEqual(raised.exception.reason_code, reason.value)


class URLNormalizationTests(NetworkPolicyTestCase):
    def test_http_urls_are_canonical_and_public_display_omits_query(self) -> None:
        target = normalize_provider_url(
            "HTTPS://Example.COM.:443/proxy/v1/?cursor=fixture"
        )

        self.assertEqual(target.scheme, "https")
        self.assertEqual(target.host, "example.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.origin, "https://example.com")
        self.assertEqual(
            target.request_url,
            "https://example.com/proxy/v1/?cursor=fixture",
        )
        self.assertEqual(
            target.display_url,
            "https://example.com/proxy/v1/",
        )
        self.assertNotIn("cursor", repr(target))

    def test_ipv6_literals_keep_brackets_and_default_ports_are_removed(self) -> None:
        target = normalize_provider_url(
            "https://[2001:4860:4860:0:0:0:0:8888]:443/v1"
        )

        self.assertEqual(target.host, "2001:4860:4860::8888")
        self.assertEqual(
            target.request_url,
            "https://[2001:4860:4860::8888]/v1",
        )
        self.assertTrue(target.is_ip_literal)

    def test_dot_segments_are_removed_without_changing_origin(self) -> None:
        target = normalize_provider_url(
            "https://api.example.test/proxy/./openai/../v1/?page=fixture"
        )

        self.assertEqual(target.origin, "https://api.example.test")
        self.assertEqual(target.path, "/proxy/v1/")
        self.assertEqual(
            target.request_url,
            "https://api.example.test/proxy/v1/?page=fixture",
        )

    def test_unsafe_or_ambiguous_urls_have_stable_reason_codes(self) -> None:
        cases = (
            (
                "https://fixture-user:fixture-pass@api.example.test/v1",
                NetworkReasonCode.USERINFO_FORBIDDEN,
            ),
            (
                "https://api.example.test/v1#private",
                NetworkReasonCode.FRAGMENT_FORBIDDEN,
            ),
            (
                "ftp://api.example.test/v1",
                NetworkReasonCode.UNSUPPORTED_SCHEME,
            ),
            ("https:///v1", NetworkReasonCode.MISSING_HOST),
            (" https://api.example.test", NetworkReasonCode.INVALID_URL),
            (
                "https://api.example.test\\@elsewhere.test",
                NetworkReasonCode.INVALID_URL,
            ),
            ("https://api.example.test:0/v1", NetworkReasonCode.INVALID_URL),
            ("https://api.example.test:/v1", NetworkReasonCode.INVALID_URL),
            ("https://api.example.test../v1", NetworkReasonCode.INVALID_URL),
            (
                "https://metadata.google.internal/computeMetadata/v1",
                NetworkReasonCode.METADATA_TARGET_FORBIDDEN,
            ),
            (
                "http://169.254.169.254/latest/meta-data",
                NetworkReasonCode.METADATA_TARGET_FORBIDDEN,
            ),
            (
                "https://[fd00:ec2::254]/latest/meta-data",
                NetworkReasonCode.METADATA_TARGET_FORBIDDEN,
            ),
        )

        for raw_url, reason in cases:
            with self.subTest(raw_url=raw_url):
                self.assert_rejected(
                    reason,
                    lambda raw_url=raw_url: normalize_provider_url(raw_url),
                )

    def test_rejections_do_not_echo_userinfo_or_query(self) -> None:
        private_value = "fixture-private-value"
        with self.assertRaises(NetworkPolicyError) as raised:
            normalize_provider_url(
                f"https://fixture-user:{private_value}@api.example.test/"
            )
        self.assertNotIn(private_value, str(raised.exception))
        self.assertNotIn("api.example.test", str(raised.exception))


class TargetAuthorizationTests(NetworkPolicyTestCase):
    def test_public_https_and_loopback_http_are_authorized_and_pinned(self) -> None:
        public_plan = authorize_target(
            "https://api.example.test/v1",
            ("8.8.8.8", "1.1.1.1", "8.8.8.8"),
        )
        self.assertEqual(
            public_plan.pinned_addresses,
            ("1.1.1.1", "8.8.8.8"),
        )
        self.assertFalse(public_plan.private_network)

        loopback_plan = authorize_target(
            "http://localhost:8080/v1",
            ("::1", "127.0.0.1"),
        )
        self.assertEqual(
            loopback_plan.pinned_addresses,
            ("127.0.0.1", "::1"),
        )
        self.assertFalse(loopback_plan.private_network)

        literal_plan = authorize_target(
            "http://127.0.0.1:8080/v1",
            ("127.0.0.1",),
        )
        self.assertEqual(literal_plan.pinned_addresses, ("127.0.0.1",))
        self.assertFalse(literal_plan.private_network)

        subdomain_plan = authorize_target(
            "http://hub.localhost:8080/v1",
            ("::1", "127.0.0.1"),
        )
        self.assertFalse(subdomain_plan.private_network)

    def test_remote_hostname_loopback_results_require_private_confirmation(
        self,
    ) -> None:
        cases = (
            ("https://remote.example.test/v1", ("127.0.0.1",)),
            ("http://remote.example.test/v1", ("127.0.0.1", "::1")),
            (
                "https://mixed.example.test/v1",
                ("8.8.8.8", "127.0.0.1"),
            ),
        )

        for raw_url, addresses in cases:
            with self.subTest(raw_url=raw_url):
                self.assert_rejected(
                    NetworkReasonCode.PRIVATE_CONFIRMATION_REQUIRED,
                    lambda raw_url=raw_url, addresses=addresses: authorize_target(
                        raw_url,
                        addresses,
                    ),
                )
                confirmation = confirm_private_target(
                    raw_url,
                    addresses,
                    confirmed=True,
                )
                plan = authorize_target(
                    raw_url,
                    addresses,
                    private_confirmation=confirmation,
                )
                self.assertTrue(plan.private_network)
                self.assertEqual(
                    plan.pinned_addresses,
                    confirmation.addresses,
                )

    def test_http_is_rejected_for_every_non_loopback_target(self) -> None:
        for addresses in (("8.8.8.8",), ("10.0.0.8",), ("fe80::8",)):
            with self.subTest(addresses=addresses):
                self.assert_rejected(
                    NetworkReasonCode.CLEARTEXT_NON_LOOPBACK,
                    lambda addresses=addresses: authorize_target(
                        "http://api.example.test/v1",
                        addresses,
                    ),
                )

        self.assert_rejected(
            NetworkReasonCode.LOCALHOST_RESOLUTION_INVALID,
            lambda: authorize_target(
                "http://localhost/v1",
                ("8.8.8.8",),
            ),
        )

    def test_private_ipv4_ipv6_and_link_local_require_explicit_confirmation(
        self,
    ) -> None:
        cases = (
            ("https://private-v4.example.test/v1", ("10.0.0.8",)),
            ("https://private-v6.example.test/v1", ("fd00::8",)),
            ("https://link-local.example.test/v1", ("fe80::8",)),
        )

        for raw_url, addresses in cases:
            with self.subTest(raw_url=raw_url):
                self.assert_rejected(
                    NetworkReasonCode.PRIVATE_CONFIRMATION_REQUIRED,
                    lambda raw_url=raw_url, addresses=addresses: authorize_target(
                        raw_url,
                        addresses,
                    ),
                )
                self.assert_rejected(
                    NetworkReasonCode.PRIVATE_CONFIRMATION_REQUIRED,
                    lambda raw_url=raw_url, addresses=addresses: confirm_private_target(
                        raw_url,
                        addresses,
                        confirmed=False,
                    ),
                )
                confirmation = confirm_private_target(
                    raw_url,
                    addresses,
                    confirmed=True,
                )
                plan = authorize_target(
                    raw_url,
                    addresses,
                    private_confirmation=confirmation,
                )
                self.assertTrue(plan.private_network)
                self.assertEqual(plan.target.origin, confirmation.origin)
                self.assertEqual(
                    plan.pinned_addresses,
                    confirmation.addresses,
                )

    def test_private_confirmation_is_bound_to_origin_and_address_set(self) -> None:
        confirmation = confirm_private_target(
            "https://private.example.test/v1",
            ("10.0.0.8",),
            confirmed=True,
        )

        self.assert_rejected(
            NetworkReasonCode.PRIVATE_CONFIRMATION_ORIGIN_MISMATCH,
            lambda: authorize_target(
                "https://other-private.example.test/v1",
                ("10.0.0.8",),
                private_confirmation=confirmation,
            ),
        )
        self.assert_rejected(
            NetworkReasonCode.PRIVATE_CONFIRMATION_ADDRESS_MISMATCH,
            lambda: authorize_target(
                "https://private.example.test/v1",
                ("10.0.0.9",),
                private_confirmation=confirmation,
            ),
        )
        self.assert_rejected(
            NetworkReasonCode.PRIVATE_CONFIRMATION_NOT_APPLICABLE,
            lambda: authorize_target(
                "https://public.example.test/v1",
                ("8.8.8.8",),
                private_confirmation=confirmation,
            ),
        )

    def test_metadata_and_other_non_routable_resolution_results_fail_closed(
        self,
    ) -> None:
        for address in (
            "169.254.169.254",
            "::ffff:169.254.169.254",
            "100.100.100.200",
        ):
            with self.subTest(address=address):
                self.assert_rejected(
                    NetworkReasonCode.METADATA_TARGET_FORBIDDEN,
                    lambda address=address: authorize_target(
                        "https://api.example.test/v1",
                        (address,),
                    ),
                )

        for address in ("0.0.0.0", "::", "224.0.0.1"):
            with self.subTest(address=address):
                self.assert_rejected(
                    NetworkReasonCode.RESTRICTED_ADDRESS_FORBIDDEN,
                    lambda address=address: authorize_target(
                        "https://api.example.test/v1",
                        (address,),
                    ),
                )

    def test_resolution_must_be_nonempty_valid_and_match_ip_literals(self) -> None:
        self.assert_rejected(
            NetworkReasonCode.DNS_RESOLUTION_EMPTY,
            lambda: authorize_target("https://api.example.test", ()),
        )
        self.assert_rejected(
            NetworkReasonCode.INVALID_RESOLVED_ADDRESS,
            lambda: authorize_target(
                "https://api.example.test",
                ("not-an-address",),
            ),
        )
        self.assert_rejected(
            NetworkReasonCode.LITERAL_ADDRESS_MISMATCH,
            lambda: authorize_target("https://8.8.8.8", ("1.1.1.1",)),
        )


class ConnectionValidationTests(NetworkPolicyTestCase):
    def setUp(self) -> None:
        self.plan = authorize_target(
            "https://api.example.test/v1?cursor=fixture",
            ("8.8.8.8", "1.1.1.1"),
        )

    def test_pre_connect_resolution_and_peer_must_match_pins(self) -> None:
        validate_pre_connect(self.plan, ("1.1.1.1", "8.8.8.8"))
        validate_peer(self.plan, "8.8.8.8")

        self.assert_rejected(
            NetworkReasonCode.DNS_REBINDING_DETECTED,
            lambda: validate_pre_connect(
                self.plan,
                ("1.1.1.1", "10.0.0.8"),
            ),
        )
        self.assert_rejected(
            NetworkReasonCode.PEER_ADDRESS_MISMATCH,
            lambda: validate_peer(self.plan, "10.0.0.8"),
        )
        self.assert_rejected(
            NetworkReasonCode.INVALID_PEER_ADDRESS,
            lambda: validate_peer(self.plan, "not-an-address"),
        )

    def test_redirects_must_stay_on_the_normalized_origin(self) -> None:
        redirected = validate_redirect(
            self.plan,
            "../models?cursor=private-fixture",
        )
        self.assertEqual(
            redirected.request_url,
            "https://api.example.test/models?cursor=private-fixture",
        )
        self.assertEqual(
            redirected.display_url,
            "https://api.example.test/models",
        )
        self.assertNotIn("cursor", repr(redirected))

        same_origin = validate_redirect(
            self.plan,
            "https://API.EXAMPLE.TEST:443/v2/models",
        )
        self.assertEqual(same_origin.origin, self.plan.target.origin)

        for location in (
            "http://api.example.test/models",
            "https://other.example.test/models",
            "https://api.example.test:444/models",
        ):
            with self.subTest(location=location):
                self.assert_rejected(
                    NetworkReasonCode.CROSS_ORIGIN_REDIRECT,
                    lambda location=location: validate_redirect(
                        self.plan,
                        location,
                    ),
                )

        self.assert_rejected(
            NetworkReasonCode.USERINFO_FORBIDDEN,
            lambda: validate_redirect(
                self.plan,
                "https://fixture:private@api.example.test/models",
            ),
        )
        self.assert_rejected(
            NetworkReasonCode.INVALID_REDIRECT,
            lambda: validate_redirect(self.plan, " /v1/models"),
        )

    def test_policy_functions_do_not_resolve_or_open_network_connections(
        self,
    ) -> None:
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
            target = normalize_provider_url("https://api.example.test/v1")
            plan = authorize_target(target, ("8.8.8.8",))
            validate_pre_connect(plan, ("8.8.8.8",))
            validate_peer(plan, "8.8.8.8")
            validate_redirect(plan, "/v1/models")


if __name__ == "__main__":
    unittest.main()
