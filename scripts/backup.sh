#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

DESTINATION="${1:-backups}"
mkdir -p "$DESTINATION"
DESTINATION="$(cd "$DESTINATION" && pwd)"
ARCHIVE="bmas-data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

[[ -f bmas.yaml ]] || { echo "ERROR: Run ./scripts/bmas init first." >&2; exit 1; }
[[ -f .env ]] || { echo "ERROR: Run ./scripts/bmas init first." >&2; exit 1; }
docker compose config --quiet

running_services="$(docker compose ps --status running --services)"
restart_daemon=false
restart_dashboard=false
if printf '%s\n' "$running_services" | grep -qx daemon; then
  restart_daemon=true
fi
if printf '%s\n' "$running_services" | grep -qx dashboard; then
  restart_dashboard=true
fi

restart_services() {
  if [[ "$restart_daemon" == true ]]; then
    docker compose start daemon >/dev/null
  fi
  if [[ "$restart_dashboard" == true ]]; then
    docker compose start dashboard >/dev/null
  fi
}

trap restart_services EXIT

if [[ "$restart_dashboard" == true ]]; then
  docker compose stop dashboard >/dev/null
fi
if [[ "$restart_daemon" == true ]]; then
  docker compose stop daemon >/dev/null
fi

docker compose run --rm --no-deps --user 0 \
  -v "$DESTINATION:/backup" \
  daemon sh -c "tar -czf '/backup/$ARCHIVE' -C /data . && chown '$HOST_UID:$HOST_GID' '/backup/$ARCHIVE'"

restart_services
trap - EXIT

if [[ "$restart_daemon" == true && "$restart_dashboard" == true ]]; then
  "$SCRIPT_DIR/healthcheck.sh" --wait 180
fi

echo "PASS: Created $DESTINATION/$ARCHIVE"
echo "Copy this archive to storage outside the Docker host."
