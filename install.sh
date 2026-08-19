#!/bin/sh

set -eu

PROGRAM="claude1"
MANAGED_MARKER="# claude1 managed source"
STICKY_MARKER="# claude1 managed sticky source"
ENABLE_STICKY=0
DISABLE_STICKY=0

usage() {
  cat <<'EOF'
用法: ./install.sh [--enable-sticky|--disable-sticky]

安装 claude1 到当前用户目录，并把安全的 zsh 集成接入 ~/.zshrc。

可用于隔离安装的环境变量：
  HOME                    用户目录
  CLAUDE1_INSTALL_ROOT    安装目录（默认: $HOME/.claude）
  CODEX1_INSTALL_ROOT     codex1 安装目录（默认: $HOME/.codex）

安装器不会读取或复制 CC Switch 配置、数据库内容或任何凭证，也不会启用
zsh-sticky-integration.sh，除非显式传入 --enable-sticky。

--disable-sticky 会移除安装器管理的粘性路由 source 行；不会改动用户自行
添加的 shell 配置。
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    --enable-sticky)
      ENABLE_STICKY=1
      ;;
    --disable-sticky)
      DISABLE_STICKY=1
      ;;
    *)
      printf '%s\n' "[claude1] 未知参数: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "$ENABLE_STICKY" -eq 1 ] && [ "$DISABLE_STICKY" -eq 1 ]; then
  printf '%s\n' "[claude1] --enable-sticky 与 --disable-sticky 不能同时使用。" >&2
  exit 2
fi

if [ -z "${HOME:-}" ]; then
  printf '%s\n' "[claude1] 安装失败：HOME 未设置。" >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd -P)
INSTALL_ROOT=${CLAUDE1_INSTALL_ROOT:-"$HOME/.claude"}
CODEX_INSTALL_ROOT=${CODEX1_INSTALL_ROOT:-"$HOME/.codex"}
CC_SWITCH_DB="$HOME/.cc-switch/cc-switch.db"
ZSHRC="$HOME/.zshrc"
ZSHRC_TARGET="$ZSHRC"
MANAGE_STICKY=$ENABLE_STICKY
if [ "$DISABLE_STICKY" -eq 0 ] && [ -f "$ZSHRC" ] && grep -Fq "$STICKY_MARKER" "$ZSHRC" 2>/dev/null; then
  MANAGE_STICKY=1
fi

require_command() {
  command_name=$1
  recovery=$2
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '%s\n' "[claude1] 安装失败：找不到 ${command_name}。${recovery}" >&2
    exit 1
  fi
}

require_command zsh "请先安装 zsh，再重新运行安装器。"
require_command python3 "请先安装 Python 3.11 或更高版本。"

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  >/dev/null 2>&1; then
  printf '%s\n' "[claude1] 安装失败：需要 Python 3.11 或更高版本。" >&2
  exit 1
fi

require_command claude "请先安装 Claude Code CLI，并确认 claude 已加入 PATH。"

if [ ! -f "$CC_SWITCH_DB" ] || [ ! -r "$CC_SWITCH_DB" ]; then
  printf '%s\n' \
    "[claude1] 安装失败：找不到可读的 CC Switch 数据库：$CC_SWITCH_DB" \
    "[claude1] 请先安装并启动一次 CC Switch，完成至少一个 Claude provider 配置。" \
    >&2
  exit 1
fi

for source_file in \
  "$SCRIPT_DIR/claude-provider-once.py" \
  "$SCRIPT_DIR/claude-hub.py" \
  "$SCRIPT_DIR/claude_hub_catalog.py" \
  "$SCRIPT_DIR/claude1_account_pool.py" \
  "$SCRIPT_DIR/claude1_protocol.py" \
  "$SCRIPT_DIR/claude1_protocol_types.py" \
  "$SCRIPT_DIR/claude1_protocol_usage.py" \
  "$SCRIPT_DIR/claude1_transport.py" \
  "$SCRIPT_DIR/claude1_usage_report.py" \
  "$SCRIPT_DIR/statusline-model.py" \
  "$SCRIPT_DIR/codex-provider-once.py" \
  "$SCRIPT_DIR/scripts/zsh-functions.sh"
do
  if [ ! -f "$source_file" ] || [ ! -r "$source_file" ]; then
    printf '%s\n' "[claude1] 安装失败：仓库文件缺失或不可读：$source_file" >&2
    exit 1
  fi
done
if [ "$MANAGE_STICKY" -eq 1 ] &&
  { [ ! -f "$SCRIPT_DIR/scripts/zsh-sticky-integration.sh" ] ||
    [ ! -r "$SCRIPT_DIR/scripts/zsh-sticky-integration.sh" ]; }; then
  printf '%s\n' \
    "[claude1] 安装失败：仓库文件缺失或不可读：$SCRIPT_DIR/scripts/zsh-sticky-integration.sh" \
    >&2
  exit 1
fi

if [ -L "$INSTALL_ROOT/scripts/claude-provider-once.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude-hub.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude_hub_catalog.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude1_account_pool.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude1_protocol.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude1_protocol_types.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude1_protocol_usage.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude1_transport.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/claude1_usage_report.py" ] ||
  [ -L "$INSTALL_ROOT/scripts/statusline-model.py" ] ||
  [ -L "$CODEX_INSTALL_ROOT/scripts/codex-provider-once.py" ] ||
  [ -L "$INSTALL_ROOT/claude1/zsh-functions.sh" ] ||
  { [ "$MANAGE_STICKY" -eq 1 ] &&
    [ -L "$INSTALL_ROOT/claude1/zsh-sticky-integration.sh" ]; }; then
  printf '%s\n' \
    "[claude1] 安装失败：目标脚本包含符号链接。为避免改写链接目标，请先手动处理后重试。" \
    >&2
  exit 1
fi

for target_path in \
  "$INSTALL_ROOT/scripts/claude-provider-once.py" \
  "$INSTALL_ROOT/scripts/claude-hub.py" \
  "$INSTALL_ROOT/scripts/claude_hub_catalog.py" \
  "$INSTALL_ROOT/scripts/claude1_account_pool.py" \
  "$INSTALL_ROOT/scripts/claude1_protocol.py" \
  "$INSTALL_ROOT/scripts/claude1_protocol_types.py" \
  "$INSTALL_ROOT/scripts/claude1_protocol_usage.py" \
  "$INSTALL_ROOT/scripts/claude1_transport.py" \
  "$INSTALL_ROOT/scripts/claude1_usage_report.py" \
  "$INSTALL_ROOT/scripts/statusline-model.py" \
  "$CODEX_INSTALL_ROOT/scripts/codex-provider-once.py" \
  "$INSTALL_ROOT/claude1/zsh-functions.sh"
do
  if [ -e "$target_path" ] && [ ! -f "$target_path" ]; then
    printf '%s\n' "[claude1] 安装失败：目标不是普通文件：$target_path" >&2
    exit 1
  fi
done
if [ "$MANAGE_STICKY" -eq 1 ] &&
  [ -e "$INSTALL_ROOT/claude1/zsh-sticky-integration.sh" ] &&
  [ ! -f "$INSTALL_ROOT/claude1/zsh-sticky-integration.sh" ]; then
  printf '%s\n' \
    "[claude1] 安装失败：目标不是普通文件：$INSTALL_ROOT/claude1/zsh-sticky-integration.sh" \
    >&2
  exit 1
fi

if [ -L "$ZSHRC" ]; then
  if [ ! -e "$ZSHRC" ]; then
    printf '%s\n' "[claude1] 安装失败：~/.zshrc 是失效的符号链接：$ZSHRC" >&2
    exit 1
  fi
  ZSHRC_TARGET=$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$ZSHRC")
fi

if [ -e "$ZSHRC_TARGET" ] && [ ! -f "$ZSHRC_TARGET" ]; then
  printf '%s\n' "[claude1] 安装失败：~/.zshrc 不是普通文件：$ZSHRC_TARGET" >&2
  exit 1
fi

mkdir -p "$INSTALL_ROOT/scripts" "$INSTALL_ROOT/claude1"
INSTALL_ROOT=$(CDPATH= cd "$INSTALL_ROOT" && pwd -P)
mkdir -p "$CODEX_INSTALL_ROOT/scripts"
CODEX_INSTALL_ROOT=$(CDPATH= cd "$CODEX_INSTALL_ROOT" && pwd -P)

LAUNCHER_TARGET="$INSTALL_ROOT/scripts/claude-provider-once.py"
HUB_TARGET="$INSTALL_ROOT/scripts/claude-hub.py"
HUB_CATALOG_MODULE_TARGET="$INSTALL_ROOT/scripts/claude_hub_catalog.py"
ACCOUNT_POOL_MODULE_TARGET="$INSTALL_ROOT/scripts/claude1_account_pool.py"
PROTOCOL_TARGET="$INSTALL_ROOT/scripts/claude1_protocol.py"
PROTOCOL_TYPES_TARGET="$INSTALL_ROOT/scripts/claude1_protocol_types.py"
PROTOCOL_USAGE_TARGET="$INSTALL_ROOT/scripts/claude1_protocol_usage.py"
TRANSPORT_TARGET="$INSTALL_ROOT/scripts/claude1_transport.py"
USAGE_REPORT_TARGET="$INSTALL_ROOT/scripts/claude1_usage_report.py"
STATUSLINE_MODEL_TARGET="$INSTALL_ROOT/scripts/statusline-model.py"
CODEX_LAUNCHER_TARGET="$CODEX_INSTALL_ROOT/scripts/codex-provider-once.py"
SHELL_TARGET="$INSTALL_ROOT/claude1/zsh-functions.sh"
STICKY_TARGET="$INSTALL_ROOT/claude1/zsh-sticky-integration.sh"

shell_quote() {
  python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]), end="")' "$1"
}

SOURCE_LINE="export CLAUDE1_SCRIPT=$(shell_quote "$LAUNCHER_TARGET") CLAUDE1_HUB_SCRIPT=$(shell_quote "$HUB_TARGET"); source $(shell_quote "$SHELL_TARGET") $MANAGED_MARKER"
STICKY_SOURCE_LINE="source $(shell_quote "$STICKY_TARGET") $STICKY_MARKER"

file_mode() {
  if mode=$(stat -c '%a' "$1" 2>/dev/null); then
    :
  elif mode=$(stat -f '%Lp' "$1" 2>/dev/null); then
    :
  else
    mode=unknown
  fi
  case "$mode" in
    ''|*[!0-7]*) printf '%s' unknown ;;
    *) printf '%s' "$mode" ;;
  esac
}

needs_install() {
  source_path=$1
  target_path=$2
  expected_mode=$3

  [ -f "$target_path" ] || return 0
  cmp -s "$source_path" "$target_path" || return 0
  [ "$(file_mode "$target_path")" = "$expected_mode" ] || return 0
  return 1
}

zshrc_needs_update() {
  [ -f "$ZSHRC_TARGET" ] || return 0
  exact_count=$(grep -Fxc "$SOURCE_LINE" "$ZSHRC_TARGET" 2>/dev/null || true)
  managed_count=$(grep -Fc "$MANAGED_MARKER" "$ZSHRC_TARGET" 2>/dev/null || true)
  if [ "$exact_count" = "1" ] && [ "$managed_count" = "1" ]; then
    return 1
  fi
  return 0
}

sticky_zshrc_needs_update() {
  if [ "$DISABLE_STICKY" -eq 1 ]; then
    [ -f "$ZSHRC_TARGET" ] || return 1
    marker_count=$(grep -Fc "$STICKY_MARKER" "$ZSHRC_TARGET" 2>/dev/null || true)
    [ "$marker_count" = "0" ] && return 1
    return 0
  fi
  [ "$MANAGE_STICKY" -eq 1 ] || return 1
  [ -f "$ZSHRC_TARGET" ] || return 0
  exact_count=$(grep -Fxc "$STICKY_SOURCE_LINE" "$ZSHRC_TARGET" 2>/dev/null || true)
  marker_count=$(grep -Fc "$STICKY_MARKER" "$ZSHRC_TARGET" 2>/dev/null || true)
  if [ "$exact_count" = "1" ] && [ "$marker_count" = "1" ]; then
    return 1
  fi
  return 0
}

NEED_LAUNCHER=0
NEED_HUB=0
NEED_HUB_CATALOG_MODULE=0
NEED_ACCOUNT_POOL_MODULE=0
NEED_PROTOCOL=0
NEED_PROTOCOL_TYPES=0
NEED_PROTOCOL_USAGE=0
NEED_TRANSPORT=0
NEED_USAGE_REPORT=0
NEED_STATUSLINE_MODEL=0
NEED_CODEX_LAUNCHER=0
NEED_SHELL=0
NEED_STICKY=0
NEED_ZSHRC=0
needs_install "$SCRIPT_DIR/claude-provider-once.py" "$LAUNCHER_TARGET" 755 &&
  NEED_LAUNCHER=1
needs_install "$SCRIPT_DIR/claude-hub.py" "$HUB_TARGET" 755 &&
  NEED_HUB=1
needs_install "$SCRIPT_DIR/claude_hub_catalog.py" "$HUB_CATALOG_MODULE_TARGET" 644 &&
  NEED_HUB_CATALOG_MODULE=1
needs_install "$SCRIPT_DIR/claude1_account_pool.py" "$ACCOUNT_POOL_MODULE_TARGET" 644 &&
  NEED_ACCOUNT_POOL_MODULE=1
needs_install "$SCRIPT_DIR/claude1_protocol.py" "$PROTOCOL_TARGET" 644 &&
  NEED_PROTOCOL=1
needs_install "$SCRIPT_DIR/claude1_protocol_types.py" "$PROTOCOL_TYPES_TARGET" 644 &&
  NEED_PROTOCOL_TYPES=1
needs_install "$SCRIPT_DIR/claude1_protocol_usage.py" "$PROTOCOL_USAGE_TARGET" 644 &&
  NEED_PROTOCOL_USAGE=1
needs_install "$SCRIPT_DIR/claude1_transport.py" "$TRANSPORT_TARGET" 644 &&
  NEED_TRANSPORT=1
needs_install "$SCRIPT_DIR/claude1_usage_report.py" "$USAGE_REPORT_TARGET" 644 &&
  NEED_USAGE_REPORT=1
needs_install "$SCRIPT_DIR/statusline-model.py" "$STATUSLINE_MODEL_TARGET" 755 &&
  NEED_STATUSLINE_MODEL=1
needs_install "$SCRIPT_DIR/codex-provider-once.py" "$CODEX_LAUNCHER_TARGET" 755 &&
  NEED_CODEX_LAUNCHER=1
needs_install "$SCRIPT_DIR/scripts/zsh-functions.sh" "$SHELL_TARGET" 644 &&
  NEED_SHELL=1
if [ "$MANAGE_STICKY" -eq 1 ]; then
  needs_install "$SCRIPT_DIR/scripts/zsh-sticky-integration.sh" "$STICKY_TARGET" 644 &&
    NEED_STICKY=1
fi
zshrc_needs_update && NEED_ZSHRC=1
sticky_zshrc_needs_update && NEED_ZSHRC=1

BACKUP_DIR=""
ensure_backup_dir() {
  if [ -n "$BACKUP_DIR" ]; then
    return
  fi
  backup_base="$INSTALL_ROOT/backups"
  mkdir -p "$backup_base"
  stamp=$(date '+%Y%m%d-%H%M%S')
  candidate="$backup_base/$stamp-$$"
  suffix=0
  while [ -e "$candidate" ]; do
    suffix=$((suffix + 1))
    candidate="$backup_base/$stamp-$$-$suffix"
  done
  mkdir -m 700 "$candidate"
  BACKUP_DIR=$candidate
}

backup_existing() {
  source_path=$1
  backup_name=$2
  if [ -f "$source_path" ]; then
    ensure_backup_dir
    cp -p "$source_path" "$BACKUP_DIR/$backup_name"
  fi
}

if [ "$NEED_LAUNCHER" -eq 1 ]; then
  backup_existing "$LAUNCHER_TARGET" "claude-provider-once.py"
fi
if [ "$NEED_HUB" -eq 1 ]; then
  backup_existing "$HUB_TARGET" "claude-hub.py"
fi
if [ "$NEED_HUB_CATALOG_MODULE" -eq 1 ]; then
  backup_existing "$HUB_CATALOG_MODULE_TARGET" "claude_hub_catalog.py"
fi
if [ "$NEED_ACCOUNT_POOL_MODULE" -eq 1 ]; then
  backup_existing "$ACCOUNT_POOL_MODULE_TARGET" "claude1_account_pool.py"
fi
if [ "$NEED_PROTOCOL" -eq 1 ]; then
  backup_existing "$PROTOCOL_TARGET" "claude1_protocol.py"
fi
if [ "$NEED_PROTOCOL_TYPES" -eq 1 ]; then
  backup_existing "$PROTOCOL_TYPES_TARGET" "claude1_protocol_types.py"
fi
if [ "$NEED_PROTOCOL_USAGE" -eq 1 ]; then
  backup_existing "$PROTOCOL_USAGE_TARGET" "claude1_protocol_usage.py"
fi
if [ "$NEED_TRANSPORT" -eq 1 ]; then
  backup_existing "$TRANSPORT_TARGET" "claude1_transport.py"
fi
if [ "$NEED_USAGE_REPORT" -eq 1 ]; then
  backup_existing "$USAGE_REPORT_TARGET" "claude1_usage_report.py"
fi
if [ "$NEED_STATUSLINE_MODEL" -eq 1 ]; then
  backup_existing "$STATUSLINE_MODEL_TARGET" "statusline-model.py"
fi
if [ "$NEED_CODEX_LAUNCHER" -eq 1 ]; then
  backup_existing "$CODEX_LAUNCHER_TARGET" "codex-provider-once.py"
fi
if [ "$NEED_SHELL" -eq 1 ]; then
  backup_existing "$SHELL_TARGET" "zsh-functions.sh"
fi
if [ "$NEED_STICKY" -eq 1 ]; then
  backup_existing "$STICKY_TARGET" "zsh-sticky-integration.sh"
fi
if [ "$NEED_ZSHRC" -eq 1 ]; then
  backup_existing "$ZSHRC_TARGET" "zshrc"
fi

install_file() {
  source_path=$1
  target_path=$2
  target_mode=$3
  target_dir=$(dirname "$target_path")
  temporary=$(mktemp "$target_dir/.claude1-install.XXXXXX")
  cp "$source_path" "$temporary"
  chmod "$target_mode" "$temporary"
  mv "$temporary" "$target_path"
}

if [ "$NEED_HUB_CATALOG_MODULE" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude_hub_catalog.py" "$HUB_CATALOG_MODULE_TARGET" 644
fi
if [ "$NEED_ACCOUNT_POOL_MODULE" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude1_account_pool.py" "$ACCOUNT_POOL_MODULE_TARGET" 644
fi
if [ "$NEED_USAGE_REPORT" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude1_usage_report.py" "$USAGE_REPORT_TARGET" 644
fi
if [ "$NEED_HUB" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude-hub.py" "$HUB_TARGET" 755
fi
if [ "$NEED_LAUNCHER" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude-provider-once.py" "$LAUNCHER_TARGET" 755
fi
if [ "$NEED_PROTOCOL_TYPES" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude1_protocol_types.py" "$PROTOCOL_TYPES_TARGET" 644
fi
if [ "$NEED_PROTOCOL_USAGE" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude1_protocol_usage.py" "$PROTOCOL_USAGE_TARGET" 644
fi
if [ "$NEED_PROTOCOL" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude1_protocol.py" "$PROTOCOL_TARGET" 644
fi
if [ "$NEED_TRANSPORT" -eq 1 ]; then
  install_file "$SCRIPT_DIR/claude1_transport.py" "$TRANSPORT_TARGET" 644
fi
if [ "$NEED_STATUSLINE_MODEL" -eq 1 ]; then
  install_file "$SCRIPT_DIR/statusline-model.py" "$STATUSLINE_MODEL_TARGET" 755
fi
if [ "$NEED_CODEX_LAUNCHER" -eq 1 ]; then
  install_file "$SCRIPT_DIR/codex-provider-once.py" "$CODEX_LAUNCHER_TARGET" 755
fi
if [ "$NEED_SHELL" -eq 1 ]; then
  install_file "$SCRIPT_DIR/scripts/zsh-functions.sh" "$SHELL_TARGET" 644
fi
if [ "$NEED_STICKY" -eq 1 ]; then
  install_file "$SCRIPT_DIR/scripts/zsh-sticky-integration.sh" "$STICKY_TARGET" 644
fi

if [ "$NEED_ZSHRC" -eq 1 ]; then
  zshrc_dir=$(dirname "$ZSHRC_TARGET")
  mkdir -p "$zshrc_dir"
  temporary=$(mktemp "$zshrc_dir/.claude1-zshrc.XXXXXX")
  zshrc_mode=600
  if [ -f "$ZSHRC_TARGET" ]; then
    existing_mode=$(file_mode "$ZSHRC_TARGET")
    if [ "$existing_mode" != "unknown" ]; then
      zshrc_mode=$existing_mode
    fi
    if [ "$MANAGE_STICKY" -eq 1 ] || [ "$DISABLE_STICKY" -eq 1 ]; then
      awk -v marker="$MANAGED_MARKER" -v sticky="$STICKY_MARKER" \
        'index($0, marker) == 0 && index($0, sticky) == 0 { print }' \
        "$ZSHRC_TARGET" > "$temporary"
    else
      awk -v marker="$MANAGED_MARKER" 'index($0, marker) == 0 { print }' \
        "$ZSHRC_TARGET" > "$temporary"
    fi
  fi
  printf '%s\n' "$SOURCE_LINE" >> "$temporary"
  if [ "$MANAGE_STICKY" -eq 1 ]; then
    printf '%s\n' "$STICKY_SOURCE_LINE" >> "$temporary"
  fi
  chmod "$zshrc_mode" "$temporary"
  mv "$temporary" "$ZSHRC_TARGET"
fi

printf '%s\n' \
  "[claude1] 已安装启动器：$LAUNCHER_TARGET" \
  "[claude1] 已安装可选 Hub：$HUB_TARGET" \
  "[claude1] 已安装 codex1 启动器：$CODEX_LAUNCHER_TARGET" \
  "[claude1] 已接入 zsh：$ZSHRC"
if [ "$MANAGE_STICKY" -eq 1 ]; then
  printf '%s\n' \
    "[claude1] 已显式启用普通 claude 的粘性路由：$STICKY_TARGET"
fi

if [ -n "$BACKUP_DIR" ]; then
  printf '%s\n' "[claude1] 原文件备份：$BACKUP_DIR"
fi

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' \
    "[claude1] 提示：未找到 uv；provider 选择仍可使用，但可选的 claude-hub 暂不能启动。" \
    "[claude1] 如需会话内 /model 切换，请先安装 uv。" \
    >&2
fi

printf '%s\n' "[claude1] 完成。运行：source $(shell_quote "$ZSHRC")，然后输入 claude1。"
