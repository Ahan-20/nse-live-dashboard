#!/usr/bin/env bash
# Smoke-tests Kite Connect after you've set KITE_API_KEY / KITE_API_SECRET /
# KITE_ACCESS_TOKEN in /etc/nse-live-dashboard/env. Verifies:
#   1. Auth works (fetches your profile)
#   2. Live LTP works (RELIANCE)
#   3. Historical candles work (RELIANCE last 5 daily bars)
#   4. Instrument-token lookup works (RELIANCE-EQ)
#
# Run as root on the VPS:  sudo bash /opt/nse-live-dashboard/deploy/check-kite.sh

set -euo pipefail
ENV_FILE="/etc/nse-live-dashboard/env"
[ -f "$ENV_FILE" ] || { echo "❌ $ENV_FILE not found — run vps-setup.sh first"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${KITE_API_KEY:?KITE_API_KEY not set in $ENV_FILE}"
: "${KITE_ACCESS_TOKEN:?KITE_ACCESS_TOKEN not set — do the browser login flow first}"

H=(-H "X-Kite-Version: 3" -H "Authorization: token $KITE_API_KEY:$KITE_ACCESS_TOKEN")

hr(){ echo; echo "════ $* ════"; }

hr "1. Profile (auth check)"
curl -sS "${H[@]}" https://api.kite.trade/user/profile \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('user:',d.get('data',{}).get('user_name'),'| broker:',d.get('data',{}).get('broker'))" \
  || { echo "❌ auth failed. Refresh access token via /kite/login."; exit 1; }

hr "2. Live LTP (RELIANCE)"
curl -sS "${H[@]}" "https://api.kite.trade/quote/ltp?i=NSE:RELIANCE" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('data',{}).get('NSE:RELIANCE'))"

hr "3. Instrument-token lookup"
TOKEN=$(curl -sS "${H[@]}" "https://api.kite.trade/instruments/NSE" \
        | awk -F, 'NR>1 && $3=="RELIANCE" && $6=="EQ" {print $1; exit}')
echo "RELIANCE instrument_token = $TOKEN"
[ -z "$TOKEN" ] && { echo "❌ couldn't find RELIANCE token"; exit 1; }

hr "4. Historical daily bars (last 7 days)"
FROM=$(date -d '7 days ago' +%Y-%m-%d)
TO=$(date +%Y-%m-%d)
curl -sS "${H[@]}" "https://api.kite.trade/instruments/historical/$TOKEN/day?from=$FROM&to=$TO" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);c=d.get('data',{}).get('candles',[]);print(f'{len(c)} bars');[print(' ',b) for b in c[-3:]]"

echo
echo "✅ All Kite endpoints OK."
