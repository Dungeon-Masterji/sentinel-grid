#!/usr/bin/env bash
# Expose the local n8n webhook to the internet and print the URL to paste into
# N8N_WEBHOOK_URL (Azure App Setting / GitHub secret). Uses ngrok if installed, else
# cloudflared (quick tunnel, no account). Ctrl-C stops the tunnel.
#
#   make demo-tunnel
set -euo pipefail
PORT="${N8N_PORT:-5678}"
HOOK_PATH="/webhook/sentinel-grid/alert"

if ! curl -fsS -o /dev/null "http://localhost:${PORT}/healthz"; then
  echo "n8n is not answering on localhost:${PORT}. Start it first: make n8n-up" >&2
  exit 1
fi

print_url() {
  local base="$1"
  echo
  echo "  n8n webhook (public):  ${base}${HOOK_PATH}"
  echo
  echo "  Azure:   az webapp config appsettings set -n sentinel-grid-api -g <resource-group> \\"
  echo "             --settings N8N_WEBHOOK_URL=${base}${HOOK_PATH}"
  echo "  GitHub:  gh secret set N8N_WEBHOOK_URL --body ${base}${HOOK_PATH}"
  echo
  echo "  Note: n8n calls back the API at the payload's callback_base (the Azure API when the"
  echo "  alert came from Azure), so no second tunnel is needed for the API."
  echo
}

if command -v ngrok >/dev/null 2>&1; then
  ngrok http "${PORT}" --log=stdout >/tmp/sentinel-ngrok.log 2>&1 &
  pid=$!
  trap 'kill ${pid} 2>/dev/null' EXIT
  for _ in $(seq 1 30); do
    url=$(curl -fsS http://127.0.0.1:4040/api/tunnels 2>/dev/null |
          python3 -c "import json,sys; t=[x['public_url'] for x in json.load(sys.stdin)['tunnels'] if x['public_url'].startswith('https')]; print(t[0] if t else '')" || true)
    [ -n "${url}" ] && break
    sleep 1
  done
  [ -z "${url:-}" ] && { echo "ngrok did not report a tunnel; see /tmp/sentinel-ngrok.log" >&2; exit 1; }
  print_url "${url}"
  wait "${pid}"
elif command -v cloudflared >/dev/null 2>&1; then
  log=/tmp/sentinel-cloudflared.log
  cloudflared tunnel --url "http://localhost:${PORT}" >"${log}" 2>&1 &
  pid=$!
  trap 'kill ${pid} 2>/dev/null' EXIT
  for _ in $(seq 1 30); do
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "${log}" | head -1 || true)
    [ -n "${url}" ] && break
    sleep 1
  done
  [ -z "${url:-}" ] && { echo "cloudflared did not report a URL; see ${log}" >&2; exit 1; }
  print_url "${url}"
  wait "${pid}"
else
  echo "Need ngrok (brew install ngrok/ngrok/ngrok) or cloudflared (brew install cloudflared)." >&2
  exit 1
fi
