#!/usr/bin/env zsh

set -eu

typeset -r TEST_DIR="${0:A:h}"
typeset -r REPO_ROOT="${TEST_DIR:h}"
typeset -r INSTALLER="${REPO_ROOT}/install.sh"
typeset -r SYSTEM_ZSH="$(whence -p zsh)"
typeset -r SYSTEM_PYTHON="$(whence -p python3)"
typeset -r SYSTEM_DIRNAME="$(whence -p dirname)"
typeset -r TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claude1-install.XXXXXX")"

cleanup() {
  command rm -rf -- "$TEMP_ROOT"
}
trap cleanup EXIT HUP INT TERM

typeset -gi PASSED=0

fail() {
  print -u2 -- "not ok - $1"
  return 1
}

pass() {
  PASSED=$((PASSED + 1))
  print -- "ok ${PASSED} - $1"
}

assert_file_content() {
  local expected="$1"
  local file_path="$2"
  [[ -f "$file_path" ]] || fail "missing file: $file_path"
  [[ "$(<"$file_path")" == "$expected" ]] ||
    fail "unexpected content in $file_path"
}

write_command() {
  local file_path="$1"
  local body="${2:-exit 0}"
  print -r -- '#!/bin/sh' > "$file_path"
  print -r -- "$body" >> "$file_path"
  command chmod 755 "$file_path"
}

make_home() {
  local name="$1"
  local home="${TEMP_ROOT}/${name}"
  command mkdir -p -- "$home/bin" "$home/.cc-switch"
  write_command "$home/bin/zsh" "exec ${(q)SYSTEM_ZSH} \"\$@\""
  write_command "$home/bin/python3" "exec ${(q)SYSTEM_PYTHON} \"\$@\""
  write_command "$home/bin/claude" 'printf "%s\n" "ordinary-claude"'
  command touch "$home/.cc-switch/cc-switch.db"
  print -r -- "$home"
}

run_install() {
  local home="$1"
  shift
  HOME="$home" \
    CLAUDE1_INSTALL_ROOT="$home/install root" \
    PATH="$home/bin:/usr/bin:/bin" \
    /bin/sh "$INSTALLER" "$@"
}

managed_line_count() {
  local file_path="$1"
  command grep -c '# claude1 managed source' "$file_path" 2>/dev/null || true
}

sticky_line_count() {
  local file_path="$1"
  command grep -c '# claude1 managed sticky source' "$file_path" 2>/dev/null || true
}

test_first_install_is_safe() {
  local home="$(make_home first)"
  print -r -- 'claude() { print -- "kept-claude"; }' > "$home/.zshrc"

  local output
  output="$(run_install "$home" 2>&1)"

  command cmp -s "$REPO_ROOT/claude-provider-once.py" \
    "$home/install root/scripts/claude-provider-once.py" ||
    fail "launcher was not installed"
  command cmp -s "$REPO_ROOT/claude-hub.py" \
    "$home/install root/scripts/claude-hub.py" ||
    fail "hub was not installed"
  command cmp -s "$REPO_ROOT/claude1_protocol.py" \
    "$home/install root/scripts/claude1_protocol.py" ||
    fail "protocol bridge was not installed"
  command cmp -s "$REPO_ROOT/statusline-model.py" \
    "$home/install root/scripts/statusline-model.py" ||
    fail "statusline model resolver was not installed"
  command cmp -s "$REPO_ROOT/zsh-functions.sh" \
    "$home/install root/claude1/zsh-functions.sh" ||
    fail "safe shell integration was not installed"
  [[ "$(managed_line_count "$home/.zshrc")" == "1" ]] ||
    fail "managed source line was not added exactly once"
  [[ "$(<"$home/.zshrc")" != *"zsh-sticky-integration"* ]] ||
    fail "installer enabled sticky integration"
  [[ ! -e "$home/install root/claude1/zsh-sticky-integration.sh" ]] ||
    fail "installer copied sticky integration"
  [[ "$output" == *"未找到 uv"* ]] ||
    fail "missing uv warning was not shown"
  HOME="$home" "$SYSTEM_PYTHON" \
    "$home/install root/scripts/claude-provider-once.py" --help \
    >/dev/null ||
    fail "installed launcher cannot import the protocol bridge"

  local shell_output
  shell_output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" "$SYSTEM_ZSH" -f -c '
      source "$HOME/.zshrc"
      (( ${+functions[claude1]} )) || exit 20
      claude
    '
  )"
  [[ "$shell_output" == "kept-claude" ]] ||
    fail "ordinary claude function was replaced"

  pass "first install adds only the safe claude1 integration"
}

test_repeated_install_is_idempotent() {
  local home="$(make_home repeat)"
  print -r -- '# user config' > "$home/.zshrc"

  run_install "$home" >/dev/null 2>&1
  local before_zshrc="$(<"$home/.zshrc")"
  local -a before_backups=("$home/install root/backups"/*(N))

  run_install "$home" >/dev/null 2>&1
  local -a after_backups=("$home/install root/backups"/*(N))

  [[ "$(<"$home/.zshrc")" == "$before_zshrc" ]] ||
    fail "second install changed .zshrc"
  [[ "$(managed_line_count "$home/.zshrc")" == "1" ]] ||
    fail "second install duplicated managed source line"
  [[ ${#after_backups} -eq ${#before_backups} ]] ||
    fail "unchanged reinstall created another backup"

  pass "repeated install is idempotent"
}

test_explicit_sticky_install_routes_ordinary_claude_and_survives_safe_reinstall() {
  local home="$(make_home sticky)"
  print -r -- '# user config' > "$home/.zshrc"
  print -r -- 'direct' > "$home/.cc-switch/claude1-backend"

  run_install "$home" --enable-sticky >/dev/null 2>&1

  command cmp -s "$REPO_ROOT/zsh-sticky-integration.sh" \
    "$home/install root/claude1/zsh-sticky-integration.sh" ||
    fail "sticky integration was not installed after explicit opt-in"
  [[ "$(sticky_line_count "$home/.zshrc")" == "1" ]] ||
    fail "sticky source line was not added exactly once"

  local output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" \
      "$SYSTEM_ZSH" -f -c 'source "$HOME/.zshrc"; claude sample'
  )"
  [[ "$output" == "ordinary-claude" ]] ||
    fail "opt-in install did not route ordinary claude through sticky backend"

  print -r -- '# stale sticky integration' > \
    "$home/install root/claude1/zsh-sticky-integration.sh"
  run_install "$home" >/dev/null 2>&1
  [[ "$(sticky_line_count "$home/.zshrc")" == "1" ]] ||
    fail "safe reinstall removed or duplicated an existing sticky opt-in"
  command cmp -s "$REPO_ROOT/zsh-sticky-integration.sh" \
    "$home/install root/claude1/zsh-sticky-integration.sh" ||
    fail "safe reinstall did not update an existing sticky opt-in"

  pass "explicit sticky install reconnects claude1 use state"
}

test_disable_sticky_removes_only_managed_sticky_source() {
  local home="$(make_home disable-sticky)"
  print -r -- '# user config' > "$home/.zshrc"
  run_install "$home" --enable-sticky >/dev/null 2>&1
  run_install "$home" --disable-sticky >/dev/null 2>&1

  [[ "$(sticky_line_count "$home/.zshrc")" == "0" ]] ||
    fail "--disable-sticky did not remove the managed sticky source"
  [[ "$(managed_line_count "$home/.zshrc")" == "1" ]] ||
    fail "--disable-sticky removed the safe integration"
  [[ "$(<"$home/.zshrc")" == *'# user config'* ]] ||
    fail "--disable-sticky removed user shell configuration"
  pass "--disable-sticky revokes the managed sticky integration"
}

test_existing_files_are_backed_up() {
  local home="$(make_home backup)"
  command mkdir -p -- "$home/install root/scripts" "$home/install root/claude1"
  print -r -- 'old launcher' > "$home/install root/scripts/claude-provider-once.py"
  print -r -- 'old hub' > "$home/install root/scripts/claude-hub.py"
  print -r -- 'old protocol' > "$home/install root/scripts/claude1_protocol.py"
  print -r -- 'old statusline model' > "$home/install root/scripts/statusline-model.py"
  print -r -- 'old shell' > "$home/install root/claude1/zsh-functions.sh"
  command mkdir -p -- "$home/dotfiles"
  print -r -- 'old zshrc' > "$home/dotfiles/zshrc"
  command ln -s "$home/dotfiles/zshrc" "$home/.zshrc"

  run_install "$home" >/dev/null 2>&1

  [[ -L "$home/.zshrc" ]] || fail "installer replaced the .zshrc symlink"
  local -a backups=("$home/install root/backups"/*(N/))
  [[ ${#backups} -eq 1 ]] || fail "expected one backup directory"
  local backup="${backups[1]}"
  assert_file_content "old launcher" "$backup/claude-provider-once.py"
  assert_file_content "old hub" "$backup/claude-hub.py"
  assert_file_content "old protocol" "$backup/claude1_protocol.py"
  assert_file_content "old statusline model" "$backup/statusline-model.py"
  assert_file_content "old shell" "$backup/zsh-functions.sh"
  assert_file_content "old zshrc" "$backup/zshrc"

  pass "existing targets and .zshrc are backed up before replacement"
}

make_dependency_path() {
  local home="$1"
  shift
  local bin_path="$home/dependency-bin"
  command mkdir -p -- "$bin_path"
  command ln -s "$SYSTEM_DIRNAME" "$bin_path/dirname"
  local name
  for name in "$@"; do
    write_command "$bin_path/$name"
  done
  print -r -- "$bin_path"
}

assert_missing_dependency() {
  local label="$1"
  local expected="$2"
  local home="$3"
  local bin_path="$4"
  local output exit_code

  set +e
  output="$(
    HOME="$home" CLAUDE1_INSTALL_ROOT="$home/install" PATH="$bin_path" \
      /bin/sh "$INSTALLER" 2>&1
  )"
  exit_code=$?
  set -e

  [[ $exit_code -ne 0 ]] || fail "$label should fail installation"
  [[ "$output" == *"$expected"* ]] ||
    fail "$label error did not explain the missing dependency"
}

test_missing_dependencies_are_clear() {
  local home_zsh="${TEMP_ROOT}/missing-zsh"
  command mkdir -p -- "$home_zsh/.cc-switch"
  command touch "$home_zsh/.cc-switch/cc-switch.db"
  local path_zsh="$(make_dependency_path "$home_zsh" python3 claude)"
  assert_missing_dependency "missing zsh" "找不到 zsh" "$home_zsh" "$path_zsh"

  local home_python="${TEMP_ROOT}/missing-python"
  command mkdir -p -- "$home_python/.cc-switch"
  command touch "$home_python/.cc-switch/cc-switch.db"
  local path_python="$(make_dependency_path "$home_python" zsh claude)"
  assert_missing_dependency "missing Python" "找不到 python3" "$home_python" "$path_python"

  local home_claude="${TEMP_ROOT}/missing-claude"
  command mkdir -p -- "$home_claude/.cc-switch"
  command touch "$home_claude/.cc-switch/cc-switch.db"
  local path_claude="$(make_dependency_path "$home_claude" zsh python3)"
  assert_missing_dependency "missing Claude Code" "找不到 claude" \
    "$home_claude" "$path_claude"

  local home_db="${TEMP_ROOT}/missing-db"
  command mkdir -p -- "$home_db"
  local path_db="$(make_dependency_path "$home_db" zsh python3 claude)"
  assert_missing_dependency "missing CC Switch DB" "CC Switch 数据库" \
    "$home_db" "$path_db"

  pass "missing hard dependencies have actionable errors"
}

test_first_install_is_safe
test_repeated_install_is_idempotent
test_explicit_sticky_install_routes_ordinary_claude_and_survives_safe_reinstall
test_disable_sticky_removes_only_managed_sticky_source
test_existing_files_are_backed_up
test_missing_dependencies_are_clear

print -- "1..${PASSED}"
