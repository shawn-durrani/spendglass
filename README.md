# Spendglass

**Your bank transactions, stored on your own machine, readable by your
local AI agents, and by nothing else.** Spendglass syncs your accounts
through the [Redbark](https://docs.redbark.com) open-banking API into a
local SQLite file, works out who your merchants actually are, and serves
the result three ways: a password-protected web UI, a spending-analytics
page, and a read-only [MCP](https://modelcontextprotocol.io) server so
agents running on this machine can answer questions about your money.

Local only, on purpose: nothing listens beyond loopback, and there is no
remote mode to misconfigure.

## The promise, in plain English

- **Your data never leaves the machine.** The server binds `127.0.0.1`
  only. There is no cloud component, no telemetry, no account with us.
- **Nothing here can move money.** The sync is read-only at the API layer
  (the client has no non-GET path), and the MCP server opens the database
  read-only at the SQLite layer, so a write raises no matter who asks.
  The only thing the UI can change is labels: merchant names, categories,
  and themes.
- **The bank key never enters the server.** Syncing runs as a separate
  short-lived process; the long-running UI and MCP processes never hold
  the Redbark key.
- **It works without an AI key.** Every deterministic feature (sync,
  transfers, recurring detection, trends, themes, backups) runs with no
  model configured. An Anthropic key adds merchant identification, and
  its absence never breaks anything.

## What you get

Sync, store, and UI; a merchant-identity pipeline (web-search lookup agent
plus a human review queue); an internal-transfer matcher; cross-cutting
themes; a subscription detector; eight trend devices; scheduled background
syncs; automatic backups; and a spending page with drill-down to the
actual transactions. Every visualisation carries a plain-English "what am
I looking at?" explainer.

## Requirements

- Python 3.12+
- A [Redbark](https://docs.redbark.com) API key (`rbk_live_…`). Redbark
  is an Australian CDR open-banking aggregator; your bank consents live
  in their dashboard, and Spendglass only ever reads through them. Data
  is AUD-flavoured and CDR-shaped (16 fixed bank categories, 12-month
  consents).
- Optional: an Anthropic API key for merchant identification.

## Quick start

```sh
git clone https://github.com/shawn-durrani/spendglass.git
cd spendglass
./start.sh
```

`start.sh` creates `.venv`, installs dependencies when they change,
refuses a second instance on port **8903**, creates `.env` from the
example on first run, and serves the UI at **http://127.0.0.1:8903**. It
prints a **recovery secret** to the terminal; paste that on first visit
to set a durable password, and after that you just log in. Set
`SPENDGLASS_RECOVERY_SECRET` in `.env` to keep the secret stable, and
`SPENDGLASS_DEV=1` for auto-reload during development.

Add your Redbark key to `.env`, then pull data:

```sh
.venv/bin/python -m spendglass.sync
```

First sync backfills 365 days (`SPENDGLASS_BACKFILL_DAYS`, up to the CDR
two-year cap). While the server runs it keeps itself fresh: sync + enrich
run every N hours (admin setting, default 6) as subprocesses.

## Connect your agents (MCP)

```sh
claude mcp add -s user spendglass -e PYTHONPATH=<repo> -- <repo>/.venv/bin/python -m spendglass.mcp_server
```

Sixteen read-only tools (accounts, transaction search, spending
summaries, recurring charges, health, eight trend devices, themes, and
subscriptions), with all money arithmetic done in SQL integer cents; the
model only reads answers. The tool list is pinned by a test: adding a
tool is a reviewed decision, because the tool surface is the security
surface. Every response carries `as_of` and `stale` flags, so a stale
store answers loudly, never quietly.

## Merchant identity

Bank descriptors are plumbing ("SQ *COFFEE CO", reference numbers, FX
tails); the identity pipeline works out who the merchant is. A
deterministic normaliser cleans and keys descriptors, an optional
web-search agent proposes identities, confident proposals auto-apply, and
the rest land in a review queue. One decision keys on the merchant, so a
single click labels every matching transaction, and provenance decorators
show how each label was established (✦ agent, ✓ human, ? pending).

**What the miners cost** (they use *your* Anthropic key): the lookup agent
is the expensive one (a capable model plus billed web searches per unclear
merchant); the sweep and classifier use a small model via the Batch API
(cheap); everything else is deterministic SQL and costs nothing.
Discovery is front-loaded: once coverage is built, only new merchants
trigger lookups.

## What counts as spend

Spend views answer "what did I consume?": matched internal transfers,
loan principal, and committed family-style transfers are excluded by
default (never hidden; a toggle shows everything), while loan *interest*
counts as spend, because that money is gone forever.

## Themes

A theme is one number for something that spans categories: a renovation
is trades + hardware + architects; pets are supplies + vet + insurance.
New stores start with **no** themes: create your own in the admin panel,
blank or from a template (Renovation, Pets, Travel, Kids, AI, Health &
Fitness). Themes never change a transaction; they are a read-time lens.

## Back up & restore

The server backs itself up: consistent snapshots into `data/backups/` at
startup and every `SPENDGLASS_BACKUP_INTERVAL_HOURS` (default 24; `0`
disables), keeping the newest `SPENDGLASS_BACKUP_KEEP` (default 10). Set
`SPENDGLASS_BACKUP_MIRROR_DIR` to a synced folder for off-machine copies.
The banner shows the last backup and warns if snapshots stall.

**Restore:** stop the server, copy a snapshot from `data/backups/` over
`data/store.db` (remove `store.db-wal`/`store.db-shm` if present), start
the server.

**Why it matters:** the store holds bank rows (re-fetchable only within
the backfill window) and your decisions (irreplaceable).

## Updating

```sh
git pull && ./start.sh
```

Dependencies reinstall when they change; schema migrations run forward
automatically. A database written by *newer* code is refused, never
mangled; upgrade the code rather than downgrading the data.

## Security

Loopback-only binds with a Host-header allowlist (DNS-rebinding defence),
scrypt-hashed password, sessions stored as digests (the file on disk never
holds a usable credential), cross-site POSTs rejected, provider keys
validated before saving and never echoed back. See
[SECURITY.md](SECURITY.md) for the threat model and how to report issues.
The design rules behind all of this live in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Licence

[MIT](LICENSE).
