#!/usr/bin/env bash
# Interactive setup for Kite Connect credentials on the VPS.
# Prompts for API Key + Secret, writes them into the env file, restarts the
# service. Uses < /dev/tty so `read` works even when piped from curl.
#
# Run on the VPS as root:
#   curl -sSL https://raw.githubusercontent.com/Ahan-20/nse-live-dashboard/main/deploy/configure-kite.sh | bash

set -euo pipefail

ENV_FILE="/etc/nse-live-dashboard/env"
SVC="nse-live-dashboard"
DEFAULT_KEY="2gllbtln1fp0tmie"      # from Ahan's screenshot; can be overridden

if [ "$(id -u)" != "0" ]; then
    echo "❌ Please run as root: sudo bash configure-kite.sh"
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  Kite Connect credentials setup"
echo "════════════════════════════════════════════════════════════════════"
echo ""
echo "Paste each credential when prompted. Enter alone accepts the default."
echo ""

# Read from /dev/tty explicitly so this works when run via 'curl | bash'.
read -p "API Key [default: $DEFAULT_KEY]: " API_KEY < /dev/tty
API_KEY="${API_KEY:-$DEFAULT_KEY}"

read -p "API Secret (paste from Kite console): " API_SECRET < /dev/tty
if [ -z "${API_SECRET:-}" ]; then
    echo "❌ API Secret is required. Aborting."
    exit 1
fi

# Ensure the /etc dir exists (should already from vps-setup.sh)
mkdir -p "$(dirname "$ENV_FILE")"

# Preserve any KITE_ACCESS_TOKEN we may already have — the daily browser
# login writes to this file too. Grab the existing token if present.
EXISTING_TOKEN=""
if [ -f "$ENV_FILE" ]; then
    EXISTING_TOKEN="$(grep -E '^KITE_ACCESS_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
fi

cat > "$ENV_FILE" << EOF
# NSE Live Dashboard — secrets. Written by configure-kite.sh.
# Do NOT commit. Do NOT paste into git or chat.
PORT=8787

# --- Kite Connect (Zerodha) ---
INTRADAY_PROVIDER=kite
KITE_API_KEY=$API_KEY
KITE_API_SECRET=$API_SECRET
$( [ -n "$EXISTING_TOKEN" ] && echo "KITE_ACCESS_TOKEN=$EXISTING_TOKEN" || echo "# KITE_ACCESS_TOKEN=  # populated by /kite/login browser flow" )

# --- Groww (unused — kept as commented reference) ---
# GROWW_API_KEY=
# GROWW_API_SECRET=
# GROWW_REGISTERED_IP=
EOF
chmod 600 "$ENV_FILE"

echo ""
echo "→ Restarting $SVC ..."
systemctl restart "$SVC"
sleep 3

if systemctl is-active --quiet "$SVC"; then
    echo ""
    echo "✅ Service restarted with Kite credentials in place."
    echo ""
    echo "  Verify from an external check:"
    echo "    curl -s https://139-59-76-126.sslip.io/api/tradefinder?universe=nifty50 \\"
    echo "      | python3 -c 'import sys,json;d=json.load(sys.stdin);print(\"intraday_connected:\",d.get(\"intraday_connected\"),\"| provider:\",d.get(\"intraday_provider\"))'"
    echo ""
    echo "  Then open this in your browser to complete the daily Kite login:"
    echo ""
    echo "      https://139-59-76-126.sslip.io/kite/login"
    echo ""
    echo "  Zerodha will ask you to sign in and Authorize."
    echo "  When you see the green ✓ page, come back to Claude and say: connected"
else
    echo ""
    echo "❌ Service failed to start. Recent logs:"
    echo ""
    journalctl -u "$SVC" -n 25 --no-pager
    exit 1
fi
