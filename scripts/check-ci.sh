#!/usr/bin/env bash
# check-ci.sh — Run all CI checks locally before pushing.
# Mirrors .github/workflows/ci.yml exactly.
# Usage: ./scripts/check-ci.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0

run_check() {
  local name="$1"; shift
  echo -e "\n${YELLOW}▶ ${name}${NC}"
  if "$@" 2>&1; then
    echo -e "${GREEN}✅ ${name} — PASS${NC}"
    ((PASS++)) || true
  else
    echo -e "${RED}❌ ${name} — FAIL${NC}"
    ((FAIL++)) || true
  fi
}

# ── Pre-flight: clear .next so tsc matches CI (no stale build artifacts) ──────
if [ -d mission-control/.next ]; then
  echo -e "${YELLOW}Removing stale mission-control/.next (not present in CI)${NC}"
  rm -rf mission-control/.next
fi

# ── Daemon ─────────────────────────────────────────────────────────────────────
echo -e "\n━━━ DAEMON ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cd "$REPO_ROOT/daemon"

run_check "Daemon: ruff"     ruff check src/ tests/
run_check "Daemon: mypy"     mypy src/ --ignore-missing-imports
run_check "Daemon: pytest"   pytest tests/ -q --tb=short

# ── Agent ──────────────────────────────────────────────────────────────────────
echo -e "\n━━━ AGENT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cd "$REPO_ROOT/agent"
if [ -f requirements.txt ]; then pip install -q -r requirements.txt; fi

run_check "Agent: pytest"    pytest tests/ -q --tb=short

# ── Mission Control ────────────────────────────────────────────────────────────
echo -e "\n━━━ MISSION CONTROL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
cd "$REPO_ROOT/mission-control"

run_check "Dashboard: npm ci"  npm ci
run_check "Dashboard: eslint"  npm run lint
run_check "Dashboard: tsc"     npx tsc --noEmit
run_check "Dashboard: tests"   npm run test:run
run_check "Dashboard: build"   npm run build

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ "$FAIL" -eq 0 ]; then
  echo -e "${GREEN}✅ ALL ${PASS} CHECKS PASSED — safe to push${NC}"
  exit 0
else
  echo -e "${RED}❌ ${FAIL} CHECK(S) FAILED, ${PASS} passed — fix before pushing${NC}"
  exit 1
fi
