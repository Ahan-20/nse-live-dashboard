#!/bin/bash
# Double-click this file in Finder to start the dashboard with intraday on.
# Lives in ~/Downloads/nse-live/. macOS asks for permission the first time;
# allow it.

set -e
cd "$(dirname "$0")"

echo "════════════════════════════════════════════════════════════"
echo "  NSE Live Dashboard — local intraday mode"
echo "════════════════════════════════════════════════════════════"
echo

# Sanity: .env must exist (otherwise Groww credentials aren't loaded).
if [ ! -f .env ]; then
  echo "❌ No .env file found in $(pwd)"
  echo
  echo "   Run this once to set it up:"
  echo "       cp .env.example .env"
  echo "       open .env       # then fill in GROWW_API_KEY + GROWW_API_SECRET"
  echo
  echo "   Press any key to close…"
  read -n1 -s; exit 1
fi

# One-time dependency: pyotp (for Groww's TOTP auth). Stdlib otherwise.
if ! python3 -c "import pyotp" >/dev/null 2>&1; then
  echo "📦 Installing pyotp (one-time)…"
  python3 -m pip install --user --quiet pyotp || {
    echo "❌ Couldn't install pyotp. Run manually: pip3 install pyotp"
    read -n1 -s; exit 1
  }
fi

# Open the browser after the server has had a moment to bind.
(
  sleep 2
  open "http://localhost:8787/"
) &

echo
echo "Starting server. Close this window to stop."
echo "Tip: leave the window open in the background while you trade."
echo
exec python3 server.py
