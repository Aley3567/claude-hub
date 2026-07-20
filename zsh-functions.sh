#!/usr/bin/env zsh
# claude1 shell functions — add these to ~/.zshrc

# `claude1` injects a selected provider through a temporary settings file.
export CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1

# Claude skill toggle
alias sk="bash ~/.claude/scripts/skill-toggle.sh"

# Auto-patch Claude Code Tool Search after updates
_claude_ensure_toolsearch_patch() {
  # ... (patch logic, see full script)
}

# Main claude1 function
claude1() {
  _claude_ensure_toolsearch_patch
  unset CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS
  unset CLAUDE_CODE_WORKFLOWS
  local args=()
  for arg in "$@"; do
    if [[ "$arg" == "-dp" ]]; then
      args+=(--dangerously-skip-permissions)
    else
      args+=("$arg")
    fi
  done
  python3 "$HOME/.claude/scripts/claude-provider-once.py" "${args[@]}"
}

# Default Claude entrypoint follows claude1's sticky backend.
# 默认(无状态) = reclaude
claude() {
  _claude_ensure_toolsearch_patch
  local _sticky="$HOME/.cc-switch/claude1-backend"
  local _backend=reclaude
  [[ -r "$_sticky" ]] && _backend="$(<"$_sticky")"
  if [[ "$_backend" == reclaude ]]; then
    "$HOME/.local/bin/reclaude-isolated" "$@"
  else
    command claude "$@"
  fi
}

# Explicit raw Claude Code CLI entrypoint.
claude-direct() {
  command claude "$@"
}

# AnyRouter 专用 Claude Code 启动命令
alias claude-any='command claude --settings ~/.claude/settings.anyrouter.json'

# Notion MCP 按需启动
alias claude-notion='claude --mcp-config ~/.claude/mcp-notion.json'
