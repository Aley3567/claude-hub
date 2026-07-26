#!/usr/bin/env zsh

set -eu

typeset -r TEST_DIR="${0:A:h}"
typeset -r REPO_ROOT="${TEST_DIR:h}"
typeset -r INSTALLER="${REPO_ROOT}/install.sh"
typeset -r SYSTEM_PYTHON="$(whence -p python3)"
typeset -r TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claude-hub-install.XXXXXX")"

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

write_fake_python() {
  local target="$1"
  command mkdir -p -- "${target:h}"
  {
    print -r -- '#!/bin/sh'
    print -r -- 'set -eu'
    print -r -- 'if { [ "${FAKE_PYTHON_TOO_OLD:-0}" = "1" ] || [ "${FAKE_OLD_COMMAND:-}" = "$0" ]; } && [ "${1:-}" = "-c" ]; then'
    print -r -- '  exit 1'
    print -r -- 'fi'
    print -r -- 'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then'
    print -r -- '  shift 2'
    print -r -- '  if [ "${1:-}" = "--version" ]; then'
    print -r -- '    [ "${FAKE_PIP_AVAILABLE:-1}" = "1" ] || exit 1'
    print -r -- '    [ "${FAKE_NO_PIP_COMMAND:-}" != "$0" ] || exit 1'
    print -r -- '    printf "%s\n" "pip 99.0 from fake fixture"'
    print -r -- '    exit 0'
    print -r -- '  fi'
    print -r -- '  [ "${FAKE_PIP_AVAILABLE:-1}" = "1" ] || exit 1'
    print -r -- '  [ "${FAKE_NO_PIP_COMMAND:-}" != "$0" ] || exit 1'
    print -r -- '  [ "${1:-}" = "install" ] || exit 91'
    print -r -- '  : "${FAKE_PIP_LOG:?}"'
    print -r -- '  {'
    print -r -- '    printf "%s\n" "CALL"'
    print -r -- '    for argument in "$@"; do'
    print -r -- '      printf "ARG=%s\n" "$argument"'
    print -r -- '    done'
    print -r -- '    printf "%s\n" "END"'
    print -r -- '  } >> "$FAKE_PIP_LOG"'
    print -r -- '  exit 0'
    print -r -- 'fi'
    print -r -- "exec ${(q)SYSTEM_PYTHON} \"\$@\""
  } > "$target"
  command chmod 755 "$target"
}

make_fixture() {
  local name="$1"
  local python_name="$2"
  local root="${TEMP_ROOT}/${name}"
  command mkdir -p -- "$root/bin" "$root/home"
  write_fake_python "$root/bin/$python_name"
  : > "$root/pip.log"
  print -r -- "$root"
}

run_install() {
  local root="$1"
  shift
  (
    unset HOME
    PATH="$root/bin" \
      FAKE_PIP_LOG="$root/pip.log" \
      FAKE_PIP_AVAILABLE="${FAKE_PIP_AVAILABLE:-1}" \
      FAKE_PYTHON_TOO_OLD="${FAKE_PYTHON_TOO_OLD:-0}" \
      /bin/sh "$INSTALLER" "$@"
  )
}

assert_no_legacy_writes() {
  local root="$1"
  [[ ! -e "$root/home/.zshrc" ]] ||
    fail "installer wrote .zshrc"
  [[ ! -e "$root/home/.claude" ]] ||
    fail "installer wrote the legacy Claude install tree"
  [[ ! -e "$root/home/.cc-switch" ]] ||
    fail "installer wrote CC Switch state"
  [[ ! -e "$root/home/.config" ]] ||
    fail "installer wrote settings under HOME"
}

expected_call() {
  local package_spec="$1"
  print -r -- "CALL"
  print -r -- "ARG=install"
  print -r -- "ARG=--upgrade"
  print -r -- "ARG=$package_spec"
  print -r -- "END"
}

assert_success_output() {
  local output="$1"
  local python_path="$2"
  local package_spec="$3"
  local expected_command
  expected_command="$(
    "$SYSTEM_PYTHON" - "$python_path" "$package_spec" <<'PY'
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
  )"

  [[ "$output" == *"[claude-hub] 执行: $expected_command"* ]] ||
    fail "installer did not print the exact safely quoted pip command"
  [[ "$output" == *$'  claude-hub\n  claude1\n  switchctl'* ]] ||
    fail "installer did not print all installed entry points"
}

test_macos_fresh_install_uses_python3_and_core_package() {
  local root="$(make_fixture "macOS fixture's fresh install" python3)"
  local output
  output="$(run_install "$root" 2>&1)"

  [[ "$(<"$root/pip.log")" == "$(expected_call claude-hub-kit)" ]] ||
    fail "fresh macOS fixture did not use the direct core pip spec"
  assert_success_output "$output" "$root/bin/python3" "claude-hub-kit"
  assert_no_legacy_writes "$root"

  pass "macOS fixture performs a core pip install without HOME"
}

test_linux_repeated_run_keeps_upgrade_semantics() {
  local root="$(make_fixture linux-repeat python)"

  run_install "$root" >/dev/null 2>&1
  run_install "$root" >/dev/null 2>&1

  local expected="$(expected_call claude-hub-kit)"
  [[ "$(<"$root/pip.log")" == "${expected}"$'\n'"${expected}" ]] ||
    fail "repeated Linux fixture did not make two identical upgrade calls"
  assert_no_legacy_writes "$root"

  pass "Linux fresh and repeated runs both use pip --upgrade"
}

test_desktop_installs_only_the_desktop_extra() {
  local root="$(make_fixture desktop python3)"
  local output
  output="$(run_install "$root" --desktop 2>&1)"

  [[ "$(<"$root/pip.log")" == "$(expected_call 'claude-hub-kit[desktop]')" ]] ||
    fail "--desktop did not install the desktop extra"
  assert_success_output \
    "$output" "$root/bin/python3" "claude-hub-kit[desktop]"
  assert_no_legacy_writes "$root"

  pass "--desktop maps to the published desktop extra"
}

test_missing_python_fails_before_pip() {
  local root="${TEMP_ROOT}/missing-python"
  command mkdir -p -- "$root/bin" "$root/home"
  : > "$root/pip.log"
  local output exit_code

  set +e
  output="$(run_install "$root" 2>&1)"
  exit_code=$?
  set -e

  [[ $exit_code -eq 1 ]] || fail "missing Python should exit 1"
  [[ "$output" == *"找不到 Python 3.11"* ]] ||
    fail "missing Python error is not actionable"
  [[ ! -s "$root/pip.log" ]] || fail "missing Python reached pip"
  assert_no_legacy_writes "$root"

  pass "missing Python fails before pip or configuration access"
}

test_missing_pip_fails_before_install() {
  local root="$(make_fixture missing-pip python3)"
  local output exit_code

  set +e
  output="$(
    FAKE_PIP_AVAILABLE=0 run_install "$root" 2>&1
  )"
  exit_code=$?
  set -e

  [[ $exit_code -eq 1 ]] || fail "missing pip should exit 1"
  [[ "$output" == *"均没有 pip"* ]] ||
    fail "missing pip error is not actionable"
  [[ ! -s "$root/pip.log" ]] || fail "missing pip attempted installation"
  assert_no_legacy_writes "$root"

  pass "missing pip fails before installation"
}

test_old_python_is_skipped_for_a_compatible_fallback() {
  local root="${TEMP_ROOT}/python-fallback"
  command mkdir -p -- "$root/bin" "$root/home"
  write_fake_python "$root/bin/python3"
  write_fake_python "$root/bin/python"
  : > "$root/pip.log"

  local output
  output="$(
    (
      unset HOME
      PATH="$root/bin" \
        FAKE_PIP_LOG="$root/pip.log" \
        FAKE_PIP_AVAILABLE=1 \
        FAKE_PYTHON_TOO_OLD=0 \
        FAKE_OLD_COMMAND="$root/bin/python3" \
        /bin/sh "$INSTALLER"
    ) 2>&1
  )"

  [[ "$(<"$root/pip.log")" == "$(expected_call claude-hub-kit)" ]] ||
    fail "compatible fallback did not invoke pip"
  [[ "$output" == *"$root/bin/python -m pip install --upgrade claude-hub-kit"* ]] ||
    fail "installer did not skip the incompatible python3 fixture"

  pass "compatible Python selection is deterministic"
}

test_python_without_pip_is_skipped_for_a_working_pair() {
  local root="${TEMP_ROOT}/pip-fallback"
  command mkdir -p -- "$root/bin" "$root/home"
  write_fake_python "$root/bin/python3"
  write_fake_python "$root/bin/python"
  : > "$root/pip.log"

  local output
  output="$(
    (
      unset HOME
      PATH="$root/bin" \
        FAKE_PIP_LOG="$root/pip.log" \
        FAKE_PIP_AVAILABLE=1 \
        FAKE_PYTHON_TOO_OLD=0 \
        FAKE_NO_PIP_COMMAND="$root/bin/python3" \
        /bin/sh "$INSTALLER"
    ) 2>&1
  )"

  [[ "$(<"$root/pip.log")" == "$(expected_call claude-hub-kit)" ]] ||
    fail "Python/pip fallback did not invoke fake pip"
  [[ "$output" == *"$root/bin/python -m pip install --upgrade claude-hub-kit"* ]] ||
    fail "installer did not select the Python with working pip"

  pass "Python discovery selects a compatible interpreter/pip pair"
}

test_existing_legacy_configuration_is_untouched() {
  local root="$(make_fixture existing-config python3)"
  command mkdir -p -- \
    "$root/home/.cc-switch" \
    "$root/home/.claude" \
    "$root/home/.config/claude-hub"
  print -r -- "fixture zsh config" > "$root/home/.zshrc"
  print -r -- "fixture database placeholder" \
    > "$root/home/.cc-switch/cc-switch.db"
  print -r -- '{"fixture":"claude-settings-placeholder"}' \
    > "$root/home/.claude/settings.json"
  print -r -- '{"fixture":"provider-profile-placeholder"}' \
    > "$root/home/.config/claude-hub/profiles.json"

  local before_tree
  before_tree="$(
    command find "$root/home" -type f -print -exec cksum {} \; |
      LC_ALL=C command sort
  )"

  HOME="$root/home" \
    PATH="$root/bin" \
    FAKE_PIP_LOG="$root/pip.log" \
    FAKE_PIP_AVAILABLE=1 \
    FAKE_PYTHON_TOO_OLD=0 \
    /bin/sh "$INSTALLER" >/dev/null 2>&1

  local after_tree
  after_tree="$(
    command find "$root/home" -type f -print -exec cksum {} \; |
      LC_ALL=C command sort
  )"
  [[ "$after_tree" == "$before_tree" ]] ||
    fail "installer modified or created legacy configuration"
  [[ "$(<"$root/pip.log")" == "$(expected_call claude-hub-kit)" ]] ||
    fail "configuration fixture did not reach fake pip"

  pass "existing Provider, Claude, shell, and profile files stay untouched"
}

test_unknown_argument_is_usage_error_without_pip() {
  local root="$(make_fixture unknown-argument python3)"
  local output exit_code
  local private_argument="fixture-private-argument"

  set +e
  output="$(run_install "$root" "$private_argument" 2>&1)"
  exit_code=$?
  set -e

  [[ $exit_code -eq 2 ]] || fail "unknown argument should exit 2"
  [[ "$output" == *"未知参数。"* ]] ||
    fail "unknown argument error is unclear"
  [[ "$output" != *"$private_argument"* ]] ||
    fail "unknown argument was echoed"
  [[ ! -s "$root/pip.log" ]] || fail "unknown argument reached pip"
  assert_no_legacy_writes "$root"

  pass "unknown arguments fail closed without pip"
}

test_macos_fresh_install_uses_python3_and_core_package
test_linux_repeated_run_keeps_upgrade_semantics
test_desktop_installs_only_the_desktop_extra
test_missing_python_fails_before_pip
test_missing_pip_fails_before_install
test_old_python_is_skipped_for_a_compatible_fallback
test_python_without_pip_is_skipped_for_a_working_pair
test_existing_legacy_configuration_is_untouched
test_unknown_argument_is_usage_error_without_pip

print -- "1..${PASSED}"
