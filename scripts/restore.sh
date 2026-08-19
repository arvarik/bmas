#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

ARCHIVE="${1:-}"
CONFIRMATION="${2:-}"

if [[ -z "$ARCHIVE" || "$CONFIRMATION" != "--yes" ]]; then
  echo "Usage: ./scripts/bmas restore ARCHIVE --yes" >&2
  echo "This command replaces the current SQLite, upload, and artifact data." >&2
  exit 1
fi

[[ -f "$ARCHIVE" ]] || { echo "ERROR: Archive does not exist: $ARCHIVE" >&2; exit 1; }
tar -tzf "$ARCHIVE" >/dev/null

ARCHIVE_DIR="$(cd "$(dirname "$ARCHIVE")" && pwd)"
ARCHIVE_NAME="$(basename "$ARCHIVE")"

echo "Creating a safety backup before the restore."
"$SCRIPT_DIR/backup.sh" backups/pre-restore

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
  -v "$ARCHIVE_DIR:/restore:ro" \
  daemon sh -c "
    find /data -mindepth 1 -maxdepth 1 ! -name uploads ! -name output -exec rm -rf -- {} +
    find /data/uploads -mindepth 1 -exec rm -rf -- {} +
    find /data/output -mindepth 1 -exec rm -rf -- {} +
    tar -xzf '/restore/$ARCHIVE_NAME' -C /data
  "

restart_services
trap - EXIT

if [[ "$restart_daemon" == true && "$restart_dashboard" == true ]]; then
  "$SCRIPT_DIR/healthcheck.sh" --wait 180
fi

echo "PASS: Restored $ARCHIVE"
