#!/usr/bin/env bash
# Install (or refresh) the launchd user agent that keeps Spendglass running on
# macOS (#28): it starts the service at login, restarts it within seconds if
# it ever exits, and brings it back after a reboot — so a crash or a forgotten
# start no longer leaves the app dark until someone notices.
#
# Idempotent: safe to re-run after a `git pull`. It takes over cleanly —
# unloading any prior agent and stopping a hand-started instance first — so
# there is only ever ONE owner of the service process.
#
# Usage:  ops/install-supervisor.sh
# Undo:   launchctl bootout gui/$(id -u)/dev.spendglass.server
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd -P)"
LABEL="dev.spendglass.server"
TEMPLATE="ops/${LABEL}.plist.template"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

# Same port-resolution rule as start.sh: shell env first, then .env (where a
# supervised install actually sets it), then the default — so the
# kill-the-hand-started-instance step targets the port really bound.
_env_port() {
  [ -f .env ] && sed -n "s/^${1}=//p" .env | tail -1 || true
}
PORT="${SPENDGLASS_UI_PORT:-$(_env_port SPENDGLASS_UI_PORT)}"
PORT="${PORT:-8903}"

[ -f "$TEMPLATE" ] || { echo "✗ template not found: $TEMPLATE" >&2; exit 1; }

AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin"

mkdir -p "$HOME/Library/LaunchAgents" "$REPO_DIR/data"
sed -e "s#{{REPO_DIR}}#${REPO_DIR}#g" \
    -e "s#{{HOME}}#${HOME}#g" \
    -e "s#{{PATH}}#${AGENT_PATH}#g" \
    "$TEMPLATE" > "$DEST"

plutil -lint "$DEST" >/dev/null
if grep -q '{{' "$DEST"; then
  echo "✗ unsubstituted placeholder left in $DEST" >&2; exit 1
fi

# Sole owner: drop any prior agent, then stop a hand-started instance still
# holding the port, before bootstrapping (RunAtLoad starts the real one).
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
sleep 1
if PID="$(lsof -tnP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)"; then
  echo "· stopping hand-started instance (pid $PID) so the agent owns the port"
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 15); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$PID" 2>/dev/null || true
fi
launchctl bootstrap "$DOMAIN" "$DEST"
launchctl enable "$DOMAIN/$LABEL"

echo "✓ supervisor installed. Spendglass will self-restart and survive reboots."
echo "  status:  launchctl print $DOMAIN/$LABEL | grep -iE 'state|pid|program'"
