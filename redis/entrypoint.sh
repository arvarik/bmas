#!/bin/sh
# ─────────────────────────────────────────────────────────────────────
# Redis Entrypoint — renders redis.conf from template
# ─────────────────────────────────────────────────────────────────────
# Substitutes $REDIS_PASSWORD in the template and starts Redis.
# ─────────────────────────────────────────────────────────────────────

set -e

TEMPLATE="/etc/redis/redis.conf.template"
OUTPUT="/tmp/redis.conf"

if [ ! -f "$TEMPLATE" ]; then
  echo "FATAL: Redis config template not found at $TEMPLATE"
  exit 1
fi

if [ -z "$REDIS_PASSWORD" ]; then
  echo "FATAL: REDIS_PASSWORD environment variable is required"
  exit 1
fi

# envsubst is not available in redis:alpine, use sed instead
sed "s|\${REDIS_PASSWORD}|${REDIS_PASSWORD}|g" "$TEMPLATE" > "$OUTPUT"
echo "✓ Redis config rendered from template."

exec redis-server "$OUTPUT"
