#!/usr/bin/env zsh
# Optional sticky integration for claude1.
#
# Source this file only if you intentionally want the ordinary `claude` command
# to follow ~/.cc-switch/claude1-backend. The safe default integration in
# zsh-functions.sh does not source this file and never replaces `claude`.
#
# To opt out again in the current shell:
#   unfunction claude

claude() {
  local state_file="${CLAUDE1_STICKY_FILE:-$HOME/.cc-switch/claude1-backend}"
  local backend="direct"

  if [[ -r "$state_file" ]]; then
    IFS= read -r backend < "$state_file"
    backend="${backend//$'\r'/}"
    [[ -n "$backend" ]] || backend="direct"
  fi
  backend="${backend:l}"

  case "$backend" in
    direct|provider)
      # `provider` is a legacy value. It did not retain a provider identity, so
      # the only safe compatibility behavior is the real Claude Code command.
      local configured="${CLAUDE1_CLAUDE_BIN:-claude}"
      local executable=""
      if [[ "$configured" == */* ]]; then
        executable="$configured"
      else
        executable="$(whence -p "$configured" 2>/dev/null)"
      fi
      if [[ -z "$executable" || ! -x "$executable" ]]; then
        print -u2 -- "[claude1] direct backend selected but Claude Code executable not found: $configured"
        print -u2 -- "[claude1] install Claude Code or set CLAUDE1_CLAUDE_BIN."
        return 127
      fi
      "$executable" "$@"
      ;;

    current|anyrouter|hub)
      if (( ${+functions[claude1]} )); then
        claude1 "$backend" "$@"
        return $?
      fi

      local launcher="$(whence -p claude1 2>/dev/null)"
      if [[ -z "$launcher" || ! -x "$launcher" ]]; then
        print -u2 -- "[claude1] $backend backend selected but the claude1 launcher is unavailable."
        print -u2 -- "[claude1] source zsh-functions.sh first or install a claude1 executable."
        return 127
      fi
      "$launcher" "$backend" "$@"
      ;;

    *)
      print -u2 -- "[claude1] unsupported sticky backend '$backend' in $state_file"
      print -u2 -- "[claude1] supported values: direct, hub, current, anyrouter."
      return 2
      ;;
  esac
}
