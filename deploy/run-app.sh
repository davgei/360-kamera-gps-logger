#!/usr/bin/env bash
set -euo pipefail

# ExecStart of 360logger-app.service. Reads deploy/app.env and launches the
# logger. If no command is configured yet, it exits cleanly (the service then
# stays idle instead of crash-looping).

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_ENV="$REPO_DIR/deploy/app.env"

APP_CMD=""
APP_WORKDIR=""
# shellcheck disable=SC1090
[[ -f "$APP_ENV" ]] && source "$APP_ENV"

log() { echo "[run-app] $*"; }

if [[ -z "$APP_CMD" ]]; then
  log "APP_CMD is empty in deploy/app.env — no logger configured yet. Nothing to run."
  exit 0
fi

cd "${APP_WORKDIR:-$REPO_DIR}"
log "Starting: $APP_CMD"
exec bash -c "$APP_CMD"
