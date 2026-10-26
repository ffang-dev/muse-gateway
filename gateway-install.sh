#!/usr/bin/env bash
# Install the muse HTTP gateway as a systemd service on port 8081.
# The access token is typed here and never leaves this machine.
set -uo pipefail

VENV="${MUSE_VENV:-$HOME/muse-venv}"
PORT="${MUSE_GATEWAY_PORT:-8081}"
UNIT=muse-gateway

echo "== preflight =="
[ -x "$HOME/muse-gateway.py" ] || { echo "FAIL: ~/muse-gateway.py missing (run install.sh step 6)"; exit 1; }
[ -x "$HOME/.config/muse-cli/cookies.txt" ] || [ -f "$HOME/.config/muse-cli/cookies.txt" ] || {
  echo "WARN: no cookies yet; the gateway will return errors until you add them."; }
"$VENV/bin/python" -c 'import sys;assert sys.version_info>=(3,10)' 2>/dev/null || { echo "FAIL: venv python"; exit 1; }

read -r -s -p "gateway access token (>=16 chars, type it yourself): " TOK; echo
[ "${#TOK}" -ge 16 ] || { echo "FAIL: token too short"; exit 1; }

cat > "$HOME/.muse-gateway.env" <<ENV
MUSE_GATEWAY_TOKEN=$TOK
MUSE_CLI=$VENV/bin/muse-cli
PORT=$PORT
MUSE_DEFAULT_WAIT=180
MUSE_HOME=$HOME
ENV
chmod 600 "$HOME/.muse-gateway.env"
echo "  wrote $HOME/.muse-gateway.env (mode 600)"

sudo tee /etc/systemd/system/$UNIT.service >/dev/null <<UNIT
[Unit]
Description=muse.ai gateway in front of muse-cli
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$HOME
EnvironmentFile=$HOME/.muse-gateway.env
ExecStart=$VENV/bin/python $HOME/muse-gateway.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now $UNIT
sleep 2
echo "ACTIVE=$(systemctl is-active $UNIT)"
echo "--- health ---"
curl -sS -m 8 "http://127.0.0.1:$PORT/v1/health"; echo
echo
echo "public url:  https://$PORT-\$(cat /etc/hostname 2>/dev/null).instabox.tech/v1/health"
echo "  (your box publishes ports as https://<port>-<machineid>.instabox.tech)"
