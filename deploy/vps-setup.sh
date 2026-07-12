#!/usr/bin/env bash
# One-shot setup for the NSE Live Dashboard on a fresh Ubuntu 24.04 VPS.
#
# What it does:
#   1. Installs Python, git, ufw, and pyotp
#   2. Clones the repo to /opt/nse-live-dashboard
#   3. Writes a systemd unit that runs server.py as an unprivileged user,
#      auto-restarts on crash, and starts on boot
#   4. Opens ports 22 (SSH) and 8787 (dashboard) in ufw
#   5. Creates /etc/nse-live-dashboard/env for secrets (Kite / Groww / etc.),
#      chmod 600 so only root can read
#   6. Starts the service
#
# Idempotent — safe to re-run to pick up a new commit or config change.
#
# Usage (as root on the VPS):
#   curl -sSL https://raw.githubusercontent.com/Ahan-20/nse-live-dashboard/main/deploy/vps-setup.sh | bash
#   # or, if you cloned first:
#   sudo bash deploy/vps-setup.sh

set -euo pipefail
REPO_URL="${REPO_URL:-https://github.com/Ahan-20/nse-live-dashboard.git}"
APP_DIR="/opt/nse-live-dashboard"
ENV_DIR="/etc/nse-live-dashboard"
ENV_FILE="$ENV_DIR/env"
SVC_USER="nselive"
SVC_NAME="nse-live-dashboard"

log(){ printf '\033[36m→\033[0m %s\n' "$*"; }
die(){ printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" != "0" ]; then
  die "Run this as root: sudo bash $0"
fi

log "Installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-venv git ufw ca-certificates >/dev/null

log "Creating service user '$SVC_USER'"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --shell /usr/sbin/nologin --home "$APP_DIR" "$SVC_USER"
fi

log "Cloning/updating repo at $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --quiet origin main
  git -C "$APP_DIR" reset --hard origin/main --quiet
else
  git clone --quiet "$REPO_URL" "$APP_DIR"
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

log "Installing Python dependencies (pyotp for TOTP; kiteconnect for Kite)"
# We ship with pure stdlib, but pyotp + kiteconnect enable Groww + Kite paths.
pip3 install --break-system-packages --quiet pyotp kiteconnect

log "Preparing secrets file at $ENV_FILE"
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# NSE Live Dashboard — secrets. Loaded into the service environment.
# Do NOT commit. Do NOT paste into git or chat. chmod 600 by setup.
PORT=8787
# --- Kite Connect (Zerodha) ---
# INTRADAY_PROVIDER=kite
# KITE_API_KEY=
# KITE_API_SECRET=
# KITE_ACCESS_TOKEN=              # updated daily via /kite/login browser flow
# --- Groww (fallback if you ever go back to it) ---
# GROWW_API_KEY=
# GROWW_API_SECRET=
# GROWW_REGISTERED_IP=
EOF
  chmod 600 "$ENV_FILE"
  log "Blank secrets template written. Edit with:  sudo nano $ENV_FILE"
else
  log "Existing secrets kept at $ENV_FILE (not overwritten)"
fi

log "Writing systemd unit"
cat > "/etc/systemd/system/${SVC_NAME}.service" <<EOF
[Unit]
Description=NSE Live Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $APP_DIR/server.py
Restart=on-failure
RestartSec=5
# Basic hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

log "Configuring firewall (ufw)"
ufw --force enable >/dev/null
ufw allow 22/tcp    >/dev/null    # SSH
ufw allow 8787/tcp  >/dev/null    # Dashboard

log "Enabling + (re)starting service"
systemctl daemon-reload
systemctl enable --quiet "$SVC_NAME"
systemctl restart "$SVC_NAME"

sleep 2
if systemctl is-active --quiet "$SVC_NAME"; then
  log "✅ Service is up"
else
  systemctl status "$SVC_NAME" --no-pager || true
  die "Service failed to start — check the log above"
fi

IP=$(hostname -I | awk '{print $1}')
cat <<EOF

════════════════════════════════════════════════════════════════════
  ✅ Setup complete.

  Dashboard:      http://$IP:8787
  Secrets:        sudo nano $ENV_FILE   (add KITE_* then restart)
  Restart:        sudo systemctl restart $SVC_NAME
  Live logs:      sudo journalctl -u $SVC_NAME -f
  Pull updates:   cd $APP_DIR && sudo -u $SVC_USER git pull && sudo systemctl restart $SVC_NAME
════════════════════════════════════════════════════════════════════
EOF
