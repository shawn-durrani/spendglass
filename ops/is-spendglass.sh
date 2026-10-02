#!/usr/bin/env bash
# Exit 0 when process <pid> is a Spendglass server, and 1 otherwise. The
# supervisor installer asks before it stops whatever holds the app's port,
# so it never stops somebody else's program.
#
# A Spendglass server is the launchd agent's own process, or a process
# whose command line runs the web app module, as start.sh does. The
# reloader's worker under SPENDGLASS_DEV counts through its parent.
#
# Usage: ops/is-spendglass.sh <pid>
set -euo pipefail

PID="$1"
LABEL="dev.spendglass.server"

agent_pid() {
  command -v launchctl >/dev/null 2>&1 || return 0
  launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
    | sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)$/\1/p' | head -1 || true
}

# The whole command line must be a Python running the module, the way
# start.sh execs it. A command that only mentions the module somewhere in
# its arguments doesn't count.
runs_the_web_app() {
  local cmd
  cmd="$(ps -ww -o command= -p "$1" 2>/dev/null || true)"
  [[ "$cmd" =~ (^|/)[Pp]ython[0-9.]*\ -m\ spendglass\.ui$ ]]
}

[ -n "$PID" ] && [ "$(agent_pid)" = "$PID" ] && exit 0
runs_the_web_app "$PID" && exit 0
PARENT="$(ps -o ppid= -p "$PID" 2>/dev/null | tr -d ' ' || true)"
[ -n "$PARENT" ] && [ "$PARENT" != 1 ] && runs_the_web_app "$PARENT" && exit 0
exit 1
