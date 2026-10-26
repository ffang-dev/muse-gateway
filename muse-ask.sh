#!/usr/bin/env bash
# muse-ask: talk to your muse.ai agent straight from the ai-server shell.
#   muse-ask "summarise my inbox"
#   MUSE_THREAD=<side chat id> MUSE_WAIT=240 muse-ask "..."
# Needs flags beyond that? Call ~/muse-venv/bin/muse-cli send directly.
set -uo pipefail
VENV="${MUSE_VENV:-$HOME/muse-venv}"
BIN="$VENV/bin/muse-cli"
[ -x "$BIN" ] || { echo "muse-cli missing at $BIN"; exit 3; }
[ $# -ge 1 ] || { echo "usage: muse-ask <text>"; exit 2; }

ARGS=(send "$1" --wait "${MUSE_WAIT:-180}")
[ -n "${MUSE_THREAD:-}" ] && ARGS+=(--thread "$MUSE_THREAD")
MUSE_NO_UPDATE_CHECK=1 "$BIN" "${ARGS[@]}"
