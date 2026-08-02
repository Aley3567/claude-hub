#!/usr/bin/env zsh
# Safe default shell integration for claude1.
#
# Source this file from ~/.zshrc. It defines `claude1` and the explicit
# `claude1-direct` helper only. In particular, it never aliases or replaces the
# ordinary `claude` command.

claude1() {
  local launcher="${CLAUDE1_SCRIPT:-$HOME/.claude/scripts/claude-provider-once.py}"
  local python="${CLAUDE1_PYTHON:-python3}"
  local python_path=""

  # Personal Claude Code patch helpers may be defined earlier in ~/.zshrc.
  # Keep this managed function as the single launcher entrypoint while
  # preserving those optional preflight hooks.
  if (( ${+functions[_claude_ensure_toolsearch_patch]} )); then
    _claude_ensure_toolsearch_patch
  fi
  if (( ${+functions[_claude_ensure_ghostty_progress_patch]} )); then
    _claude_ensure_ghostty_progress_patch
  fi

  if [[ ! -f "$launcher" || ! -r "$launcher" ]]; then
    print -u2 -- "[claude1] launcher script not found or unreadable: $launcher"
    print -u2 -- "[claude1] install claude-provider-once.py there or set CLAUDE1_SCRIPT."
    return 127
  fi

  if [[ "$python" == */* ]]; then
    python_path="$python"
  else
    python_path="$(whence -p "$python" 2>/dev/null)"
  fi
  if [[ -z "$python_path" || ! -x "$python_path" ]]; then
    print -u2 -- "[claude1] Python executable not found: $python"
    print -u2 -- "[claude1] install Python 3 or set CLAUDE1_PYTHON."
    return 127
  fi

  local arg
  for arg in "$@"; do
    if [[ "$arg" == "-dp" ]]; then
      print -u2 -- "[claude1] -dp is not supported; pass --dangerously-skip-permissions explicitly if you intentionally accept that risk."
      return 2
    fi
  done

  # Make the cleanup local to this function so the caller's shell environment
  # is preserved after claude1 exits.
  local CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS
  local CLAUDE_CODE_WORKFLOWS
  unset CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS
  unset CLAUDE_CODE_WORKFLOWS
  CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN="${CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN:-1}" \
    "$python_path" "$launcher" "$@"
}

# Explicit escape hatch that always invokes the real Claude Code executable.
claude1-direct() {
  local configured="${CLAUDE1_CLAUDE_BIN:-claude}"
  local executable=""

  if [[ "$configured" == */* ]]; then
    executable="$configured"
  else
    executable="$(whence -p "$configured" 2>/dev/null)"
  fi
  if [[ -z "$executable" && -z "${CLAUDE1_CLAUDE_BIN:-}" ]]; then
    local fallback="${CLAUDE1_DEFAULT_CLAUDE_BIN:-$HOME/.local/bin/claude}"
    [[ -x "$fallback" ]] && executable="$fallback"
  fi
  if [[ -z "$executable" || ! -x "$executable" ]]; then
    print -u2 -- "[claude1] Claude Code executable not found: $configured"
    print -u2 -- "[claude1] install Claude Code or set CLAUDE1_CLAUDE_BIN."
    return 127
  fi

  "$executable" "$@"
}
