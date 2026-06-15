#!/usr/bin/env bash
# Full clean rebuild and restart of all bMAS services
set -euo pipefail

cd /opt/bmas

echo "=== Stopping all containers ==="
docker compose down --remove-orphans 2>&1 || true

echo ""
echo "=== Rebuilding all images (no cache) ==="
docker compose build --no-cache 2>&1

echo ""
echo "=== Starting all services ==="
docker compose up -d 2>&1

echo ""
echo "=== Waiting for health checks (30s) ==="
sleep 30

echo ""
echo "=== Container status ==="
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>&1

echo ""
echo "=== Quick API check ==="
curl -s -o /dev/null -w "Dashboard: HTTP %{http_code}\n" http://localhost:9321/ 2>&1 || echo "Dashboard: not responding yet"
curl -s -o /dev/null -w "Daemon API: HTTP %{http_code}\n" http://localhost:9000/tasks?limit=1 2>&1 || echo "Daemon: not responding yet"

echo ""
echo "=== REBUILD COMPLETE ==="
