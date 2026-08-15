# Spendglass

**Your bank transactions, stored on your own machine, readable by your
local AI agents, and by nothing else.** Spendglass syncs your accounts
through the [Redbark](https://docs.redbark.com) open-banking API into a
local SQLite file, works out who your merchants actually are, and serves
the result three ways: a passkey- or password-protected web UI, a spending-analytics
page, and a read-only [MCP](https://modelcontextprotocol.io) server so
agents running on this machine can answer questions about your money.

Local only, on purpose: nothing listens beyond loopback, and there is no
remote mode to misconfigure.

## The promise, in plain English

- **Your data never leaves the machine.** The server binds `127.0.0.1`
  only. There is no cloud component, no telemetry, no account with us.
- **Nothing here can move money.** The sync is read-only at the API layer
  (the client has no non-GET path), and the MCP server's *tool surface* is
  read-only: no tool writes anything, and a test pins the exact tool list,
  so a write path cannot appear by accident. The UI cannot change bank
  data at all. What it does write is labels (merchant names, categories,
  themes) plus operator settings and provider keys.
- **Only the sync process talks to your bank.** `sync.py` is the only
  code that ever uses the Redbark key, and it runs as a separate
  short-lived process. The long-running UI and MCP servers do read `.env`
  at startup, so the key is present in their memory; what they never do is
  call the bank with it.
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
- Optional: an Anthropic API key for merchant identification, set as
  `ANTHROPIC_API_KEY` in `.env` (the admin panel can also save it there
  for you, after checking it against the API).

## Quick start

```sh
git clone https://github.com/shawn-durrani/spendglass.git
cd spendglass
./start.sh
```

`start.sh` creates `.venv`, installs dependencies when they change, refuses a
second instance on the port it is about to use, creates `.env` from the
example on first run and tightens it to `0600` on every run, and serves the UI
at **http://127.0.0.1:8903**.

Add your Redbark key to `.env`, then pull data:

```sh
.venv/bin/python -m spendglass.sync
```

The first visit asks you to set a password, using the recovery secret the
terminal printed. [docs/CONFIG.md](docs/CONFIG.md) explains the three
credentials and every other setting.

To keep it running unattended (start at login, restart on crash, survive
reboots), install the launchd supervisor once with
`ops/install-supervisor.sh`. See [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Connect your agents

```sh
claude mcp add -s user spendglass -e PYTHONPATH=<repo> -- <repo>/.venv/bin/python -m spendglass.mcp_server
```

Sixteen read-only tools. No tool writes, and a test pins the exact list,
because the tool surface is the security surface.
[docs/MCP.md](docs/MCP.md).

## Documentation

[docs/README.md](docs/README.md) indexes everything by what you are trying to
do. The short version:

- [docs/CONFIG.md](docs/CONFIG.md): every setting, the keys, the credentials.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): supervisor, sync, backup, restore.
- [docs/MCP.md](docs/MCP.md): the agent tools and what they guarantee.
- [docs/MERCHANTS.md](docs/MERCHANTS.md): merchant identity, what the miners
  cost, what counts as spend, themes.
- [ARCHITECTURE.md](ARCHITECTURE.md): the settled design decisions.

## Security

Loopback-only binds with a Host-header allowlist (DNS-rebinding defence),
a passkey-first gate with a scrypt-hashed password fallback (only the
passkey's public key is stored), sessions stored as digests (the file on
disk never holds a usable credential), cross-site POSTs rejected, provider
keys validated before saving and never echoed back. See
[SECURITY.md](SECURITY.md) for the threat model and how to report issues.
The design rules behind all of this live in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Licence

[MIT](LICENSE). One third-party component ships in this repository under
a different licence: see [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).
