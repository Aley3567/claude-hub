#!/bin/sh

set -eu

REPO_ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd -P)
cd "$REPO_ROOT"

chmod 755 .githooks/pre-commit .githooks/pre-push scripts/secret_guard.py
git config --local core.hooksPath .githooks

configured=$(git config --local --get core.hooksPath)
if [ "$configured" != ".githooks" ]; then
  printf '%s\n' "[secret-guard] 安装失败：core.hooksPath 未正确写入。" >&2
  exit 1
fi

python3 scripts/secret_guard.py --working-tree
printf '%s\n' \
  "[secret-guard] 已启用 pre-commit 与 pre-push 双重拦截。" \
  "[secret-guard] 手动全历史检查：python3 scripts/secret_guard.py --all-history"
