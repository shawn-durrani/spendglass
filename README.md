# Spendglass

Spendglass keeps a copy of your bank transactions on your own
computer, where your local AI agents can read it. It syncs your
accounts through [Redbark](https://redbark.com/docs), an open-banking
service, into a [SQLite](https://sqlite.org) file. It works out who
your merchants are. You see the result in a web app behind a passkey
or a password, with a spending page. A read-only
[MCP](https://modelcontextprotocol.io) server lets the agents on your
computer answer questions about your money.

It only answers on the computer it runs on. Nothing listens beyond
loopback, and there's no remote mode to get wrong.

## The promise, in plain English

- Your data stays on your computer unless you set something up to send
  it. The server listens on `127.0.0.1` only, and there's no cloud
  part, no telemetry and no account with us. With an Anthropic key,
  merchant identification sends merchant details to Anthropic. What the
  agent tools answer goes to your agent's own model, and a backup
  mirror copies the store to the folder you choose.
  [SECURITY.md](SECURITY.md#what-leaves-your-computer) lists what goes
  where.
- Nothing here can move money. The sync only reads, because its bank
  client makes nothing but `GET` requests. The agent tools only read
  too, and a test pins the exact list of them, so a tool that writes
  can't appear by accident. The web app can't change what the bank
  sent. It writes your labels, such as merchant names, categories and
  themes, along with its own settings, provider keys and sign-in
  records.
- Only the sync process talks to your bank. `sync.py` is the only code
  that uses the Redbark key, and it runs as a separate process that
  exits when it's done. The web app and the MCP server never call the
  bank, and [SECURITY.md](SECURITY.md#what-these-controls-dont-do)
  says where that split stops.
- It works without an AI key. Every deterministic feature runs with no
  model, including sync, transfers, recurring detection, trends, themes
  and backups. An Anthropic key adds merchant identification, and
  leaving it out never breaks anything.

## What you get

You get sync, the store and the web app. You also get a
merchant-identity pipeline, with a lookup agent that searches the web
and a review queue where you have the final say. There's an
internal-transfer matcher, themes that cut across categories, a
subscription detector and eight trend views. Sync runs in the
background on a schedule, backups happen by themselves, and the
spending page drills down to the transactions behind each number.
Every chart comes with a plain-English "what am I looking at?"
explainer.

## Requirements

- Python 3.12 or newer.
- A [Redbark](https://redbark.com/docs) API key, which starts
  `rbk_live_…`. Redbark is an Australian open-banking aggregator that
  works under the Consumer Data Right, or CDR. Your bank consents live
  in its dashboard, and Spendglass only ever reads through it. The data
  is in Australian dollars and shaped by the CDR, with 16 fixed bank
  categories and consents that last 12 months.
- Optionally, an Anthropic API key for merchant identification, set as
  `ANTHROPIC_API_KEY` in `.env`. The admin panel can save it there for
  you, after checking it with Anthropic.

## Quick start

```sh
git clone https://github.com/shawn-durrani/spendglass.git
cd spendglass
./start.sh
```

`start.sh` creates `.venv`, installs the dependencies when they change,
and won't start a second copy on a port that's already taken. On the
first run it creates `.env` from the example. It tightens `.env` to
`0600` on every run, and it serves the web app at
http://127.0.0.1:8903.

Add your Redbark key to `.env`, then pull your data:

```sh
.venv/bin/python -m spendglass.sync
```

If you skip that step, the server's own scheduled sync picks the key up
within 15 minutes.

On your first visit you set a password, using the recovery secret the
terminal printed. [docs/CONFIG.md](docs/CONFIG.md) explains the three
credentials and every other setting.

To keep it running unattended, install the launchd supervisor once
with `ops/install-supervisor.sh`. It starts the app at login, restarts
it after a crash and brings it back after a reboot.
[docs/OPERATIONS.md](docs/OPERATIONS.md) says how it works.

## Connect your agents

```sh
claude mcp add -s user spendglass -e PYTHONPATH=<repo> -- <repo>/.venv/bin/python -m spendglass.mcp_server
```

That registers sixteen read-only tools. None of them writes, and a test
pins the exact list, because the tool list is the security boundary.
[docs/MCP.md](docs/MCP.md) says what they cover.

## Documentation

[docs/README.md](docs/README.md) lists every document by what you're
trying to do. The short version:

- [docs/CONFIG.md](docs/CONFIG.md): every setting, the keys and the
  credentials.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): the supervisor, syncing,
  backups and restoring one.
- [docs/MCP.md](docs/MCP.md): the agent tools and what they promise.
- [docs/MERCHANTS.md](docs/MERCHANTS.md): how merchants get their
  names, what the miners cost, what counts as spend, and themes.
- [ARCHITECTURE.md](ARCHITECTURE.md): the design decisions that are
  settled.

## Security

The web app answers only on loopback, and it checks the `Host` header
so a web page can't reach it through DNS rebinding. It sits behind a
passkey or a password, keeps only hashes of your password and
sessions, and refuses requests from other sites. A provider key is
checked before it's saved and never shown back to you.
[SECURITY.md](SECURITY.md) holds the threat model and how to report a
problem, and [ARCHITECTURE.md](ARCHITECTURE.md) holds the design rules
behind it.

## Licence

[MIT](LICENSE). One third-party component ships in this repository
under a different licence, and
[ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) names it.
