#!/usr/bin/env bash
set -euo pipefail

# Run ONCE on a fresh Raspberry Pi, as root, from inside the cloned repo:
#
#   git clone https://github.com/davgei/360-kamera-gps-logger.git
#   cd 360-kamera-gps-logger
#   sudo deploy/bootstrap.sh <TEAMVIEWER_ASSIGNMENT_TOKEN>
#
# It installs git + TeamViewer Host, assigns the device to your TeamViewer
# account, and installs a boot service. After this, every boot pulls the latest
# code and keeps TeamViewer online — no further manual steps.

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0 <teamviewer-token>" >&2
  exit 1
fi

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
REPO_USER="$(stat -c '%U' "$REPO_DIR")"
TOKEN_FILE="$DEPLOY_DIR/teamviewer.token"

# TeamViewer token resolution: argument > env var > token file.
TOKEN="${1:-${TEAMVIEWER_ASSIGNMENT_TOKEN:-}}"
if [[ -z "$TOKEN" && -f "$TOKEN_FILE" ]]; then
  TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
fi

log() { echo "[bootstrap] $*"; }
log "Repo:     $REPO_DIR"
log "Owned by: $REPO_USER"

log "Installing git + helpers"
apt-get update
apt-get install -y git ca-certificates curl

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

log "Installing boot service"
UNIT="/etc/systemd/system/360logger-boot.service"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__REPO_USER__|$REPO_USER|g" \
  "$DEPLOY_DIR/systemd/360logger-boot.service" > "$UNIT"

chmod +x "$DEPLOY_DIR"/*.sh
systemctl daemon-reload
systemctl enable 360logger-boot.service

log "Done. Self-update + TeamViewer now run on every boot."
log "Run it now without rebooting:  sudo systemctl start 360logger-boot.service"
log "Check logs:                    journalctl -u 360logger-boot.service -b"
