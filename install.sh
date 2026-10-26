#!/usr/bin/env bash
# muse-cli on InstaBox ai-server - phase 0 install + go/no-go preflight
# Safe to re-run. Contains no secrets.
set -uo pipefail

VENV="${MUSE_VENV:-$HOME/muse-venv}"
CFG_DIR="$HOME/.config/muse-cli"
PY_MIN=10

echo "== 1. disk =="
df -h "$HOME" | tail -1 | awk '{print "  avail="$4" used="$5" on "$6}'
AVAIL_KB=$(df -Pk "$HOME" | tail -1 | awk '{print $4}')
if [ "${AVAIL_KB:-0}" -lt 300000 ]; then
  echo "  FAIL: less than 300MB free. Refusing to install."
  exit 1
fi
echo "  PASS"

echo "== 2. python =="
PY=python3
command -v $PY >/dev/null || { echo "  FAIL: no python3"; exit 1; }
PYVER=$($PY -c 'import sys;print("%d.%d"%sys.version_info[:2])')
PYOK=$($PY -c 'import sys;print(1 if sys.version_info>=(3,$PY_MIN) else 0)')
echo "  found $PY $PYVER (need >=3.$PY_MIN)"
[ "$PYOK" = 1 ] || { echo "  FAIL: python too old"; exit 1; }

echo "== 3. edge reachability from this box (NO cookies needed) =="
# 生死线: Meta resets non-browser clients at the edge. If we get any HTTP
# status back at all from a datacenter IP, the IP itself is not blocked.
for host in muse.ai hatch.metaaivm.com; do
  r=$(curl -s -o /dev/null -m 15 -w '%{http_code}/%{remote_ip}' "https://$host/" 2>/dev/null)
  code=${r%%/*}
  if [ "$code" = "000" ] || [ -z "$code" ]; then
    echo "  FAIL $host: no response (blocked or reset at edge)"
  else
    echo "  PASS $host: HTTP $code (via $r)"
  fi
done
echo "  our egress IP: $(curl -s -m 10 https://api.ipify.org || echo unknown)"

echo "== 4. venv + install =="
if [ ! -x "$VENV/bin/pip" ]; then
  $PY -m venv "$VENV" 2>/dev/null || {
    echo "  venv missing, need: sudo apt-get install -y python3-venv"
    exit 1
  }
fi
"$VENV/bin/pip" install --no-cache-dir --quiet 'muse-cli>=0.3.2' || { echo "  FAIL: pip install"; exit 1; }
echo "  installed: $("$VENV/bin/python" -c 'import muse_cli,sys;print(getattr(muse_cli,"__version__","?"))' 2>/dev/null)" -> $VENV"
echo "  disk now: $(du -sh "$VENV" 2>/dev/null | cut -f1)"

echo "== 5. config skeleton =="
mkdir -p "$CFG_DIR"
[ -f "$CFG_DIR/cookies.txt" ] || echo "  (cookies.txt not present yet - that is the next step)"
echo "  config dir: $CFG_DIR"

echo "== 6. gateway files =="
RAW="${MUSE_RAW:-https://raw.githubusercontent.com/ffang-dev/muse-gateway/main}"
for f in muse-gateway.py gateway-install.sh muse-ask.sh; do
  if curl -fsSL -m 30 "$RAW/$f" -o "$HOME/$f.new"; then
    mv "$HOME/$f.new" "$HOME/$f"
    echo "  ok $HOME/$f ($(wc -c < "$HOME/$f") bytes)"
  else
    echo "  skip $f (could not fetch from $RAW)"
    rm -f "$HOME/$f.new"
  fi
done
chmod +x "$HOME/muse-ask.sh" "$HOME/gateway-install.sh" 2>/dev/null

echo
echo "== NEXT =="
echo "  A) put your muse.ai cookies at $CFG_DIR/cookies.txt   (see step 2 of the runbook)"
echo "  B) $VENV/bin/muse-cli status"
echo "  C) once status passes:  bash ~/gateway-install.sh"
