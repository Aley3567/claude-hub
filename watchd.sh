#!/usr/bin/env bash
# Watch daemon for AnyRouter monitoring.
# Runs probe.sh periodically and alerts on failure.

set -u

DIR="${HOME}/anyrouter-tools"
PROBE_GAP=$((5 * 3600))

while true; do
  if ! bash "$DIR/probe.sh" > /dev/null 2>&1; then
    bash "$DIR/alert.sh" "AnyRouter Down" "Probe failed at $(date '+%F %T')"
  fi
  sleep "$PROBE_GAP"
done
