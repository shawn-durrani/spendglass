#!/usr/bin/env bash
# Roll the supervisor's log over once it passes a size, keeping one older
# copy beside it as <log>.1. launchd opens the log for appending and never
# trims it, so start.sh runs this on every start. Copy-then-truncate keeps
# launchd's open file handle valid, where a rename would leave it writing
# to the old copy. cp -p keeps the copy owner-only like the log.
#
# Usage: ops/rotate-log.sh <log> [max-bytes]    (default 10MB)
set -euo pipefail

LOG="$1"
MAX_BYTES="${2:-$((10 * 1024 * 1024))}"

if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" | tr -d ' ')" -gt "$MAX_BYTES" ]; then
  cp -p "$LOG" "$LOG.1" && : > "$LOG"
  echo "· rotated $LOG to $LOG.1"
fi
