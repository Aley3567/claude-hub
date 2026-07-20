#!/usr/bin/env bash
# Alert script for AnyRouter monitoring.
# Sends macOS notification when AnyRouter is down.

set -u

TITLE="${1:-AnyRouter Alert}"
MESSAGE="${2:-Service check failed}"

# macOS notification
osascript -e "display notification \"$MESSAGE\" with title \"$TITLE\"" 2>/dev/null || true

echo "[$(date '+%F %T')] ALERT: $TITLE - $MESSAGE"
