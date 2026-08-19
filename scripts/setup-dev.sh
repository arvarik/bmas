#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

PYTHON_COMMAND="${PYTHON_COMMAND:-python3.13}"
command -v "$PYTHON_COMMAND" >/dev/null 2>&1 || {
  echo "ERROR: Install Python 3.13 or set PYTHON_COMMAND." >&2
  exit 1
}
command -v node >/dev/null 2>&1 || {
  echo "ERROR: Install Node.js 22." >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  echo "ERROR: Install npm." >&2
  exit 1
}

python_version="$($PYTHON_COMMAND -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "$python_version" == "3.13" ]] || {
  echo "ERROR: Python 3.13 is required. Found $python_version." >&2
  exit 1
}

node_major="$(node -p 'process.versions.node.split(".")[0]')"
[[ "$node_major" == "22" ]] || {
  echo "ERROR: Node.js 22 is required. Found $(node --version)." >&2
  exit 1
}

"$PYTHON_COMMAND" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
npm ci --prefix mission-control

echo "PASS: The development environment is ready."
echo "Next: ./scripts/bmas test"
