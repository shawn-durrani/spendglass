# Configuration: every setting, in one place

Most settings live in `.env`, in the repo's own folder. `start.sh`
creates it from `.env.example` the first time it runs, and it tightens
the file to `0600` on every run, so only your account can read it.
When a setting is in your environment and in `.env`, the environment
wins, as long as it isn't empty. The settings for the miners and the
scheduled sync live in the admin panel instead, and the store keeps
them.

## Keys

| variable | what it's for |
|---|---|
| `REDBARK_API_KEY` | Bank sync. A live key starts `rbk_live_…`. Without it, nothing pulls. |
| `ANTHROPIC_API_KEY` | Merchant identification only. Everything else runs without it. |
| `OPENAI_API_KEY` | Nothing yet. The admin panel can check and save one for later. |

You put the Redbark key in `.env` by hand. The admin panel can save the
Anthropic or OpenAI key for you. It sends the key to that provider once
to check it works, and it writes the key to `.env` only if the check
passes. The file stays at `0600`, the app starts using the key without
a restart, and the panel never shows the key back to you.

## The two environment-only settings

`SPENDGLASS_UI_PORT` sets the port, 8903 by default, and
`SPENDGLASS_DEV` turns on auto-reload while you work on the code.
`start.sh` starts the server without loading `.env` into its
environment, so these two are read from the process environment alone.
Putting them in `.env` does nothing.

```sh
SPENDGLASS_UI_PORT=8910 ./start.sh   # if 8903 is taken
SPENDGLASS_DEV=1 ./start.sh          # auto-reload during development
```

`SPENDGLASS_DEV` turns on with any value, even `0`. The supervised
install that [docs/OPERATIONS.md](OPERATIONS.md) sets up always serves
on 8903, because its launchd agent passes the server nothing but `HOME`
and `PATH`.

Every other variable is read from `.env` as well as the environment.

## Sync

| variable | default | what it does |
|---|---|---|
| `REDBARK_API_URL` | `https://api.redbark.com` | Where the Redbark API answers. Change it only to point at a sandbox or a mock. |
| `SPENDGLASS_BACKFILL_DAYS` | `365` | How far back a new account's first sync reaches. The app sets no cap, and the CDR rules cap what a bank shares at two years. |
| `SPENDGLASS_AUTOSYNC` | unset | `0` switches scheduled sync off. `1` lets it run on a store the scratch guard would hold back. |
| `SPENDGLASS_DB` | `data/store.db` | Where the store lives. Point it somewhere disposable for a scratch instance. |

Brokerage trades are fetched again over the whole backfill window on
every sync.

The store's folder holds more than the store. The sign-in files and
the backups live beside it, and at every start the web app makes that
folder and everything in it private to your account. Give a scratch
store a folder of its own.

The scratch guard keeps a test instance from filling up with your real
bank data. Scheduled sync holds off while the store is empty and
`SPENDGLASS_DB` points anywhere but the default. The guard lifts by
itself once the store holds data. The admin panel's Run now button
syncs regardless, and so does `SPENDGLASS_AUTOSYNC=1`. A new install
at the default path syncs as soon as it starts.

## Backups

| variable | default | what it does |
|---|---|---|
| `SPENDGLASS_BACKUP_INTERVAL_HOURS` | `24` | Hours between snapshots, by the clock, so time asleep counts. `0` turns backups off. |
| `SPENDGLASS_BACKUP_KEEP` | `10` | How many snapshots to keep. |
| `SPENDGLASS_BACKUP_MIRROR_DIR` | unset | A second copy of each snapshot in another folder, such as a synced one, for a copy off the computer. |

[docs/OPERATIONS.md](OPERATIONS.md#backups) says when a snapshot is
taken and how to restore one.

## Settings in the admin panel

The admin panel's miner settings are kept in the store, and each
change takes effect without a restart.

| setting | default | what it does |
|---|---|---|
| Auto-approve threshold | `0.8` | A merchant proposal at or above this confidence, from 0 to 1, is applied without asking you. |
| Max web searches per merchant | `3` | How many searches the lookup agent may run for one merchant, from 1 to 5. |
| Parallel workers | `4` | How many API calls a miner run makes at once, from 1 to 8. |
| Learn from my decisions | on | After you approve merchants, a short pass re-guesses the pending ones. |
| Scheduled sync every N hours | `6` | Sync and enrichment run this long after the last good sync, up to 168 hours. `0` turns it off. |

## Passkey, password and the recovery secret

The lock screen knows three credentials, each with its own job. Once
you enrol a passkey, such as Touch ID, it's your everyday unlock. You
enrol it from the admin panel at http://localhost:8903. Your password
sits one click behind it as the fallback. You need the recovery secret
only to set the password the first time, and to reset it after you
forget it.

A passkey can never lock you out, because the password always stays.
Passkeys work only on `localhost`. A browser won't let an IP address
hold one, so `127.0.0.1` keeps the password lock screen.

On the first start, the terminal prints a recovery secret in full,
unless you set your own. Paste it on your first visit to set your
password. Until a password exists, every start prints a secret, so if
you miss it, restart and look again. Unless you set one yourself, each
start makes a new one, and the secret from before a restart stops
working.

Once a password exists, the secret is never printed again, because a
printed secret piles up in server logs. You don't need it for everyday
use.

If you forget your password, you don't get the old secret back. You
choose a new one. Put `SPENDGLASS_RECOVERY_SECRET=anything-you-pick` in
`.env`, restart, and use the reset form with that value. Being able to
edit `.env` on this computer is what proves it's you.

You can also set `SPENDGLASS_RECOVERY_SECRET` before the first start,
which keeps the secret the same from day one. The app never prints a
secret you set. The first start tells you to use it, and later starts
say one is configured.

## Links to your other apps

A row at the top of both pages links your other apps, one click each.
Spendglass asks each app on this computer where a browser can open it,
and keeps the answers for a minute. An app that doesn't answer within a
second is left out, so the row never offers a link that won't open.

| variable | default | what it does |
|---|---|---|
| `SPENDGLASS_SIBLING_APPS` | Crossband, Membro and Threadfold on their usual ports | A JSON object of each app's name and the health address it answers on this computer. `{}` turns the row off. |

A value that isn't a JSON object keeps the defaults, and an address
that isn't on this computer is dropped.

Spendglass answers only on this computer. It reports its local
address to the other apps as `browser_origin` on `/api/session`, so
their rows show it on the Mac and leave it out on your phone.
