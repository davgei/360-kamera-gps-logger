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

HEAD_SHA="$(runuser -u "$REPO_USER" -- git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
log "Done — code at $HEAD_SHA"
