#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

MAX_WAIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait) MAX_WAIT="${2:-0}"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ ! "$MAX_WAIT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --wait must be a non-negative integer." >&2
  exit 1
fi

env_value() {
  local name="$1"
  if [[ ! -f .env ]]; then
    return
  fi
  awk -F= -v key="$name" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' .env
}

DAEMON_PORT_VALUE="$(env_value DAEMON_PORT)"
DASHBOARD_PORT_VALUE="$(env_value DASHBOARD_PORT)"
DAEMON_PORT_VALUE="${DAEMON_PORT_VALUE:-9000}"
DASHBOARD_PORT_VALUE="${DASHBOARD_PORT_VALUE:-9321}"
READINESS_URL="http://127.0.0.1:${DAEMON_PORT_VALUE}/readiness"

deadline=$((SECONDS + MAX_WAIT))
while true; do
  readiness="$(curl -fsS "$READINESS_URL" 2>/dev/null || true)"
  if printf '%s' "$readiness" | grep -q '"status":"ready"'; then
    break
  fi
  if [[ "$MAX_WAIT" -eq 0 ]] || [[ $SECONDS -ge $deadline ]]; then
    echo "FAIL: The classic stack is not ready."
    if [[ -n "$readiness" ]]; then
      printf '%s\n' "$readiness"
    fi
    echo "Run: docker compose ps"
    echo "Run: docker compose logs daemon agent litellm redis"
    exit 1
  fi
  sleep 3
done

echo "PASS: Redis, SQLite, LiteLLM, the starter agent, and classic are ready."

DASHBOARD_HEALTH_URL="http://127.0.0.1:${DASHBOARD_PORT_VALUE}/api/health"
while true; do
  if curl -fsS "$DASHBOARD_HEALTH_URL" >/dev/null 2>&1; then
    echo "PASS: Mission Control is reachable."
    break
  fi
  if [[ "$MAX_WAIT" -eq 0 ]] || [[ $SECONDS -ge $deadline ]]; then
    echo "FAIL: Mission Control is not reachable."
    echo "Run: docker compose logs dashboard"
    exit 1
  fi
  sleep 2
done
