#!/usr/bin/env zsh

set -eu

typeset -r TEST_DIR="${0:A:h}"
typeset -r REPO_ROOT="${TEST_DIR:h}"
typeset -r DEFAULT_INTEGRATION="${REPO_ROOT}/zsh-functions.sh"
typeset -r STICKY_INTEGRATION="${REPO_ROOT}/zsh-sticky-integration.sh"
typeset -r TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/claude1-shell.XXXXXX")"

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

assert_line() {
  local expected="$1"
  local file="$2"
  local actual=""
  [[ -r "$file" ]] && actual="$(<"$file")"
  [[ "$actual" == "$expected" ]] || fail "expected '$expected', got '$actual'"
}

make_home() {
  local name="$1"
  local home="${TEMP_ROOT}/${name}"
  command mkdir -p -- "$home/bin" "$home/.cc-switch"

  print -r -- '#!/bin/sh' > "$home/bin/claude"
  print -r -- 'printf "claude:%s\n" "$*" >> "$CALL_LOG"' >> "$home/bin/claude"

  print -r -- '#!/bin/sh' > "$home/bin/reclaude-isolated"
  print -r -- 'printf "reclaude:%s\n" "$*" >> "$CALL_LOG"' >> "$home/bin/reclaude-isolated"

  print -r -- '#!/bin/sh' > "$home/bin/python3"
  print -r -- 'printf "python3:%s\n" "$*" >> "$CALL_LOG"' >> "$home/bin/python3"

  print -r -- '# launcher fixture' > "$home/launcher.py"
  command chmod +x "$home/bin/claude" "$home/bin/reclaude-isolated" "$home/bin/python3"
  print -r -- "$home"
}

test_default_does_not_override_claude() {
  local home="$(make_home default)"
  local log="${home}/calls.log"

  HOME="$home" CALL_LOG="$log" PATH="$home/bin:/usr/bin:/bin" \
    DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" \
    zsh -f -c '
      source "$DEFAULT_SCRIPT_PATH"
      (( ! ${+functions[claude]} )) || exit 40
      command claude untouched
      CLAUDE1_SCRIPT="$HOME/launcher.py" claude1 -dp hello
    '

  assert_line $'claude:untouched\npython3:'"${home}"$'/launcher.py --dangerously-skip-permissions hello' "$log"
  pass "default integration leaves ordinary claude untouched"
}

test_default_preserves_existing_claude_function() {
  local home="$(make_home preserve)"
  local output

  output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" \
      zsh -f -c '
        claude() { print -- existing-claude; }
        source "$DEFAULT_SCRIPT_PATH"
        claude
      '
  )"

  [[ "$output" == "existing-claude" ]] || fail "existing claude function was replaced"
  pass "default integration preserves an existing claude function"
}

test_default_reports_missing_launcher() {
  local home="$(make_home missing-launcher)"
  local output exit_code

  set +e
  output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" \
      CLAUDE1_SCRIPT="$home/does-not-exist.py" \
      zsh -f -c 'source "$DEFAULT_SCRIPT_PATH"; claude1' 2>&1
  )"
  exit_code=$?
  set -e

  [[ $exit_code -eq 127 ]] || fail "missing launcher should return 127"
  [[ "$output" == *"launcher script not found"* ]] || fail "missing launcher error is unclear"
  pass "claude1 reports a missing launcher clearly"
}

run_sticky_case() {
  local home="$1"
  local backend="$2"
  local expected="$3"
  local log="${home}/calls.log"
  : > "$log"

  if [[ "$backend" != "<missing>" ]]; then
    print -r -- "$backend" > "$home/.cc-switch/claude1-backend"
  fi

  HOME="$home" CALL_LOG="$log" PATH="$home/bin:/usr/bin:/bin" \
    DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" STICKY_SCRIPT_PATH="$STICKY_INTEGRATION" \
    CLAUDE1_SCRIPT="$home/launcher.py" \
    zsh -f -c '
      source "$DEFAULT_SCRIPT_PATH"
      source "$STICKY_SCRIPT_PATH"
      claude sample
    '

  assert_line "$expected" "$log"
}

test_opt_in_routes() {
  local home="$(make_home routes)"

  run_sticky_case "$home" "<missing>" "claude:sample"
  run_sticky_case "$home" "direct" "claude:sample"
  run_sticky_case "$home" "provider" "claude:sample"
  run_sticky_case "$home" "current" "python3:${home}/launcher.py current sample"
  run_sticky_case "$home" "anyrouter" "python3:${home}/launcher.py anyrouter sample"
  run_sticky_case "$home" "hub" "python3:${home}/launcher.py hub sample"
  run_sticky_case "$home" "reclaude" "reclaude:sample"

  pass "opt-in integration routes every supported sticky backend"
}

test_opt_in_reports_missing_reclaude() {
  local home="$(make_home missing-reclaude)"
  local output exit_code
  print -r -- "reclaude" > "$home/.cc-switch/claude1-backend"

  set +e
  output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" \
      DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" STICKY_SCRIPT_PATH="$STICKY_INTEGRATION" \
      CLAUDE1_RECLAUDE_BIN="missing-reclaude" \
      zsh -f -c 'source "$DEFAULT_SCRIPT_PATH"; source "$STICKY_SCRIPT_PATH"; claude' 2>&1
  )"
  exit_code=$?
  set -e

  [[ $exit_code -eq 127 ]] || fail "missing reclaude should return 127"
  [[ "$output" == *"reclaude backend selected but executable not found"* ]] \
    || fail "missing reclaude error is unclear"
  pass "opt-in integration reports a missing reclaude executable clearly"
}

test_opt_in_reports_missing_direct_binary() {
  local home="$(make_home missing-direct)"
  local output exit_code
  print -r -- "direct" > "$home/.cc-switch/claude1-backend"

  set +e
  output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" \
      DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" STICKY_SCRIPT_PATH="$STICKY_INTEGRATION" \
      CLAUDE1_CLAUDE_BIN="missing-claude" \
      zsh -f -c 'source "$DEFAULT_SCRIPT_PATH"; source "$STICKY_SCRIPT_PATH"; claude' 2>&1
  )"
  exit_code=$?
  set -e

  [[ $exit_code -eq 127 ]] || fail "missing direct Claude should return 127"
  [[ "$output" == *"direct backend selected but Claude Code executable not found"* ]] \
    || fail "missing direct Claude error is unclear"
  pass "opt-in integration reports a missing Claude Code executable clearly"
}

test_opt_in_reports_missing_launcher() {
  local home="$(make_home missing-sticky-launcher)"
  local output exit_code
  print -r -- "hub" > "$home/.cc-switch/claude1-backend"

  set +e
  output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" STICKY_SCRIPT_PATH="$STICKY_INTEGRATION" \
      zsh -f -c 'source "$STICKY_SCRIPT_PATH"; claude' 2>&1
  )"
  exit_code=$?
  set -e

  [[ $exit_code -eq 127 ]] || fail "missing claude1 launcher should return 127"
  [[ "$output" == *"claude1 launcher is unavailable"* ]] \
    || fail "missing claude1 launcher error is unclear"
  pass "opt-in integration reports a missing claude1 launcher clearly"
}

test_opt_in_rejects_unknown_backend() {
  local home="$(make_home unknown)"
  local output exit_code
  print -r -- "surprise" > "$home/.cc-switch/claude1-backend"

  set +e
  output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" \
      DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" STICKY_SCRIPT_PATH="$STICKY_INTEGRATION" \
      zsh -f -c 'source "$DEFAULT_SCRIPT_PATH"; source "$STICKY_SCRIPT_PATH"; claude' 2>&1
  )"
  exit_code=$?
  set -e

  [[ $exit_code -eq 2 ]] || fail "unknown backend should return 2"
  [[ "$output" == *"unsupported sticky backend"* ]] || fail "unknown backend error is unclear"
  pass "opt-in integration rejects unknown sticky values"
}

test_default_does_not_override_claude
test_default_preserves_existing_claude_function
test_default_reports_missing_launcher
test_opt_in_routes
test_opt_in_reports_missing_reclaude
test_opt_in_reports_missing_direct_binary
test_opt_in_reports_missing_launcher
test_opt_in_rejects_unknown_backend

print -- "1..${PASSED}"
