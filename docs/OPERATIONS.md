# Operations: keeping Spendglass running

Spendglass is a service that runs on your computer, on port 8903. If
you start it with `./start.sh`, it stops when you close the terminal,
and a crash or a reboot leaves it down until you notice. A supervisor
fixes that. It's a program that starts another one and keeps it
running. On macOS the built-in one is
[launchd](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).

## Install the supervisor

```sh
ops/install-supervisor.sh
```

The script hands Spendglass to launchd as an agent named
`dev.spendglass.server`, which runs `start.sh`. It first stops any copy
you started by hand on the port, so launchd owns the one real copy, and
it's safe to run again. From then on the app starts when you log in,
restarts within seconds if it exits, and comes back after a reboot once
you log in. If it keeps crashing, launchd waits ten seconds between
tries. Its output goes to `data/service.log`.

## Everyday commands

Run these from the repo folder, because the `tail` path is relative to
it.

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

`bootout` lasts until you next log in. The agent's file stays in
`~/Library/LaunchAgents`, and launchd loads it again at login. To stop
supervising for good, delete `~/Library/LaunchAgents/dev.spendglass.server.plist`
after the `bootout`.

While the supervisor holds port 8903, `./start.sh` refuses to start a
second copy. Killing the process by hand only makes launchd start it
again, so use the restart command. `./start.sh` is the right command
again once you `bootout` the agent.

## Before you restart

A restart cuts short whatever the service is doing, so ask it first:

```sh
curl -s http://127.0.0.1:8903/api/busy
```

It answers `{"busy":false,"reasons":[]}` when nothing is running.
When something is, it names the work with fixed labels: `sync`, `enrich`,
`lookup`, `classify`, `sweep`, `propagate` or `backup`. The route needs
no sign-in and never carries content. The fleet's deploy watcher waits
on it before it restarts Spendglass. A sync run left open by a crash
stops counting after an hour, so a stale row can't hold up every
deploy.

## Syncing

Run the first sync by hand. It reaches back 365 days, which
`SPENDGLASS_BACKFILL_DAYS` in [docs/CONFIG.md](CONFIG.md#sync) changes.

```sh
.venv/bin/python -m spendglass.sync
```

While the server runs, it keeps the store fresh by itself. Every six
hours by default, measured from the last good sync, it runs sync and
then enrichment, each as its own process. You set the interval in the
admin panel. After a failed sync it tries again 15 minutes later.

The web app's banner shows the last sync and the last backup. Each
agent tool's answer says how fresh the data is, as
[docs/MCP.md](MCP.md#freshness) explains, so a store that has stopped
syncing says so.

## Backups

The server backs itself up. It takes a consistent snapshot into a
`backups` folder beside the store, `data/backups/` by default, at
startup and then every `SPENDGLASS_BACKUP_INTERVAL_HOURS`. It keeps the
newest `SPENDGLASS_BACKUP_KEEP` snapshots, and
[docs/CONFIG.md](CONFIG.md#backups) has the defaults. A snapshot is
skipped whenever the store hasn't changed since the newest one,
including the one at startup.

The interval goes by the clock, so time the Mac spends asleep counts.
The server checks every five minutes, so a snapshot that fell due
during sleep is taken within five minutes of waking. A failed backup
waits a full interval before it tries again. Set
`SPENDGLASS_BACKUP_MIRROR_DIR` to a synced folder to keep a copy off
the computer.

The banner warns when backups stall, which means the newest snapshot
is more than twice the interval old, or there's none. A store that
hasn't changed takes no snapshots, so with scheduled sync off that
warning can show when nothing is wrong.

Backups matter because the store holds two things that are hard to
replace. Bank rows can be fetched again, but only as far back as the
backfill window reaches. Your own decisions can't be fetched again at
all. A snapshot holds the store only, so your keys in `.env`, your
password, your passkeys and your sessions aren't in it.

### Restore

1. Stop the server. Under the supervisor, that's the `bootout` command.
2. Copy a snapshot from `data/backups/` over `data/store.db`.
3. Remove `store.db-wal` and `store.db-shm` if they're there.
4. Start the server. Under the supervisor, run
   `ops/install-supervisor.sh` again.

## Updating

Under the supervisor, pull and restart:

```sh
git pull && launchctl kickstart -k gui/$(id -u)/dev.spendglass.server
```

Without it, stop the running copy first, then:

```sh
git pull && ./start.sh
```

`start.sh` reinstalls the dependencies when they change. The store adds
any new tables and columns it needs each time it opens. A database
written by newer code is refused, so it never gets mangled. Upgrade the
code, and don't downgrade the data.

## Not on macOS?

The same idea works with
[systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html),
the supervisor on most Linux systems. You'd write a unit with
`Restart=always` and `WantedBy=default.target`. No unit file ships
yet, but the plist template's command maps straight across. Its
command, `bash start.sh`, goes in `ExecStart`, and its working
directory, the repo, goes in `WorkingDirectory`.
