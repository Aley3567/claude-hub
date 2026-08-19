from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sqlite3
import sys
import tempfile
import tomllib
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "codex-provider-once.py"

FAKE_API_KEY = "fake-key-9821"
FAKE_ACCESS_TOKEN = "fake-tok-9821"

API_KEY_CONFIG = """
model_provider = "custom"
model = "gpt-5.6-sol"
model_reasoning_effort = "high"
disable_response_storage = true

[model_providers.custom]
name = "Acme Codex"
base_url = "https://acme.example/v1"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "bearer-x1"
"""

OAUTH_CONFIG = """
model = "gpt-5.6-sol"
model_verbosity = "medium"
notify = ["helper", "turn-ended"]
model_provider = "chatgpt_http"

[model_providers.chatgpt_http]
name = "Vendor"
base_url = "https://vendor.example/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true

[tui]
theme = "dark"
"""


@contextmanager
def loaded_launcher(env: dict[str, str]):
    """Load a fresh launcher module after applying an isolated runtime env."""
    with mock.patch.dict(os.environ, env, clear=False):
        name = f"codex1_launcher_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(name, LAUNCHER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            yield module
        finally:
            sys.modules.pop(name, None)


def isolated_env(home: Path, **overrides: str) -> dict[str, str]:
    env = {
        "CODEX1_HOME": str(home),
        "CODEX1_DB_PATH": str(home / ".cc-switch" / "cc-switch.db"),
        "CODEX1_MRU_PATH": str(home / ".cc-switch" / "codex1-mru.json"),
        "CODEX1_CODEX_HOME": str(home / ".codex"),
    }
    env.update(overrides)
    return env


def write_fake_db(path: Path, rows: list[tuple]) -> None:
    """Create a minimal CC Switch style providers table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, "
            "settings_config TEXT, sort_index INTEGER, PRIMARY KEY (id, app_type))"
        )
        conn.executemany(
            "INSERT INTO providers (id, app_type, name, settings_config, sort_index) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def settings_blob(auth: dict, config: str) -> str:
    return json.dumps({"auth": auth, "config": config})


class ProfileBuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    @contextmanager
    def launcher(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            yield module

    def test_api_key_profile_uses_env_key_and_renames_section(self):
        with self.launcher() as module:
            profile = module.build_profile(
                API_KEY_CONFIG, {"OPENAI_API_KEY": FAKE_API_KEY}
            )
            parsed = tomllib.loads(profile["toml"])

        self.assertEqual(profile["kind"], "api-key")
        self.assertEqual(profile["api_key"], FAKE_API_KEY)
        self.assertEqual(parsed["model_provider"], "codex1")
        self.assertIn("codex1", parsed["model_providers"])
        self.assertNotIn("custom", parsed["model_providers"])
        section = parsed["model_providers"]["codex1"]
        self.assertEqual(section["env_key"], "CODEX1_API_KEY")
        self.assertEqual(section["base_url"], "https://acme.example/v1")
        self.assertNotIn("experimental_bearer_token", section)
        # 顶层设置原样带进 profile。
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["model_reasoning_effort"], "high")
        self.assertIs(parsed["disable_response_storage"], True)

    def test_api_key_never_appears_in_profile_text(self):
        with self.launcher() as module:
            profile = module.build_profile(
                API_KEY_CONFIG, {"OPENAI_API_KEY": FAKE_API_KEY}
            )
        self.assertNotIn(FAKE_API_KEY, profile["toml"])
        self.assertNotIn("bearer-x1", profile["toml"])
        self.assertEqual(profile["auth_payload"], {})

    def test_oauth_profile_keeps_auth_and_omits_env_key(self):
        auth = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {"access_token": FAKE_ACCESS_TOKEN, "account_id": "acc"},
        }
        with self.launcher() as module:
            profile = module.build_profile(OAUTH_CONFIG, auth)
            parsed = tomllib.loads(profile["toml"])

        self.assertEqual(profile["kind"], "chatgpt")
        self.assertIsNone(profile["api_key"])
        self.assertEqual(profile["auth_payload"], auth)
        section = parsed["model_providers"]["codex1"]
        self.assertNotIn("env_key", section)
        self.assertEqual(parsed["notify"], ["helper", "turn-ended"])
        # 嵌套表交给基础 config.toml,并在返回值里点名。
        self.assertNotIn("tui", parsed)
        self.assertEqual(profile["dropped_keys"], ["tui"])

    def test_missing_provider_section_fails_fast(self):
        config = 'model_provider = "custom"\n\n[model_providers.other]\nbase_url = "x"\n'
        with self.launcher() as module:
            with self.assertRaises(RuntimeError) as caught:
                module.build_profile(config, {"OPENAI_API_KEY": FAKE_API_KEY})
        self.assertIn("model_providers.custom", str(caught.exception))

    def test_missing_model_provider_fails_fast(self):
        with self.launcher() as module:
            with self.assertRaises(RuntimeError):
                module.build_profile('model = "x"\n', {"OPENAI_API_KEY": FAKE_API_KEY})

    def test_api_key_channel_without_key_fails_fast(self):
        with self.launcher() as module:
            with self.assertRaises(RuntimeError):
                module.build_profile(API_KEY_CONFIG, {})


class TomlWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    @contextmanager
    def launcher(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            yield module

    def test_scalar_rendering(self):
        with self.launcher() as module:
            self.assertEqual(module.toml_value(True), "true")
            self.assertEqual(module.toml_value(False), "false")
            self.assertEqual(module.toml_value(7), "7")
            self.assertEqual(module.toml_value("a"), '"a"')
            self.assertEqual(module.toml_value(["a", "b"]), '["a", "b"]')

    def test_string_escaping(self):
        with self.launcher() as module:
            rendered = module.toml_value('back\\slash "quoted"\nline\ttab')
            round_trip = tomllib.loads(f"k = {rendered}")
        self.assertEqual(round_trip["k"], 'back\\slash "quoted"\nline\ttab')

    def test_render_toml_round_trip(self):
        with self.launcher() as module:
            text = module.render_toml(
                {"model_provider": "codex1", "flag": False, "count": 3},
                {"model_providers.codex1": {"base_url": "https://x/v1"}},
            )
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["model_provider"], "codex1")
        self.assertIs(parsed["flag"], False)
        self.assertEqual(parsed["count"], 3)
        self.assertEqual(parsed["model_providers"]["codex1"]["base_url"], "https://x/v1")

    def test_unsupported_type_rejected(self):
        with self.launcher() as module:
            with self.assertRaises(TypeError):
                module.toml_value({"nested": 1})
            self.assertFalse(module.toml_supported({"nested": 1}))


class MatchProvidersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.providers = [
            {"id": "aaa11111", "name": "Acme Codex"},
            {"id": "bbb22222", "name": "Nimbus"},
            {"id": "ccc33333", "name": "Nimbus"},
        ]

    @contextmanager
    def launcher(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            yield module

    def test_exact_name_wins_over_substring(self):
        providers = [
            {"id": "1", "name": "sota"},
            {"id": "2", "name": "nimbus"},
        ]
        with self.launcher() as module:
            matches, exact = module.match_providers(providers, "sota")
        self.assertTrue(exact)
        self.assertEqual([p["id"] for p in matches], ["1"])

    def test_substring_fallback(self):
        with self.launcher() as module:
            matches, exact = module.match_providers(self.providers, "acme")
        self.assertFalse(exact)
        self.assertEqual([p["id"] for p in matches], ["aaa11111"])

    def test_id_selector_is_exact_only(self):
        with self.launcher() as module:
            matches, exact = module.match_providers(self.providers, "id:bbb22222")
            fuzzy, _ = module.match_providers(self.providers, "bbb")
        self.assertTrue(exact)
        self.assertEqual([p["id"] for p in matches], ["bbb22222"])
        self.assertEqual(fuzzy, [])

    def test_duplicate_names_conflict(self):
        with self.launcher() as module:
            matches, exact = module.match_providers(self.providers, "Nimbus")
            with self.assertRaises(RuntimeError) as caught:
                module.choose(self.providers, "Nimbus")
        self.assertTrue(exact)
        self.assertEqual(len(matches), 2)
        self.assertIn("bbb22222", str(caught.exception))

    def test_unknown_hint_fails(self):
        with self.launcher() as module:
            with self.assertRaises(RuntimeError):
                module.choose(self.providers, "nope")


class ShadowHomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.real = self.home / ".codex"
        (self.real / "sessions").mkdir(parents=True)
        (self.real / "config.toml").write_text('model = "base"\n', encoding="utf-8")
        (self.real / "auth.json").write_text('{"OPENAI_API_KEY": "real"}', encoding="utf-8")
        (self.real / ".secret-state").write_text("x", encoding="utf-8")

    @contextmanager
    def launcher(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            yield module

    def test_shadow_layout_and_permissions(self):
        with self.launcher() as module:
            shadow = module.build_shadow_home(self.real, 'model_provider = "codex1"\n', "{}")
            try:
                self.assertTrue((shadow / "config.toml").is_symlink())
                self.assertEqual(
                    (shadow / "config.toml").resolve(), (self.real / "config.toml").resolve()
                )
                self.assertTrue((shadow / "sessions").is_symlink())
                # 点开头的条目也要跟过去。
                self.assertTrue((shadow / ".secret-state").is_symlink())

                profile = shadow / "codex1.config.toml"
                self.assertFalse(profile.is_symlink())
                self.assertEqual(stat.S_IMODE(profile.lstat().st_mode), 0o600)
                self.assertEqual(profile.read_text(), 'model_provider = "codex1"\n')

                auth = shadow / "auth.json"
                self.assertFalse(auth.is_symlink())
                self.assertEqual(stat.S_IMODE(auth.lstat().st_mode), 0o600)
                self.assertEqual(auth.read_text(), "{}")
                self.assertEqual(stat.S_IMODE(shadow.lstat().st_mode), 0o700)

                # 真实文件全程不被改写。
                self.assertEqual(
                    (self.real / "auth.json").read_text(), '{"OPENAI_API_KEY": "real"}'
                )
                self.assertEqual((self.real / "config.toml").read_text(), 'model = "base"\n')
            finally:
                module.shutil.rmtree(shadow, ignore_errors=True)

    def test_missing_real_home_fails_fast(self):
        with self.launcher() as module:
            with self.assertRaises(RuntimeError):
                module.build_shadow_home(self.home / "nope", "x = 1\n", "{}")


class ProviderListingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.db = self.home / ".cc-switch" / "cc-switch.db"
        write_fake_db(
            self.db,
            [
                (
                    "id-oauth",
                    "codex",
                    "Vendor Official",
                    settings_blob(
                        {"auth_mode": "chatgpt", "tokens": {"access_token": FAKE_ACCESS_TOKEN}},
                        OAUTH_CONFIG,
                    ),
                    0,
                ),
                (
                    "id-acme",
                    "codex",
                    "Acme Codex",
                    settings_blob({"OPENAI_API_KEY": FAKE_API_KEY}, API_KEY_CONFIG),
                    None,
                ),
                ("id-claude", "claude", "Claude Only", settings_blob({}, ""), 0),
            ],
        )

    def test_list_providers_filters_and_orders(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            providers = module.list_providers()
        self.assertEqual([p["id"] for p in providers], ["id-oauth", "id-acme"])

    def test_order_by_mru_promotes_recent(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            providers = module.list_providers()
            ordered = module.order_by_mru(providers, {"id-acme": 100.0})
        self.assertEqual([p["id"] for p in ordered], ["id-acme", "id-oauth"])

    def test_record_use_writes_private_file_without_credentials(self):
        env = isolated_env(self.home)
        with loaded_launcher(env) as module:
            module.record_use("id-acme")
            mru_path = Path(env["CODEX1_MRU_PATH"])
            content = mru_path.read_text()
        self.assertEqual(stat.S_IMODE(mru_path.lstat().st_mode), 0o600)
        self.assertIn("id-acme", content)
        self.assertNotIn(FAKE_API_KEY, content)

    def test_list_output_has_no_credentials(self):
        env = dict(os.environ)
        env.update(isolated_env(self.home))
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), "--list"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        combined = result.stdout + result.stderr
        self.assertIn("Acme Codex", combined)
        self.assertIn("[chatgpt]", combined)
        self.assertNotIn(FAKE_API_KEY, combined)
        self.assertNotIn(FAKE_ACCESS_TOKEN, combined)


class ArgumentSplitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.providers = [{"id": "id-acme", "name": "Acme Codex"}]

    def test_hint_is_consumed_and_rest_passes_through(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            hint, rest = module.split_args(["acme", "exec", "hi"], self.providers)
        self.assertEqual(hint, "acme")
        self.assertEqual(rest, ["exec", "hi"])

    def test_unmatched_positional_is_passed_through(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            hint, rest = module.split_args(["exec", "hi"], self.providers)
        self.assertIsNone(hint)
        self.assertEqual(rest, ["exec", "hi"])

    def test_flags_are_never_treated_as_hints(self):
        with loaded_launcher(isolated_env(self.home)) as module:
            hint, rest = module.split_args(["--full-auto"], self.providers)
        self.assertIsNone(hint)
        self.assertEqual(rest, ["--full-auto"])


if __name__ == "__main__":
    unittest.main()
