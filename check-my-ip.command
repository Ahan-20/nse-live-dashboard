#!/bin/bash
# Double-click to check your current public IP and whether it has changed since
# the last time you ran it. Reboot your router, double-click again, and see if
# the IP is the same — if yes, your home connection is static enough for Groww.

cd "$(dirname "$0")"
echo "════════════════════════════════════════════════════════════"
echo "  Home IP stability check"
echo "════════════════════════════════════════════════════════════"
echo

CURR=$(curl -s --max-time 8 https://api.ipify.org || echo "")
if [ -z "$CURR" ]; then
  echo "❌ Could not reach ipify.org. Check your internet."
  read -n1 -s; exit 1
fi

echo "Current public IP   : $CURR"
if [ -f .last-ip ]; then
  LAST=$(cat .last-ip)
  echo "Last recorded IP    : $LAST"
  if [ "$CURR" = "$LAST" ]; then
    echo "✅ Same as last time — looks stable."
  else
    echo "⚠️  CHANGED since last check."
    echo "    If you registered the old IP with Groww, calls will fail until"
    echo "    you re-register $CURR (7-day lock applies)."
  fi
else
  echo "First run — saving this as your baseline."
fi
echo "$CURR" > .last-ip

echo
echo "Test: reboot your router, double-click this file again. If the IP is the"
echo "same after a reboot, your home IP is static enough for Groww. If it"
echo "changes, the local workflow won't work reliably — we'd need a VPS."
echo
echo "Press any key to close…"
read -n1 -s
