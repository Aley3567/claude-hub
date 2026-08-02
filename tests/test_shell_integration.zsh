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

  print -r -- '#!/bin/sh' > "$home/bin/python3"
  print -r -- 'printf "python3:%s\n" "$*" >> "$CALL_LOG"' >> "$home/bin/python3"

  print -r -- '# launcher fixture' > "$home/launcher.py"
  command chmod +x "$home/bin/claude" "$home/bin/python3"
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
      CLAUDE1_SCRIPT="$HOME/launcher.py" claude1 hello
    '

  assert_line $'claude:untouched\npython3:'"${home}"$'/launcher.py hello' "$log"
  pass "default integration leaves ordinary claude untouched"
}

test_default_rejects_hidden_dangerous_shorthand() {
  local home="$(make_home no-dp)"
  local output exit_code
  set +e
  output="$(
    HOME="$home" PATH="$home/bin:/usr/bin:/bin" DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" \
      CLAUDE1_SCRIPT="$home/launcher.py" \
      zsh -f -c 'source "$DEFAULT_SCRIPT_PATH"; claude1 -dp' 2>&1
  )"
  exit_code=$?
  set -e
  [[ $exit_code -eq 2 ]] || fail "-dp should be rejected"
  [[ "$output" == *"not supported"* ]] || fail "-dp rejection is unclear"
  pass "claude1 rejects the hidden dangerous shorthand"
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

test_default_preserves_preflight_hooks_and_cleans_experimental_env() {
  local home="$(make_home preflight)"
  local log="${home}/calls.log"
  print -r -- '#!/bin/sh' > "$home/bin/python3"
  print -r -- 'printf "python3:%s\n" "$*" >> "$CALL_LOG"' >> "$home/bin/python3"
  print -r -- 'printf "child-env:%s:%s\n" "${CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS-unset}" "${CLAUDE_CODE_WORKFLOWS-unset}" >> "$CALL_LOG"' >> "$home/bin/python3"
  command chmod +x "$home/bin/python3"

  HOME="$home" CALL_LOG="$log" PATH="$home/bin:/usr/bin:/bin" \
    DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" \
    CLAUDE1_SCRIPT="$home/launcher.py" \
    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS="must-not-leak" \
    CLAUDE_CODE_WORKFLOWS="must-not-leak" \
    zsh -f -c '
      _claude_ensure_toolsearch_patch() { print -r -- toolsearch >> "$CALL_LOG"; }
      _claude_ensure_ghostty_progress_patch() { print -r -- ghostty >> "$CALL_LOG"; }
      source "$DEFAULT_SCRIPT_PATH"
      claude1 sample
      print -r -- "after:${CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS}:${CLAUDE_CODE_WORKFLOWS}" >> "$CALL_LOG"
    '

  assert_line $'toolsearch\nghostty\npython3:'"${home}"$'/launcher.py sample\nchild-env:unset:unset\nafter:must-not-leak:must-not-leak' "$log"
  pass "managed claude1 preserves optional preflights and scopes env cleanup"
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
  pass "opt-in integration routes every supported sticky backend"
}

test_opt_in_replaces_existing_claude_alias_and_reads_launcher_sticky_path() {
  local home="$(make_home alias)"
  local state="$home/custom-backend"
  print -r -- "hub" > "$state"
  local log="$home/calls.log"
  HOME="$home" CALL_LOG="$log" PATH="$home/bin:/usr/bin:/bin" \
    DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" STICKY_SCRIPT_PATH="$STICKY_INTEGRATION" \
    CLAUDE1_SCRIPT="$home/launcher.py" CLAUDE1_BACKEND_STICKY="$state" \
    zsh -f -c 'alias claude="print stale"; source "$DEFAULT_SCRIPT_PATH"; source "$STICKY_SCRIPT_PATH"; claude sample'
  assert_line "python3:${home}/launcher.py hub sample" "$log"
  pass "sticky integration replaces aliases and reads launcher sticky path"
}

test_direct_falls_back_to_default_claude_binary() {
  local home="$(make_home direct-fallback)"
  command mkdir -p -- "$home/.local/bin"
  command cp "$home/bin/claude" "$home/.local/bin/claude"
  local log="$home/calls.log"
  HOME="$home" CALL_LOG="$log" PATH="/usr/bin:/bin" DEFAULT_SCRIPT_PATH="$DEFAULT_INTEGRATION" \
    zsh -f -c 'source "$DEFAULT_SCRIPT_PATH"; claude1-direct sample'
  assert_line "claude:sample" "$log"
  pass "claude1-direct falls back to the default Claude binary"
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
test_default_rejects_hidden_dangerous_shorthand
test_default_preserves_existing_claude_function
test_default_reports_missing_launcher
test_default_preserves_preflight_hooks_and_cleans_experimental_env
test_opt_in_routes
test_opt_in_replaces_existing_claude_alias_and_reads_launcher_sticky_path
test_direct_falls_back_to_default_claude_binary
test_opt_in_reports_missing_direct_binary
test_opt_in_reports_missing_launcher
test_opt_in_rejects_unknown_backend

print -- "1..${PASSED}"
