# Operations: keeping Spendglass running

Spendglass is a long-running local service on port 8903. Left to `./start.sh`
it dies with your terminal, and a crash or a reboot leaves it down until you
notice. On macOS, launchd fixes that.

## Install the supervisor

```sh
ops/install-supervisor.sh
```

The agent is `dev.spendglass.server`. It runs `start.sh`, starts at login,
restarts within seconds if the process exits, and survives reboots. Logs go to
`data/service.log`.

## Everyday commands

Run these from the repo folder, since `tail` is relative to it.

```sh
# restart, after a git pull or an .env change
launchctl kickstart -k gui/$(id -u)/dev.spendglass.server

# is it running, and as which pid?
launchctl print gui/$(id -u)/dev.spendglass.server | grep -iE 'state|pid'

# follow the log
tail -f data/service.log

# stop supervising, and stop the service
launchctl bootout gui/$(id -u)/dev.spendglass.server
```

While the supervisor holds port 8903, `./start.sh` refuses to start a second
instance. Killing the process by hand only makes launchd start it again, so
use the restart command above. `./start.sh` becomes the right command again
once you `bootout` the agent.

## Syncing

The first sync backfills 365 days (`SPENDGLASS_BACKFILL_DAYS`, up to the CDR
two-year cap):

```sh
.venv/bin/python -m spendglass.sync
```

While the server runs it keeps itself fresh: sync and enrich run every N hours
as subprocesses, six by default, set in the admin panel.

Every MCP response but one carries `as_of` and `stale` flags, so a store that
has stopped syncing answers loudly. The UI banner shows the last sync and the
last backup.

## Backups

The server backs itself up: consistent snapshots into `data/backups/` at
startup and every `SPENDGLASS_BACKUP_INTERVAL_HOURS` (default 24, `0` disables),
keeping the newest `SPENDGLASS_BACKUP_KEEP` (default 10). Set
`SPENDGLASS_BACKUP_MIRROR_DIR` to a synced folder for off-machine copies. The
banner warns if snapshots stall.

This matters because the store holds two things that are hard to replace: bank
rows, re-fetchable only within the backfill window, and your own decisions,
which are not re-fetchable at all.

### Restore

1. Stop the server.
2. Copy a snapshot from `data/backups/` over `data/store.db`.
3. Remove `store.db-wal` and `store.db-shm` if present.
4. Start the server.

## Updating

```sh
git pull && ./start.sh
```

Dependencies reinstall when they change, and schema migrations run forward
automatically. A database written by *newer* code is refused rather than
mangled, so upgrade the code rather than downgrading the data.

## Not on macOS?

The same idea works with `systemd`: a unit with `Restart=always` and
`WantedBy=default.target`. No unit file ships here yet, but the plist
template's command (`bash start.sh`, working directory = the repo) maps
directly onto `ExecStart` and `WorkingDirectory`.
