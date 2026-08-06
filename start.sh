#!/usr/bin/env bash
# Launcher — creates .venv, installs deps when they change, refuses to start a
# second instance on port 8903, prints the recovery secret, serves the UI.
set -euo pipefail
cd "$(dirname "$0")"

PORT=8903

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✖ port $PORT is already in use — spendglass is probably running." >&2
  echo "  Open http://127.0.0.1:$PORT or stop the other instance first." >&2
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

exec .venv/bin/python -m spendglass.ui
