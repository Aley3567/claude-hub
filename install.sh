#!/bin/sh

set -eu

PACKAGE_NAME="claude-hub-kit"
DESKTOP_PACKAGE="${PACKAGE_NAME}[desktop]"

usage() {
  printf '%s\n' \
    "用法: ./install.sh [--desktop]" \
    "" \
    "通过当前可用的 Python 3.11+ 和 pip 安装或升级 claude-hub-kit。" \
    "" \
    "选项：" \
    "  --desktop    安装 claude-hub-kit[desktop] 桌面功能" \
    "  -h, --help   显示本帮助" \
    "" \
    "安装完成后可用入口：" \
    "  claude-hub" \
    "  claude1" \
    "  switchctl" \
    "" \
    "安装器不读取或写入 HOME、CC Switch、Claude settings、Provider 或凭据。"
}

case "$#" in
  0)
    package_spec=$PACKAGE_NAME
    ;;
  1)
    case "$1" in
      --desktop)
        package_spec=$DESKTOP_PACKAGE
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        printf '%s\n' "[claude-hub] 未知参数。" >&2
        usage >&2
        exit 2
        ;;
    esac
    ;;
  *)
    printf '%s\n' "[claude-hub] 参数过多。" >&2
    usage >&2
    exit 2
    ;;
esac

python_path=""
python_found=0
compatible_python_found=0
for python_command in python3 python
do
  if candidate=$(command -v "$python_command" 2>/dev/null); then
    python_found=1
    if "$candidate" -c \
      'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1; then
      compatible_python_found=1
      if "$candidate" -m pip --version >/dev/null 2>&1; then
        python_path=$candidate
        break
      fi
    fi
  fi
done

if [ -z "$python_path" ]; then
  if [ "$python_found" -eq 0 ]; then
    printf '%s\n' \
      "[claude-hub] 安装失败：找不到 Python 3.11 或更高版本。" >&2
  elif [ "$compatible_python_found" -eq 0 ]; then
    printf '%s\n' \
      "[claude-hub] 安装失败：需要 Python 3.11 或更高版本。" >&2
  else
    printf '%s\n' \
      "[claude-hub] 安装失败：可用的 Python 3.11+ 均没有 pip。" \
      "[claude-hub] 请先为 Python 3.11+ 安装 pip，再重新运行。" \
      >&2
  fi
  exit 1
fi

quoted_command=$(
  "$python_path" - "$python_path" "$package_spec" <<'PY'
import shlex
import sys

print(
    shlex.join(
        [
            sys.argv[1],
            "-m",
            "pip",
            "install",
            "--upgrade",
            sys.argv[2],
        ]
    )
)
PY
)

printf '%s\n' "[claude-hub] 执行: $quoted_command"
"$python_path" -m pip install --upgrade "$package_spec"

printf '%s\n' \
  "[claude-hub] 安装完成。可用入口：" \
  "  claude-hub" \
  "  claude1" \
  "  switchctl"
