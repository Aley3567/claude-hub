#!/usr/bin/env bash
# Records real claude1 "Any router" turn outcomes without storing conversation text.

set -u
umask 077

DIR="$HOME/anyrouter-tools"
LOG="$DIR/watch.log"
NEXT_PROBE="$DIR/.next_probe"
PROBE_GAP=$((5 * 3600))

trim_log() {
  if [[ -f "$LOG" ]] && [[ $(wc -l < "$LOG") -gt 500 ]]; then
    tail -200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi
}

record() {
  local status="$1"
  local message="$2"
  local gap="$3"
  local now

  now=$(date +%s)
  echo $((now + gap)) > "$NEXT_PROBE"
  echo "[$(date '+%F %T')] $status $message; next=${gap}s" >> "$LOG"
  trim_log
}

case "${1:-}" in
  success)
    # Stop only fires after Claude finishes a real response. No prompt or transcript is read.
    record "LIVE_OK" "真实 claude1 会话已成功响应" "$PROBE_GAP"
    ;;
  failure)
    # StopFailure input contains an error category. Ignore all conversation fields.
    error_type=$(/usr/bin/jq -r '.error // "unknown"' 2>/dev/null || echo "unknown")
    case "$error_type" in
      rate_limit)
        record "LIVE_BUSY" "真实 claude1 会话限流" "$PROBE_GAP"
        ;;
      overloaded|server_error)
        record "LIVE_DOWN" "真实 claude1 会话上游故障" "$PROBE_GAP"
        ;;
      authentication_failed|oauth_org_not_allowed)
        record "LIVE_AUTH" "真实 claude1 会话认证失败" "$PROBE_GAP"
        ;;
      *)
        record "LIVE_UNK" "真实 claude1 会话失败($error_type)" "$PROBE_GAP"
        ;;
    esac
    ;;
  *)
    exit 0
    ;;
esac

exit 0
