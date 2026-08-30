#!/usr/bin/env bash
# check-ci.sh — Run the complete required check set locally before pushing.
#
# The authoritative test manifest (test-manifest.yaml) defines the set.
# This script executes the same complete profile that continuous
# integration covers with its partition profiles, including Playwright.
#
# Usage: ./scripts/check-ci.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="$REPO_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: Run ./scripts/bmas setup-dev before ./scripts/bmas test." >&2
  exit 1
fi

# Resolve python3 and the Python tools inside the project environment.
export PATH="$REPO_ROOT/.venv/bin:$PATH"

# Remove stale Next.js build artifacts so tsc matches continuous integration.
if [ -d mission-control/.next ]; then
  echo "Removing stale mission-control/.next (not present in continuous integration)"
  rm -rf mission-control/.next
fi

exec "$PYTHON" scripts/run-test-manifest.py --profile complete "$@"
