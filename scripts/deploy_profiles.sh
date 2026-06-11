#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────
# deploy_profiles.sh — Replicate bMAS Hermes profiles to agent nodes
# ────────────────────────────────────────────────────────────────────
# Reads node IPs from bmas.yaml (or accepts them as positional args),
# creates the 7 role profiles on each node via `hermes profile create`,
# then copies SOUL.md + config.yaml from agent/profiles/<role>/.
#
# Each profile's .env is symlinked to ~/.hermes/.env so API keys are
# shared without duplicating secrets.
#
# Usage:
#   bash scripts/deploy_profiles.sh                     # auto-read nodes from bmas.yaml
#   bash scripts/deploy_profiles.sh 192.168.4.103 ...   # explicit IPs
#
# Prerequisites:
#   - SSH access to each node as root (key-based auth)
#   - Hermes Agent installed on each node (hermes CLI available)
#   - This script must be run from the bMAS repo root (/opt/bmas)
#
# Spec: docs/proposals/12-hermes-and-node-topology.md §2.5–3
# ────────────────────────────────────────────────────────────────────

set -euo pipefail

PROFILES=(planner expert critic conflict_resolver cleaner decider universal)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROFILES_DIR="${REPO_ROOT}/agent/profiles"

# ── Resolve node IPs ──────────────────────────────────────────────────

if [ $# -gt 0 ]; then
    NODES=("$@")
else
    # Parse agent node IPs from bmas.yaml using Python for accuracy
    # (only top-level nodes[].host, not inference sub-hosts or control_plane)
    BMAS_YAML="${REPO_ROOT}/bmas.yaml"
    if [ ! -f "$BMAS_YAML" ]; then
        echo "❌ bmas.yaml not found at ${BMAS_YAML}"
        echo "   Run from repo root or pass node IPs as arguments."
        exit 1
    fi
    mapfile -t NODES < <(python3 -c "
import yaml, sys
with open('${BMAS_YAML}') as f:
    cfg = yaml.safe_load(f)
for n in cfg.get('nodes', []):
    h = n.get('host', '')
    if h:
        print(h)
")
    if [ ${#NODES[@]} -eq 0 ]; then
        echo "❌ No node IPs found in ${BMAS_YAML}"
        exit 1
    fi
fi

echo ""
echo "┌───────────────────────────────────────────────┐"
echo "│  bMAS Profile Deployment (doc 12 §2.5)        │"
echo "└───────────────────────────────────────────────┘"
echo "  Profiles: ${PROFILES[*]}"
echo "  Nodes:    ${NODES[*]}"
echo "  Source:   ${PROFILES_DIR}"
echo ""

# ── Validate local profile files exist ────────────────────────────────

for profile in "${PROFILES[@]}"; do
    for file in SOUL.md config.yaml; do
        if [ ! -f "${PROFILES_DIR}/${profile}/${file}" ]; then
            echo "❌ Missing: ${PROFILES_DIR}/${profile}/${file}"
            exit 1
        fi
    done
done
echo "  ✓ All profile source files present"
echo ""

# ── Deploy to each node ──────────────────────────────────────────────

ERRORS=0

for ip in "${NODES[@]}"; do
    echo "═══ Deploying to ${ip} ═══"

    # Verify SSH connectivity
    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "root@${ip}" 'true' 2>/dev/null; then
        echo "  ❌ Cannot SSH to root@${ip} — skipping"
        ERRORS=$((ERRORS + 1))
        continue
    fi

    # Verify hermes is installed
    if ! ssh "root@${ip}" 'command -v hermes >/dev/null 2>&1'; then
        echo "  ❌ hermes not found on ${ip} — skipping"
        ERRORS=$((ERRORS + 1))
        continue
    fi

    for profile in "${PROFILES[@]}"; do
        echo "  → ${profile}"

        # Create profile if it doesn't exist (idempotent)
        ssh "root@${ip}" "
            if [ -d ~/.hermes/profiles/${profile} ]; then
                echo '    (exists)'
            else
                hermes profile create ${profile} 2>/dev/null || true
                echo '    (created)'
            fi
        "

        # Copy SOUL.md and config.yaml
        PROFILE_DIR="/root/.hermes/profiles/${profile}"
        scp -q "${PROFILES_DIR}/${profile}/SOUL.md" "root@${ip}:${PROFILE_DIR}/SOUL.md"
        scp -q "${PROFILES_DIR}/${profile}/config.yaml" "root@${ip}:${PROFILE_DIR}/config.yaml"

        # Symlink .env to default profile's .env (shared API keys)
        ssh "root@${ip}" "
            if [ ! -e ${PROFILE_DIR}/.env ]; then
                ln -sf /root/.hermes/.env ${PROFILE_DIR}/.env
                echo '    (.env symlinked)'
            fi
        "
    done

    # Show profile list for verification
    echo ""
    echo "  ── hermes profile list ──"
    ssh "root@${ip}" 'hermes profile list 2>/dev/null' || echo "  (profile list unavailable)"
    echo ""
done

# ── Summary ───────────────────────────────────────────────────────────

echo "┌───────────────────────────────────────────────┐"
if [ $ERRORS -eq 0 ]; then
    echo "│  ✅ Profile deployment complete                │"
else
    echo "│  ⚠️  Deployment completed with ${ERRORS} error(s)      │"
fi
echo "└───────────────────────────────────────────────┘"
echo ""

exit $ERRORS
