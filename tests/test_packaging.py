from __future__ import annotations

import io
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from claude_hub import __version__  # noqa: E402
from claude_hub.entrypoints import claude1_main, hub_main  # noqa: E402


class PackagingMetadataTests(unittest.TestCase):
    def test_project_and_console_script_contract(self) -> None:
        config = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["project"]["name"], "claude-hub-kit")
        self.assertEqual(config["project"]["version"], __version__)
        self.assertEqual(config["project"]["requires-python"], ">=3.11")
        self.assertEqual(
            config["project"]["scripts"],
            {
                "claude-hub": "claude_hub.entrypoints:hub_main",
                "claude1": "claude_hub.entrypoints:claude1_main",
                "switchctl": "claude_hub.switchctl:main",
            },
        )

    @unittest.skipUnless(
        importlib.util.find_spec("setuptools"),
        "setuptools is not installed in this test interpreter",
    )
    def test_wheel_contains_only_package_and_distribution_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            temp = Path(raw_temp)
            project = temp / "project"
            project.mkdir()
            shutil.copy2(ROOT / "pyproject.toml", project / "pyproject.toml")
            shutil.copy2(ROOT / "README.md", project / "README.md")
            shutil.copytree(SRC, project / "src")
            wheel_dir = temp / "wheelhouse"

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheel_dir),
                    str(project),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=completed.stdout + completed.stderr,
            )
            wheels = list(wheel_dir.glob("claude_hub_kit-*.whl"))
            self.assertEqual(len(wheels), 1)

            with zipfile.ZipFile(wheels[0]) as archive:
                members = archive.namelist()

            self.assertIn("claude_hub/__init__.py", members)
            self.assertIn("claude_hub/entrypoints.py", members)
            self.assertTrue(
                all(
                    name.startswith("claude_hub/")
                    or ".dist-info/" in name
                    for name in members
                )
            )
            self.assertFalse(
                any(
                    "tests/" in name
                    or "__pycache__" in name
                    or name.endswith((".pyc", ".pyo"))
                    or ".cc-switch" in name
                    for name in members
                )
            )


class PlaceholderEntrypointTests(unittest.TestCase):
    def test_help_is_clear_and_successful(self) -> None:
        for program, entrypoint in (
            ("claude-hub", hub_main),
            ("claude1", claude1_main),
        ):
            with self.subTest(program=program):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = entrypoint(["--help"])

                self.assertEqual(status, 0)
                normalized = " ".join(output.getvalue().split())
                self.assertIn(f"usage: {program}", normalized)
                self.assertIn(
                    "operational behavior is not packaged yet",
                    normalized,
                )

    def test_unimplemented_operation_returns_placeholder_error(self) -> None:
        for program, entrypoint in (
            ("claude-hub", hub_main),
            ("claude1", claude1_main),
        ):
            for argv in ([], ["serve"]):
                with self.subTest(program=program, argv=argv):
                    output = io.StringIO()
                    with redirect_stderr(output):
                        status = entrypoint(argv)

                    self.assertEqual(status, 2)
                    self.assertIn(
                        "packaged operational behavior is not implemented yet",
                        output.getvalue(),
                    )


if __name__ == "__main__":
    unittest.main()
