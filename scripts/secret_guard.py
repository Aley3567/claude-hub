#!/usr/bin/env python3
"""Fail closed when a Git change contains credentials or private channel data.

The guard has two complementary data sources:

1. Generic credential patterns and sensitive file names.
2. Private fingerprints loaded at runtime from environment variables,
   CC Switch's read-only database, and the local claude-hub configuration.

Findings never print the matched value. They only identify the category,
location, and a short one-way finding id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


ALLOW_MARKER = "secret-guard: allow"
ZERO_SHA = "0" * 40
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|bearer|credential|"
    r"password|passwd|private[_-]?key|secret|session[_-]?token)"
)
PLACEHOLDER_RE = re.compile(
    r"(?i)(example|fixture|fake|dummy|sample|placeholder|changeme|redacted|"
    r"xxxx+|your[_-]|test[_-]?(?:key|token|secret)|\$\{|<[^>]+>)"
)
PUBLIC_PROVIDER_LABELS = {
    "anthropic",
    "anyrouter",
    "claude",
    "claude1",
    "claudehub",
    "codex",
    "deepseek",
    "direct",
    "glm",
    "gpt",
    "grok",
    "hub",
    "kimi",
    "mimo",
    "openai",
}
GENERIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "anthropic-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    ),
    (
        "openai-key",
        re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "github-token",
        re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "google-api-key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    ),
    (
        "slack-token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    ),
    (
        "embedded-url-credential",
        re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    ),
    (
        "literal-bearer-token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b"),
    ),
    (
        "generic-secret-assignment",
        re.compile(
            r"""(?ix)
            ["']?
            (?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|
               private[_-]?key|secret|session[_-]?token)
            ["']?
            \s*(?:=|:)\s*
            ["']
            ([A-Za-z0-9._~+/=-]{16,})
            ["']
            """
        ),
    ),
)


@dataclass(frozen=True)
class PrivateFingerprint:
    category: str
    value: str

    @property
    def finding_id(self) -> str:
        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True)
class Finding:
    category: str
    path: str
    line: int
    finding_id: str = ""

    def render(self) -> str:
        suffix = f"#{self.finding_id}" if self.finding_id else ""
        return f"[{self.category}{suffix}] {self.path}:{self.line}"


def run_git(*args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        input=input_bytes,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def is_placeholder(value: str) -> bool:
    return not value.strip() or bool(PLACEHOLDER_RE.search(value))


def add_fingerprint(
    output: set[PrivateFingerprint],
    category: str,
    value: object,
    *,
    minimum_length: int = 8,
) -> None:
    if not isinstance(value, str):
        return
    normalized = value.strip()
    if len(normalized) < minimum_length or is_placeholder(normalized):
        return
    output.add(PrivateFingerprint(category, normalized))


def is_public_provider_label(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return normalized in PUBLIC_PROVIDER_LABELS


def walk_private_json(
    value: object,
    output: set[PrivateFingerprint],
    *,
    parent_key: str = "",
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if isinstance(child, str):
                if SENSITIVE_KEY_RE.search(key_text):
                    add_fingerprint(output, "private-credential", child)
                elif re.search(r"(?i)(?:base[_-]?url|endpoint|proxy|website[_-]?url)", key_text):
                    add_fingerprint(output, "private-upstream", child)
                elif key_text == "provider":
                    if not is_public_provider_label(child):
                        add_fingerprint(
                            output,
                            "private-provider-name",
                            child,
                            minimum_length=4,
                        )
            walk_private_json(child, output, parent_key=key_text)
    elif isinstance(value, list):
        for child in value:
            walk_private_json(child, output, parent_key=parent_key)


def load_private_fingerprints() -> set[PrivateFingerprint]:
    output: set[PrivateFingerprint] = set()

    for name, value in os.environ.items():
        if SENSITIVE_KEY_RE.search(name):
            add_fingerprint(output, "private-environment-value", value)

    cc_switch_dir = Path.home() / ".cc-switch"
    db_path = cc_switch_dir / "cc-switch.db"
    if db_path.is_file():
        try:
            uri = db_path.resolve(strict=True).as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            try:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(providers)"
                    ).fetchall()
                }
                selected = [
                    column
                    for column in (
                        "name",
                        "settings_config",
                        "meta",
                        "website_url",
                        "notes",
                    )
                    if column in columns
                ]
                if selected:
                    rows = connection.execute(
                        f"SELECT {', '.join(selected)} FROM providers "
                        "WHERE app_type='claude'"
                    ).fetchall()
                    for row in rows:
                        provider = dict(zip(selected, row))
                        provider_name = provider.get("name")
                        if not is_public_provider_label(provider_name):
                            add_fingerprint(
                                output,
                                "private-provider-name",
                                provider_name,
                                minimum_length=4,
                            )
                        for key in ("settings_config", "meta"):
                            raw = provider.get(key)
                            if not isinstance(raw, str):
                                continue
                            try:
                                walk_private_json(json.loads(raw), output)
                            except (json.JSONDecodeError, UnicodeError):
                                pass
                        for key in ("website_url", "notes"):
                            add_fingerprint(
                                output,
                                "private-provider-metadata",
                                provider.get(key),
                            )
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            print(
                "[secret-guard] 警告：无法只读加载 CC Switch 私密指纹；"
                "通用凭证检测仍会继续。",
                file=sys.stderr,
            )

    hub_config = cc_switch_dir / "claude-hub.json"
    if hub_config.is_file():
        try:
            raw = json.loads(hub_config.read_text(encoding="utf-8"))
            walk_private_json(raw, output)
            channels = raw.get("channels") if isinstance(raw, dict) else None
            if isinstance(channels, dict):
                for alias in channels:
                    if not is_public_provider_label(alias):
                        add_fingerprint(
                            output,
                            "private-channel-alias",
                            alias,
                            minimum_length=4,
                        )
        except (OSError, UnicodeError, json.JSONDecodeError):
            print(
                "[secret-guard] 警告：无法加载本机 claude-hub 配置私密指纹；"
                "其他检测仍会继续。",
                file=sys.stderr,
            )
    return output


def sensitive_path(path: str) -> str | None:
    pure = PurePosixPath(path)
    lowered = [part.casefold() for part in pure.parts]
    basename = pure.name.casefold()
    if ".cc-switch" in lowered:
        return "private-config-path"
    if basename.startswith("local-private-"):
        return "local-private-artifact"
    if basename in {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        "cc-switch.db",
        "claude-hub.json",
        "claude-hub-token",
        "claude1-config.json",
        "claude1-mru.json",
    }:
        return "private-config-path"
    if basename.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".p12", ".pfx")):
        return "sensitive-file-type"
    return None


def scan_bytes(
    path: str,
    content: bytes,
    fingerprints: Iterable[PrivateFingerprint],
) -> list[Finding]:
    path_category = sensitive_path(path)
    findings = [Finding(path_category, path, 1)] if path_category else []
    if b"\0" in content[:8192]:
        return findings
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return findings

    for line_number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for category, pattern in GENERIC_PATTERNS:
            for match in pattern.finditer(line):
                candidate = match.group(match.lastindex or 0)
                if not is_placeholder(candidate):
                    findings.append(Finding(category, path, line_number))
                    break
        for fingerprint in fingerprints:
            if fingerprint.value in line:
                findings.append(
                    Finding(
                        fingerprint.category,
                        path,
                        line_number,
                        fingerprint.finding_id,
                    )
                )
    return findings


def decode_paths(raw: bytes) -> list[str]:
    return [
        item.decode("utf-8", "surrogateescape")
        for item in raw.split(b"\0")
        if item
    ]


def staged_files() -> Iterable[tuple[str, bytes]]:
    paths = decode_paths(
        run_git(
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    )
    for path in paths:
        try:
            yield path, run_git("show", f":{path}")
        except subprocess.CalledProcessError:
            continue


def working_tree_files() -> Iterable[tuple[str, bytes]]:
    for path in decode_paths(run_git("ls-files", "-co", "--exclude-standard", "-z")):
        try:
            yield path, Path(path).read_bytes()
        except (OSError, ValueError):
            continue


def commits_from_pre_push(lines: Iterable[str]) -> list[str]:
    commits: set[str] = set()
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = fields
        if local_sha == ZERO_SHA:
            continue
        if remote_sha == ZERO_SHA:
            args = ("rev-list", local_sha, "--not", "--remotes")
        else:
            args = ("rev-list", f"{remote_sha}..{local_sha}")
        commits.update(run_git(*args).decode().splitlines())
    return sorted(commits)


def all_history_commits() -> list[str]:
    return run_git("rev-list", "--all").decode().splitlines()


def history_files(commits: Iterable[str]) -> Iterable[tuple[str, bytes]]:
    seen_blobs: set[str] = set()
    for commit in commits:
        entries = decode_paths(run_git("ls-tree", "-r", "-z", commit))
        for entry in entries:
            metadata, path = entry.split("\t", 1)
            _mode, object_type, object_sha = metadata.split()
            if object_type != "blob" or object_sha in seen_blobs:
                continue
            seen_blobs.add(object_sha)
            try:
                yield f"{path}@{commit[:10]}", run_git("cat-file", "blob", object_sha)
            except subprocess.CalledProcessError:
                continue


def filter_labels_already_in_remote(
    fingerprints: set[PrivateFingerprint],
) -> set[PrivateFingerprint]:
    """Do not repeatedly flag non-secret labels that are already public.

    Credentials remain findings even if a remote ref already contains them.
    This exception is limited to provider labels, aliases, URLs, and notes so
    the guard can protect newly added local channels without becoming unusable
    on a repository that already names public providers.
    """

    public_categories = {
        "private-provider-name",
        "private-channel-alias",
        "private-upstream",
        "private-provider-metadata",
    }
    candidates = {
        item for item in fingerprints if item.category in public_categories
    }
    if not candidates:
        return fingerprints
    try:
        commits = run_git("rev-list", "--remotes=origin").decode().splitlines()
        if not commits:
            return fingerprints
        remote_blobs = [
            content for _path, content in history_files(commits) if b"\0" not in content[:8192]
        ]
    except subprocess.CalledProcessError:
        return fingerprints

    already_public = {
        item
        for item in candidates
        if any(item.value.encode("utf-8") in content for content in remote_blobs)
    }
    return fingerprints - already_public


def scan_files(
    files: Iterable[tuple[str, bytes]],
    fingerprints: Iterable[PrivateFingerprint],
) -> list[Finding]:
    findings: set[Finding] = set()
    for path, content in files:
        findings.update(scan_bytes(path, content, fingerprints))
    return sorted(findings, key=lambda item: (item.path, item.line, item.category))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="阻止凭证、本机渠道和私有配置进入 Git。"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="扫描暂存区")
    mode.add_argument("--pre-push", action="store_true", help="扫描即将推送的提交")
    mode.add_argument("--working-tree", action="store_true", help="扫描当前工作树")
    mode.add_argument("--all-history", action="store_true", help="扫描全部 Git 历史")
    parser.add_argument(
        "--no-private-sources",
        action="store_true",
        help="不读取本机环境、CC Switch DB 和 Hub 配置（适合 CI）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fingerprints: set[PrivateFingerprint] = set()
    if not args.no_private_sources:
        fingerprints = filter_labels_already_in_remote(
            load_private_fingerprints()
        )

    try:
        if args.staged:
            files = staged_files()
            label = "暂存区"
        elif args.working_tree:
            files = working_tree_files()
            label = "工作树"
        elif args.all_history:
            files = history_files(all_history_commits())
            label = "Git 历史"
        else:
            commits = commits_from_pre_push(sys.stdin)
            files = history_files(commits)
            label = "待推送提交"
        findings = scan_files(files, fingerprints)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", "replace").strip()
        print(f"[secret-guard] Git 检测失败：{message}", file=sys.stderr)
        return 2

    if not findings:
        print(
            f"[secret-guard] 通过：{label}未发现凭证、私有渠道或敏感配置。"
        )
        return 0

    print(
        f"[secret-guard] 已拦截：{label}发现 {len(findings)} 处潜在泄漏。",
        file=sys.stderr,
    )
    for finding in findings:
        print(f"  {finding.render()}", file=sys.stderr)
    print(
        "[secret-guard] 未显示任何秘密值。请删除/改为占位符后重新检测；"
        f"确认是假阳性时，仅在该行加入注释 `{ALLOW_MARKER}`。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
