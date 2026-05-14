#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# bMAS — Post-Deploy Health Check
# ─────────────────────────────────────────────────────────────────────
# Pings every control plane service and reports status.
#
# Usage:
#   ./scripts/healthcheck.sh              # uses bmas.yaml defaults
#   ./scripts/healthcheck.sh --wait 60    # wait up to 60s for services
# ─────────────────────────────────────────────────────────────────────

set -uo pipefail

# ── Parse Args ───────────────────────────────────────────────────────

MAX_WAIT=0  # seconds to wait for services (0 = single check, no retry)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait) MAX_WAIT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# ── Load Config ──────────────────────────────────────────────────────

BMAS_CONFIG="${BMAS_CONFIG:-/etc/bmas/bmas.yaml}"

# Try local path first (dev), then container path
if [[ ! -f "$BMAS_CONFIG" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO_ROOT="$(dirname "$SCRIPT_DIR")"
  if [[ -f "$REPO_ROOT/bmas.yaml" ]]; then
    BMAS_CONFIG="$REPO_ROOT/bmas.yaml"
  else
    echo "❌ Config file not found: $BMAS_CONFIG"
    echo "   Set BMAS_CONFIG or run from the repo root."
    exit 1
  fi
fi

# Parse host and ports from bmas.yaml using Python (available everywhere)
eval "$(python3 - "$BMAS_CONFIG" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f)
cp = cfg.get("control_plane", {})
host = cp.get("host", "localhost")
ports = cp.get("ports", {})
print(f'HOST="{host}"')
print(f'REDIS_PORT={ports.get("redis", 6379)}')
print(f'LITELLM_PORT={ports.get("litellm", 4000)}')
print(f'TRIAGE_PORT={ports.get("triage", 8001)}')
print(f'DAEMON_PORT={ports.get("daemon", 9000)}')
print(f'PROJECT_NAME="{cfg.get("project", {}).get("name", "bMAS")}"')
triage_enabled = cfg.get("triage", {}).get("enabled", True)
print(f'TRIAGE_ENABLED={"true" if triage_enabled else "false"}')
PYEOF
)"

# ── Helpers ──────────────────────────────────────────────────────────

PASS=0
FAIL=0
SKIP=0

check_service() {
  local name="$1"
  local url="$2"
  local timeout="${3:-5}"

  local http_code
  http_code=$(curl -sf -o /dev/null -w '%{http_code}' --connect-timeout "$timeout" --max-time "$timeout" "$url" 2>/dev/null) || http_code="000"

  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "  ✅ $name — healthy (HTTP $http_code)"
    ((PASS++))
    return 0
  else
    echo "  ❌ $name — unreachable (HTTP $http_code)"
    ((FAIL++))
    return 1
  fi
}

check_redis() {
  local password="${REDIS_PASSWORD:-}"

  if ! command -v redis-cli &>/dev/null; then
    # Fallback: try TCP connect
    if timeout 3 bash -c "echo PING | nc -q1 $HOST $REDIS_PORT 2>/dev/null" | grep -q PONG 2>/dev/null; then
      echo "  ✅ Redis — healthy (PONG via nc)"
      ((PASS++))
      return 0
    elif timeout 3 bash -c "cat < /dev/tcp/$HOST/$REDIS_PORT" &>/dev/null; then
      echo "  ✅ Redis — port open (no redis-cli for full check)"
      ((PASS++))
      return 0
    else
      echo "  ❌ Redis — unreachable at $HOST:$REDIS_PORT"
      ((FAIL++))
      return 1
    fi
  fi

  local args=(-h "$HOST" -p "$REDIS_PORT")
  if [[ -n "$password" ]]; then
    args+=(-a "$password" --no-auth-warning)
  fi

  if redis-cli "${args[@]}" ping 2>/dev/null | grep -q PONG; then
    echo "  ✅ Redis — healthy (PONG)"
    ((PASS++))
    return 0
  else
    echo "  ❌ Redis — unreachable at $HOST:$REDIS_PORT"
    ((FAIL++))
    return 1
  fi
}

# ── Banner ───────────────────────────────────────────────────────────

echo ""
echo "┌─────────────────────────────────────────────┐"
echo "│      $PROJECT_NAME — Health Check            "
echo "└─────────────────────────────────────────────┘"
echo "  Host: $HOST"
echo "  Config: $BMAS_CONFIG"
if [[ "$MAX_WAIT" -gt 0 ]]; then
  echo "  Mode: wait up to ${MAX_WAIT}s for services"
fi
echo ""

# ── Wait Mode ────────────────────────────────────────────────────────

if [[ "$MAX_WAIT" -gt 0 ]]; then
  echo "  Waiting for services to come up..."
  DEADLINE=$((SECONDS + MAX_WAIT))

  while [[ $SECONDS -lt $DEADLINE ]]; do
    # Check if daemon (last to start) is responding
    if curl -sf -o /dev/null --connect-timeout 2 --max-time 2 "http://$HOST:$DAEMON_PORT/health" 2>/dev/null; then
      echo "  ✓ Services detected, running checks..."
      echo ""
      break
    fi
    REMAINING=$((DEADLINE - SECONDS))
    printf "\r  ⏳ Waiting... (%ds remaining)  " "$REMAINING"
    sleep 3
  done

  if [[ $SECONDS -ge $DEADLINE ]]; then
    echo ""
    echo "  ⚠️  Timeout reached, checking whatever is up..."
    echo ""
  fi
fi

# ── Run Checks ───────────────────────────────────────────────────────

echo "  Checking services..."
echo ""

# 1. Redis
check_redis

# 2. LiteLLM
check_service "LiteLLM" "http://$HOST:$LITELLM_PORT/health/readiness"

# 3. Triage (only if enabled)
if [[ "$TRIAGE_ENABLED" == "true" ]]; then
  check_service "Triage (vLLM)" "http://$HOST:$TRIAGE_PORT/health"
else
  echo "  ⏭️  Triage — skipped (disabled in config)"
  ((SKIP++))
fi

# 4. Daemon
check_service "Daemon" "http://$HOST:$DAEMON_PORT/health"

# 5. Beszel Hub (optional — only if configured in bmas.yaml)
BESZEL_HUB=$(python3 - "$BMAS_CONFIG" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f)
print(cfg.get("monitoring", {}).get("beszel_hub", ""))
PYEOF
)

if [[ -n "$BESZEL_HUB" ]]; then
  check_service "Beszel Hub" "$BESZEL_HUB/api/health"

  # Try authenticated check for data access
  if [[ -f "$REPO_ROOT/.env" ]]; then
    source "$REPO_ROOT/.env"
  fi
  if [[ -n "${BESZEL_EMAIL:-}" && -n "${BESZEL_PASSWORD:-}" ]]; then
    AUTH_RESULT=$(curl -sf -X POST "$BESZEL_HUB/api/collections/users/auth-with-password" \
      -H 'Content-Type: application/json' \
      -d "{\"identity\":\"$BESZEL_EMAIL\",\"password\":\"$BESZEL_PASSWORD\"}" 2>/dev/null)
    if echo "$AUTH_RESULT" | grep -q '"token"'; then
      echo "  ✅ Beszel Auth — credentials valid"
      ((PASS++))
    else
      echo "  ❌ Beszel Auth — invalid credentials (BESZEL_EMAIL/BESZEL_PASSWORD)"
      ((FAIL++))
    fi
  else
    echo "  ⏭️  Beszel Auth — skipped (no BESZEL_EMAIL/BESZEL_PASSWORD in .env)"
    ((SKIP++))
  fi
else
  echo "  ⏭️  Beszel Hub — skipped (not configured in bmas.yaml)"
  ((SKIP++))
fi

# ── Summary ──────────────────────────────────────────────────────────

echo ""
TOTAL=$((PASS + FAIL + SKIP))
if [[ "$FAIL" -eq 0 ]]; then
  echo "┌─────────────────────────────────────────────┐"
  echo "│  ✅ All services healthy ($PASS/$TOTAL passed)          "
  echo "└─────────────────────────────────────────────┘"
  echo ""
  exit 0
else
  echo "┌─────────────────────────────────────────────┐"
  echo "│  ❌ $FAIL service(s) unhealthy ($PASS/$TOTAL passed)     "
  echo "└─────────────────────────────────────────────┘"
  echo ""
  echo "  Troubleshooting:"
  echo "    docker compose ps        # check container status"
  echo "    docker compose logs -f   # watch logs"
  echo ""
  exit 1
fi
