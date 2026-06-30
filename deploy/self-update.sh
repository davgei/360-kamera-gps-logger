#!/usr/bin/env bash
set -euo pipefail

# Runs on every boot via 360logger-boot.service.
# REPO_DIR and REPO_USER are injected by the systemd unit; the fallbacks let
# you also run this by hand from inside the repo.

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO_USER="${REPO_USER:-$(stat -c '%U' "$REPO_DIR")}"

log() { echo "[self-update] $*"; }

log "Pulling latest code in $REPO_DIR (as $REPO_USER)"
if ! runuser -u "$REPO_USER" -- git -C "$REPO_DIR" pull --ff-only; then
  log "git pull skipped (offline, or no upstream yet) — continuing"
fi

log "Ensuring TeamViewer daemon is running"
teamviewer daemon start >/dev/null 2>&1 || true

# Install/refresh the logger app's Python dependencies into a venv if configured.
APP_REQUIREMENTS=""
APP_VENV="venv"
APP_ENV="$REPO_DIR/deploy/app.env"
# shellcheck disable=SC1090
[[ -f "$APP_ENV" ]] && source "$APP_ENV"
if [[ -n "$APP_REQUIREMENTS" && -f "$REPO_DIR/$APP_REQUIREMENTS" ]]; then
  VENV_DIR="$REPO_DIR/$APP_VENV"
  if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating virtualenv at $VENV_DIR"
    runuser -u "$REPO_USER" -- python3 -m venv "$VENV_DIR" || log "venv creation failed — continuing"
  fi
  log "Installing Python deps from $APP_REQUIREMENTS into $APP_VENV"
  runuser -u "$REPO_USER" -- "$VENV_DIR/bin/pip" install -r "$REPO_DIR/$APP_REQUIREMENTS" \
    || log "pip install failed — continuing"
fi

# Restart the app so it picks up the freshly pulled code.
log "Restarting logger app service"
systemctl restart 360logger-app.service || true

HEAD_SHA="$(runuser -u "$REPO_USER" -- git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
log "Done — code at $HEAD_SHA"
