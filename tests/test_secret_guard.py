from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "secret_guard.py"
SPEC = importlib.util.spec_from_file_location("secret_guard", MODULE_PATH)
assert SPEC and SPEC.loader
secret_guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = secret_guard
SPEC.loader.exec_module(secret_guard)


class SecretGuardTests(unittest.TestCase):
    def test_generic_token_is_reported_without_secret_value(self) -> None:
        token = "ghp_" + "A" * 30
        findings = secret_guard.scan_bytes("config.py", token.encode(), ())
        self.assertEqual(findings[0].category, "github-token")
        self.assertNotIn(token, findings[0].render())

    def test_private_fingerprint_is_redacted_and_located(self) -> None:
        private = secret_guard.PrivateFingerprint(
            "private-provider-name", "personal-channel-42"
        )
        findings = secret_guard.scan_bytes(
            "README.md",
            b"use personal-channel-42 here",
            (private,),
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 1)
        self.assertNotIn(private.value, findings[0].render())
        self.assertIn(private.finding_id, findings[0].render())

    def test_allow_marker_only_exempts_the_marked_line(self) -> None:
        content = (
            b'API_KEY="***REMOVED***"  # secret-guard: allow\n'
            b'API_KEY="***REMOVED***"\n'  # secret-guard: allow
        )
        findings = secret_guard.scan_bytes("fixture.py", content, ())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 2)

    def test_sensitive_local_files_are_blocked_even_without_content(self) -> None:
        findings = secret_guard.scan_bytes(".env.local", b"", ())
        self.assertEqual(findings[0].category, "private-config-path")
        screenshot = secret_guard.scan_bytes(
            "docs/design-references/local-private-screen.png",
            b"\x89PNG\r\n",
            (),
        )
        self.assertEqual(screenshot[0].category, "local-private-artifact")

    def test_examples_and_placeholders_are_not_reported(self) -> None:
        content = b'API_KEY="your_api_key_here"\n'
        self.assertEqual(secret_guard.scan_bytes(".env.example", content, ()), [])

    def test_public_vendor_labels_are_not_private_fingerprints(self) -> None:
        self.assertTrue(secret_guard.is_public_provider_label("Claude-Hub"))
        self.assertTrue(secret_guard.is_public_provider_label("OpenAI"))
        self.assertFalse(secret_guard.is_public_provider_label("my-company-route"))


if __name__ == "__main__":
    unittest.main()
