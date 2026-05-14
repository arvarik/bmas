#!/bin/sh
# ─────────────────────────────────────────────────────────────────────
# LiteLLM Entrypoint — generates config.yaml, then starts the proxy
# ─────────────────────────────────────────────────────────────────────

set -e

BMAS_CONFIG="${BMAS_CONFIG:-/etc/bmas/bmas.yaml}"
OUTPUT="/tmp/litellm_config.yaml"

if [ ! -f "$BMAS_CONFIG" ]; then
  echo "FATAL: bmas.yaml not found at $BMAS_CONFIG"
  exit 1
fi

python3 /app/generate_config.py --config "$BMAS_CONFIG" --output "$OUTPUT"

echo "Starting LiteLLM proxy..."
exec litellm --config "$OUTPUT" --port 4000
