#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${OPENPERPDESK_ENV_FILE:-$ROOT_DIR/.env}"
BACKUP_DIR="${OPENPERPDESK_BACKUP_DIR:-$ROOT_DIR/backups}"
COMPOSE=(docker compose --env-file "$ENV_FILE")

usage() {
  cat <<'EOF'
Usage: infra/openperpdesk.sh <command> [args]

Commands:
  up                 Build and start the stack
  down               Stop and remove the stack
  restart            Restart the stack
  status             Show Compose service status
  smoke              Check the web and API health endpoints
  backup             Checkpoint SQLite and copy a timestamped backup
  restore <file>     Replace SQLite from a local backup, then restart API
  logs [service]     Follow logs for the stack or one service
EOF
}

require_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    printf 'Missing env file: %s\n' "$ENV_FILE" >&2
    printf 'Create it with: cp .env.example .env\n' >&2
    exit 1
  fi

  local mode
  if mode="$(stat -c '%a' "$ENV_FILE" 2>/dev/null)"; then
    :
  else
    mode="$(stat -f '%Lp' "$ENV_FILE")"
  fi
  if [[ "$mode" != "600" ]]; then
    printf 'Refusing to use %s: permissions must be 600 (currently %s)\n' "$ENV_FILE" "$mode" >&2
    exit 1
  fi
}

compose() {
  "${COMPOSE[@]}" "$@"
}

web_origin() {
  local binding
  binding="$(compose port web 80 2>/dev/null | tail -n 1 || true)"
  if [[ -z "$binding" ]]; then
    printf 'http://127.0.0.1:8080'
    return
  fi
  printf 'http://%s' "${binding#*:}"
}

smoke() {
  local origin="${1:-$(web_origin)}"
  curl --fail --silent --show-error --max-time 10 "$origin/" >/dev/null
  curl --fail --silent --show-error --max-time 10 "$origin/api/v1/health" >/dev/null
  curl --fail --silent --show-error --max-time 10 "$origin/api/v1/health/readiness" >/dev/null
  printf 'Smoke check passed: web and API health endpoints are reachable.\n'
}

backup() {
  mkdir -p "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"
  local stamp destination
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  destination="$BACKUP_DIR/openperpdesk-$stamp.sqlite3"
  compose exec -T api python -c \
    'import os, sqlite3; path=os.path.join(os.getenv("DATA_DIR", "/data"), "openperpdesk.sqlite3"); connection=sqlite3.connect(path); result=connection.execute("PRAGMA integrity_check").fetchone()[0]; connection.execute("PRAGMA wal_checkpoint(TRUNCATE)"); connection.close(); raise SystemExit(0 if result == "ok" else result)'
  compose cp api:/data/openperpdesk.sqlite3 "$destination"
  chmod 600 "$destination"
  printf 'Backup created: %s\n' "$destination"
}

restore() {
  local source="${1:-}"
  if [[ -z "$source" || ! -f "$source" ]]; then
    printf 'Usage: %s restore <local sqlite backup>\n' "$0" >&2
    exit 1
  fi
  case "$source" in
    *.sqlite3) ;;
    *)
      printf 'Refusing to restore a non-SQLite backup: %s\n' "$source" >&2
      exit 1
      ;;
  esac
  compose stop api
  compose cp "$source" api:/data/openperpdesk.sqlite3
  compose start api
  smoke
  printf 'Restore completed from: %s\n' "$source"
}

main() {
  local command="${1:-}"
  shift || true
  if [[ "$command" == "-h" || "$command" == "--help" || -z "$command" ]]; then
    usage
    return 0
  fi

  require_env
  cd "$ROOT_DIR"

  case "$command" in
    up)
      compose up -d --build
      ;;
    down)
      compose down
      ;;
    restart)
      compose up -d --build
      ;;
    status)
      compose ps
      ;;
    smoke)
      smoke "$@"
      ;;
    backup)
      backup
      ;;
    restore)
      restore "$@"
      ;;
    logs)
      compose logs -f --tail=200 "$@"
      ;;
    *)
      printf 'Unknown command: %s\n\n' "$command" >&2
      usage >&2
      return 2
      ;;
  esac
}

main "$@"
