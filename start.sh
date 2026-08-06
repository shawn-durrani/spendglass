#!/usr/bin/env bash
# Launcher: creates .venv, installs deps when they change, refuses to start a
# second instance on the port it is about to use, prints the recovery secret,
# serves the UI.
set -euo pipefail
cd "$(dirname "$0")"

# Same variable the server reads, so the conflict check, the message and
# the bind can never disagree.
PORT="${SPENDGLASS_UI_PORT:-8903}"

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✖ port $PORT is already in use; spendglass is probably running." >&2
  echo "  Open http://127.0.0.1:$PORT, stop the other instance, or set" >&2
  echo "  SPENDGLASS_UI_PORT to a free port." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "creating .venv…"
  python3 -m venv .venv
fi

# Reinstall only when requirements.txt changed since the last install.
stamp=.venv/.requirements-stamp
if [ ! -f "$stamp" ] || ! cmp -s requirements.txt "$stamp"; then
  echo "installing dependencies…"
  .venv/bin/pip -q install -r requirements.txt
  cp requirements.txt "$stamp"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — add your Redbark API key, then run a sync:"
  echo "  .venv/bin/python -m spendglass.sync"
fi

# .env holds live API keys, and cp above takes the umask (0644 on a default
# setup), so tighten it. Unconditional and on every start, so an .env created
# before this line existed gets repaired instead of staying world-readable.
chmod 600 .env

exec .venv/bin/python -m spendglass.ui
