#!/usr/bin/env bash
# AnyRouter availability probe for claude1.
# Checks if the AnyRouter endpoint is healthy.

set -u

TOKEN_FILE="${HOME}/anyrouter-tools/token"
ENDPOINT="***REMOVED***/v1/health"
TIMEOUT=10

probe() {
  local token
  if [[ -f "$TOKEN_FILE" ]]; then
    token=$(cat "$TOKEN_FILE")
  else
    echo "ERROR: token file not found at $TOKEN_FILE" >&2
    exit 1
  fi

  local http_code
  http_code=$(curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout "$TIMEOUT" \
    -H "Authorization: Bearer $token" \
    "$ENDPOINT" 2>/dev/null || echo "000")

  if [[ "$http_code" == "200" ]]; then
    echo "OK"
    exit 0
  else
    echo "FAIL: HTTP $http_code"
    exit 1
  fi
}

probe "$@"
