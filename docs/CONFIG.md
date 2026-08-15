# Configuration: every setting, in one place

Most settings live in `.env` in the repo root. `start.sh` creates it from the
example on first run and tightens it to `0600` on every run. The environment
wins over `.env` where both are set.

## Keys

| variable | needed for |
|---|---|
| `REDBARK_API_KEY` | bank sync. Starts `rbk_live_…`. Without it, nothing pulls. |
| `ANTHROPIC_API_KEY` | merchant identification only. Everything else runs without it. |

The admin panel can save either for you, checking it against the provider's
API first, and writes `.env` back at `0600`.

## The two environment-only settings

`start.sh` execs the server without sourcing `.env`, so these two are read
from the process environment alone. Putting them in `.env` does nothing.

```sh
SPENDGLASS_UI_PORT=8904 ./start.sh   # if 8903 is taken
SPENDGLASS_DEV=1 ./start.sh          # auto-reload during development
```

Every other variable on this page is read from `.env` as well as the
environment.

## Sync and backup

| variable | default | what it does |
|---|---|---|
| `SPENDGLASS_BACKFILL_DAYS` | `365` | How far the first sync reaches back, up to the CDR two-year cap. |
| `SPENDGLASS_BACKUP_INTERVAL_HOURS` | `24` | Snapshot cadence. `0` disables. |
| `SPENDGLASS_BACKUP_KEEP` | `10` | How many snapshots to retain. |
| `SPENDGLASS_BACKUP_MIRROR_DIR` | unset | Second copy into a synced folder, for off-machine backups. |
| `SPENDGLASS_DB` | `data/store.db` | Store location. Point it somewhere disposable for a scratch instance. |

## Passkey, password and the recovery secret

Three credentials with distinct jobs. Once you enrol a **passkey** (Touch ID,
from the admin panel at http://localhost:8903) that is your everyday unlock.
Your **password** sits one click behind it as the fallback. The **recovery
secret** exists only for the two moments neither can help: first setup, and a
reset after you forget the password.

A passkey can never lock you out, because the password always remains.
Passkeys work only on `localhost`: an IP origin cannot hold one, by browser
rule, so `127.0.0.1` keeps the password gate.

**First start.** The terminal prints the recovery secret in full. Paste it on
the first visit to set your password. Until a password exists every start
prints a usable secret, so missing it just means looking again after a
restart.

**After that.** The secret is never printed again, because a printed secret
piles up in server logs. You never need it for normal use.

**Forgot your password?** You do not retrieve the old secret, you choose a new
one. Put `SPENDGLASS_RECOVERY_SECRET=anything-you-pick` in `.env`, restart,
and use the reset form with that value. Being able to edit `.env` on this
machine is what proves it is you.

Setting `SPENDGLASS_RECOVERY_SECRET` up front also works and keeps the secret
stable from day one. The startup banner then says one is configured without
printing it.
