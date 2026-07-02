#!/usr/bin/env bash
set -euo pipefail

# Run ONCE on a fresh Raspberry Pi, as root, from inside the cloned repo:
#
#   git clone https://github.com/davgei/360-kamera-gps-logger.git
#   cd 360-kamera-gps-logger
#   sudo deploy/bootstrap.sh <TEAMVIEWER_ASSIGNMENT_TOKEN>
#
# It installs git + TeamViewer Host, assigns the device to your TeamViewer
# account, and installs two boot services:
#   360logger-boot.service  — pulls the latest code + keeps TeamViewer online
#   360logger-app.service   — runs the logger app (see deploy/app.env)
# After this, the Pi sets itself up on every boot — no further manual steps.

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0 <teamviewer-token>" >&2
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
REPO_USER="$(stat -c '%U' "$REPO_DIR")"
TOKEN_FILE="$DEPLOY_DIR/teamviewer.token"

# TeamViewer token resolution: argument > env var > token file.
# The token is never stored in the repo (teamviewer.token is git-ignored).
TOKEN="${1:-${TEAMVIEWER_ASSIGNMENT_TOKEN:-}}"
if [[ -z "$TOKEN" && -f "$TOKEN_FILE" ]]; then
  TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
fi

log() { echo "[bootstrap] $*"; }
log "Repo:     $REPO_DIR"
log "Owned by: $REPO_USER"

log "Installing git, Python tooling + helpers"
apt-get update
apt-get install -y git ca-certificates curl python3 python3-pip python3-venv

log "Installing TeamViewer Host"
if ! command -v teamviewer >/dev/null 2>&1; then
  ARCH="$(dpkg --print-architecture)"   # armhf (32-bit) or arm64 (64-bit)
  DEB="/tmp/teamviewer-host_${ARCH}.deb"
  log "  architecture: $ARCH"
  curl -fsSL -o "$DEB" "https://download.teamviewer.com/download/linux/teamviewer-host_${ARCH}.deb"
  apt-get install -y "$DEB"
  rm -f "$DEB"
else
  log "  already installed"
fi

log "Enabling TeamViewer daemon"
teamviewer daemon enable || true
teamviewer daemon start  || true

STATE_DIR="/var/lib/360logger"
MARKER="$STATE_DIR/teamviewer-assigned"
mkdir -p "$STATE_DIR"

if [[ -f "$MARKER" ]]; then
  log "TeamViewer already assigned — skipping"
elif [[ -n "$TOKEN" ]]; then
  log "Assigning device to TeamViewer account"
  if teamviewer assignment --token "$TOKEN" --grant-easy-access; then
    touch "$MARKER"
    log "  assignment succeeded"
  else
    log "  assignment FAILED — see deploy/README.md (EULA may need one-time acceptance)"
  fi
else
  log "No TeamViewer token provided and device not yet assigned."
  log "Re-run with a token: sudo deploy/bootstrap.sh <token>"
  log "(or place it in deploy/teamviewer.token)"
fi

log "Adding $REPO_USER to gpio + input groups (LEDs + mouse button)"
usermod -aG gpio,input "$REPO_USER" || true

log "Installing systemd services"
for unit in 360logger-boot 360logger-app 360logger-upload 360logger-photo; do
  sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__REPO_USER__|$REPO_USER|g" \
    "$DEPLOY_DIR/systemd/${unit}.service" > "/etc/systemd/system/${unit}.service"
done

chmod +x "$DEPLOY_DIR"/*.sh
systemctl daemon-reload
systemctl enable 360logger-boot.service 360logger-app.service 360logger-upload.service 360logger-photo.service

log "Done. On every boot the Pi now pulls the latest code, keeps TeamViewer"
log "online, and (re)starts the logger app from deploy/app.env."
log "Run it now without rebooting:"
log "  sudo systemctl start 360logger-boot.service 360logger-app.service"
log "Check logs:"
log "  journalctl -u 360logger-boot.service -b"
log "  journalctl -u 360logger-app.service  -b"
