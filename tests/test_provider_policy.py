from __future__ import annotations

import unittest

import claude1_provider as policy


class CapabilityProfileTests(unittest.TestCase):
    def test_safe_defaults_are_unknown_or_disabled_and_auditable(self) -> None:
        profile = policy.resolve_capability_profile()

        self.assertEqual(profile.get("protocol"), "anthropic")
        self.assertEqual(profile.get("tool_search"), "unsupported")
        self.assertEqual(profile.get("count_tokens"), "estimated")
        self.assertIsNone(profile.get("context_window"))
        self.assertEqual(profile.get("thinking"), "unsupported")
        self.assertEqual(profile.get("beta_policy"), "filtered")
        self.assertEqual(
            profile.get("background_worker_safe"),
            "unverified",
        )
        self.assertEqual(profile.source("context_window"), "safe-default")
        self.assertEqual(profile.status("context_window"), "unverified")

    def test_override_precedence_and_verification_are_per_field(self) -> None:
        profile = policy.resolve_capability_profile(
            meta={
                "claude1Capabilities": {
                    "tool_search": "probe",
                    "context_window": 200000,
                }
            },
            settings={
                "api_format": "openai_chat",
                "claude1": {
                    "capabilities": {
                        "tool_search": "supported",
                        "thinking": "supported",
                    }
                },
            },
            override={
                "tool_search": {
                    "value": "unsupported",
                    "status": "verified",
                },
                "context_window": 128000,
                "sources": {"context_window": "recent-verification"},
                "verification": {"context_window": "verified"},
            },
        )

        self.assertEqual(profile.get("protocol"), "openai_chat")
        self.assertEqual(profile.get("tool_search"), "unsupported")
        self.assertEqual(profile.source("tool_search"), "user-config")
        self.assertEqual(profile.status("tool_search"), "verified")
        self.assertEqual(profile.get("thinking"), "supported")
        self.assertEqual(profile.source("thinking"), "provider-settings")
        self.assertEqual(profile.get("context_window"), 128000)
        self.assertEqual(
            profile.source("context_window"),
            "recent-verification",
        )
        self.assertEqual(profile.status("context_window"), "verified")

    def test_invalid_cross_protocol_exact_count_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            policy.ProviderPolicyError,
            "only valid for anthropic",
        ):
            policy.resolve_capability_profile(
                override={
                    "protocol": "openai_responses",
                    "count_tokens": "exact",
                }
            )

    def test_beta_mapping_requires_an_explicit_map(self) -> None:
        with self.assertRaisesRegex(policy.ProviderPolicyError, "beta_map"):
            policy.resolve_capability_profile(
                override={"beta_policy": "mapped"}
            )

    def test_model_capabilities_use_exact_opaque_ids(self) -> None:
        profile = policy.resolve_capability_profile(
            override={
                "context_window": "unknown",
                "thinking": "unsupported",
                "models": {
                    "opaque/model:v2": {
                        "context_window": {
                            "value": 256000,
                            "source": "recent-verification",
                            "status": "verified",
                        },
                        "thinking": "supported",
                    }
                },
            }
        )

        exact = profile.for_model("opaque/model:v2")
        other = profile.for_model("OPAQUE/model:v2")
        self.assertEqual(exact.get("context_window"), 256000)
        self.assertEqual(
            exact.source("context_window"),
            "recent-verification",
        )
        self.assertEqual(exact.status("context_window"), "verified")
        self.assertEqual(exact.get("thinking"), "supported")
        self.assertIsNone(other.get("context_window"))


class ProviderIsolationTests(unittest.TestCase):
    def test_custom_base_url_without_provider_credential_fails_closed(self) -> None:
        profile = policy.resolve_capability_profile()
        for env in (
            {"ANTHROPIC_BASE_URL": "https://third-party.invalid"},
            {
                "ANTHROPIC_BASE_URL": "https://third-party.invalid",
                "CLAUDE_CODE_OAUTH_TOKEN": "ambient-oauth-must-not-be-used",  # secret-guard: allow
            },
        ):
            with self.subTest(env=env):
                with self.assertRaisesRegex(
                    policy.ProviderPolicyError,
                    "refusing official credential fallback",
                ):
                    policy.prepare_provider_settings(
                        {"env": env},
                        profile,
                    )

    def test_prepared_session_keeps_only_explicit_route_auth_and_gates_features(self) -> None:
        profile = policy.resolve_capability_profile(
            override={
                "tool_search": "supported",
                "context_window": 128000,
                "thinking": "unsupported",
                "beta_policy": "filtered",
            }
        )
        original = {
            "forceLoginMethod": "claudeai",
            "env": {
                "ANTHROPIC_BASE_URL": "https://third-party.invalid/v1",
                "ANTHROPIC_AUTH_TOKEN": "fixture-provider-token",  # secret-guard: allow
                "CLAUDE_CODE_OAUTH_TOKEN": "ambient-oauth-must-not-survive",  # secret-guard: allow
                "ANTHROPIC_BETAS": "unsupported-beta",
                "OPENAI_API_KEY": "unrelated-credential-must-not-survive",  # secret-guard: allow
            },
        }

        prepared = policy.prepare_provider_settings(original, profile)

        self.assertNotIn("forceLoginMethod", prepared)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", prepared["env"])
        self.assertNotIn("OPENAI_API_KEY", prepared["env"])
        self.assertNotIn("ANTHROPIC_BETAS", prepared["env"])
        self.assertEqual(
            prepared["env"]["ANTHROPIC_AUTH_TOKEN"],
            "fixture-provider-token",
        )
        self.assertEqual(prepared["env"]["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(
            prepared["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"],
            "1",
        )
        self.assertEqual(
            prepared["env"]["CLAUDE_CODE_DISABLE_1M_CONTEXT"],
            "1",
        )
        self.assertEqual(prepared["env"]["MAX_THINKING_TOKENS"], "0")
        self.assertEqual(original["forceLoginMethod"], "claudeai")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", original["env"])

    def test_url_userinfo_is_rejected_before_launch(self) -> None:
        with self.assertRaisesRegex(
            policy.ProviderPolicyError,
            "base URL is invalid",
        ):
            policy.prepare_provider_settings(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": (
                            "https://placeholder:placeholder@third-party.invalid"  # secret-guard: allow
                        ),
                        "ANTHROPIC_API_KEY": "fixture-provider-key",
                    }
                },
                policy.resolve_capability_profile(),
            )

    def test_ambiguous_credentials_and_unsafe_workers_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            policy.ProviderPolicyError,
            "two different credentials",
        ):
            policy.prepare_provider_settings(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://third-party.invalid",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-auth",
                        "ANTHROPIC_API_KEY": "fixture-key",
                    }
                },
                policy.resolve_capability_profile(),
            )
        with self.assertRaisesRegex(
            policy.ProviderPolicyError,
            "background workers unsafe",
        ):
            policy.prepare_provider_settings(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://third-party.invalid",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-auth",
                    }
                },
                policy.resolve_capability_profile(
                    override={"background_worker_safe": "unsafe"}
                ),
            )

    def test_unknown_context_rejects_direct_1m_model(self) -> None:
        with self.assertRaisesRegex(
            policy.ProviderPolicyError,
            "context window is unknown",
        ):
            policy.prepare_provider_settings(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://third-party.invalid",
                        "ANTHROPIC_AUTH_TOKEN": "fixture-auth",  # secret-guard: allow
                        "ANTHROPIC_MODEL": "opaque-model[1m]",
                    }
                },
                policy.resolve_capability_profile(),
            )


if __name__ == "__main__":
    unittest.main()
