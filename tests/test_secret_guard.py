from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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

    def test_allow_marker_only_exempts_the_named_generic_category(self) -> None:
        allowed_value = b"0123456789abc" + b"defghijkl"
        blocked_value = b"abcdefghijklm" + b"nopqrstuvwxyz"
        content = (
            b'API_KEY="'
            + allowed_value
            + b'"  # secret-guard: allow generic-secret-assignment\n'
            + b'API_KEY="'
            + blocked_value
            + b'"\n'
        )
        findings = secret_guard.scan_bytes("fixture.py", content, ())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 2)

    def test_private_fingerprint_requires_its_finding_id_for_an_allowance(self) -> None:
        private = secret_guard.PrivateFingerprint(
            "private-provider-name", "personal-channel-42"
        )
        unscoped = secret_guard.scan_bytes(
            "README.md",
            b"personal-channel-42  # secret-guard: allow private-provider-name",
            (private,),
        )
        scoped = secret_guard.scan_bytes(
            "README.md",
            (
                f"personal-channel-42  # secret-guard: allow private-provider-name "
                f"{private.finding_id}"
            ).encode(),
            (private,),
        )
        self.assertEqual(len(unscoped), 1)
        self.assertEqual(scoped, [])

    def test_generic_token_assignment_accepts_common_secret_punctuation(self) -> None:
        token = b"abc!def@ghi#jkl$" + b"mno%pqr"
        findings = secret_guard.scan_bytes(
            "config.py", b'TOKEN="' + token + b'"', ()
        )
        self.assertEqual(findings[0].category, "generic-secret-assignment")

    def test_placeholder_words_do_not_hide_a_long_token_value(self) -> None:
        token = b"local-token-" + b"0123456789abcdef"
        embedded_fixture_word = b"prefix-fixture-token-" + b"0123456789abcdef"
        for value in (token, embedded_fixture_word):
            with self.subTest(value=value):
                findings = secret_guard.scan_bytes(
                    "config.py", b'TOKEN="' + value + b'"', ()
                )
                self.assertEqual(
                    findings[0].category, "generic-secret-assignment"
                )

    def test_entire_fixture_value_is_still_treated_as_a_placeholder(self) -> None:
        for value in (b"fixture-local-token", b"CLAUDE_HUB_LOCAL_TOKEN"):
            with self.subTest(value=value):
                content = b'TOKEN="' + value + b'"'
                self.assertEqual(
                    secret_guard.scan_bytes("fixture.py", content, ()), []
                )

    def test_bearer_prose_is_not_treated_as_a_token(self) -> None:
        for content in (
            b"Bearer responsibilities are shared",
            b"Authorization: Bearer responsibilities are shared",
        ):
            with self.subTest(content=content):
                self.assertEqual(
                    secret_guard.scan_bytes("README.md", content, ()), []
                )

    def test_long_alphabetic_bearer_value_is_reported(self) -> None:
        token = b"abcdefgh" + b"ijklmnop"
        for prefix in (b"Bearer ", b"Authorization: Bearer "):
            with self.subTest(prefix=prefix):
                findings = secret_guard.scan_bytes(
                    "config.py", prefix + token, ()
                )
                self.assertEqual(findings[0].category, "literal-bearer-token")

    def test_secret_after_nul_byte_is_still_scanned(self) -> None:
        token = b"0123456789abc" + b"defghijkl"
        findings = secret_guard.scan_bytes(
            "config.py", b"prefix\0TOKEN=\"" + token + b'"', ()
        )
        self.assertEqual(findings[0].category, "generic-secret-assignment")

    def test_utf16_secret_assignment_is_scanned(self) -> None:
        token = "0123456789abcdefghijkl"  # secret-guard: allow generic-secret-assignment
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                findings = secret_guard.scan_bytes(
                    "config.txt", f'TOKEN="{token}"'.encode(encoding), ()
                )
                self.assertTrue(
                    any(
                        finding.category == "generic-secret-assignment"
                        for finding in findings
                    )
                )

    def test_staged_scan_includes_type_changes(self) -> None:
        calls = []

        def fake_git(*args, input_bytes=None):
            calls.append(args)
            return b"link\0" if args[0] == "diff" else b"target"

        with mock.patch.object(secret_guard, "run_git", side_effect=fake_git):
            self.assertEqual(list(secret_guard.staged_files()), [("link", b"target")])
        self.assertIn("--diff-filter=ACMRT", calls[0])

    def test_working_tree_scan_does_not_follow_symlinks_or_read_fifo(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            secret = root / "outside"
            secret.write_text(
                'TOKEN="0123456789abcdefghijkl"'  # secret-guard: allow generic-secret-assignment
            )
            (root / "link").symlink_to(secret)
            if hasattr(os, "mkfifo"):
                os.mkfifo(root / "pipe")
            names = b"link\0" + (b"pipe\0" if hasattr(os, "mkfifo") else b"")
            with mock.patch.object(secret_guard, "run_git", return_value=names), mock.patch.object(
                secret_guard, "Path", side_effect=lambda value: root / value
            ):
                files = list(secret_guard.working_tree_files())
        self.assertEqual(files, [("link", str(secret).encode())])

    def test_new_remote_and_missing_remote_object_scan_all_local_commits(self) -> None:
        local = "1" * 40
        remote = "2" * 40

        with mock.patch.object(
            secret_guard, "run_git", return_value=b"commit-a\ncommit-b\n"
        ) as run_git:
            commits = secret_guard.commits_from_pre_push(
                [f"refs/heads/main {local} refs/heads/main {secret_guard.ZERO_SHA}"]
            )
        self.assertEqual(commits, ["commit-a", "commit-b"])
        run_git.assert_called_once_with("rev-list", local)

        def missing_remote(*args, input_bytes=None):
            if args[0] == "cat-file":
                raise secret_guard.subprocess.CalledProcessError(1, args)
            return b"commit-a\n"

        with mock.patch.object(secret_guard, "run_git", side_effect=missing_remote) as run_git:
            commits = secret_guard.commits_from_pre_push(
                [f"refs/heads/main {local} refs/heads/main {remote}"]
            )
        self.assertEqual(commits, ["commit-a"])
        run_git.assert_any_call("rev-list", local)

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
        for slot in ("Fable", "Opus", "Sonnet", "Haiku"):
            with self.subTest(slot=slot):
                self.assertTrue(secret_guard.is_public_provider_label(slot))
        self.assertFalse(secret_guard.is_public_provider_label("my-company-route"))

    def test_loopback_urls_are_not_private_upstream_fingerprints(self) -> None:
        fingerprints: set[secret_guard.PrivateFingerprint] = set()
        secret_guard.walk_private_json(
            {
                "base_url": "http://127.0.0.1:18400/v1",
                "endpoint": "http://localhost:18400/v1",
                "proxy": "http://[::1]:18400",
            },
            fingerprints,
        )
        self.assertEqual(fingerprints, set())

    def test_remote_urls_remain_private_upstream_fingerprints(self) -> None:
        fingerprints: set[secret_guard.PrivateFingerprint] = set()
        secret_guard.walk_private_json(
            {"base_url": "https://fixture-upstream.example.test/v1"},
            fingerprints,
        )
        self.assertEqual(
            {fingerprint.category for fingerprint in fingerprints},
            {"private-upstream"},
        )


if __name__ == "__main__":
    unittest.main()
