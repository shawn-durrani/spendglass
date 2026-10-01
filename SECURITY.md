# Security

## Reporting

Report a suspected vulnerability privately through GitHub. Open the
Security tab on the repository and choose "Report a vulnerability".
Please don't open a public issue for a security report. One person
maintains the app, and you'll hear back within a few days.

## The threat model

Spendglass runs on your own computer and keeps a read-only copy of your
financial data. The controls are there to stop anything else on the
network reaching that copy, to keep the keys out of reach, and to make
sure nothing can move money.

### Who can reach the web app

The web app listens on `127.0.0.1` and nowhere else. The MCP server
talks to your agent over standard input and output, and it listens on
no port at all. The web app also checks the `Host` header against a
short list of its own addresses. That stops
[DNS rebinding](https://developer.mozilla.org/en-US/docs/Glossary/DNS_rebinding),
where a web page you visit points its own name at your computer to
reach the app. A `POST` request that the browser marks as coming from another
site is refused, and the session cookie is `HttpOnly` with `SameSite`
set to `Strict`.

### The lock screen

A password guards the web app. It's stored as a scrypt hash in
`data/ui_auth.json`. Signing in starts a session that lasts 24 hours
from that moment and survives a restart. The app stores only a SHA-256
hash of each session, so the file on disk never holds anything that
can sign you in.

Signing out ends that session. Resetting the password or removing a
passkey ends every session, and the browser that did it gets a fresh
one straight away.

A passkey can sit in front of the password. You enrol one from a
session that's already unlocked, never from the lock screen. At
`http://localhost:8903` the lock screen then offers the passkey first,
with the password one click behind it. The app keeps only public
material in `data/ui_passkeys.json`, such as the public key and the
credential's id, so a copy of the store can't stand in for the
passkey. Passkeys work only on `localhost`, and
[docs/CONFIG.md](docs/CONFIG.md#passkey-password-and-the-recovery-secret)
says how the passkey, the password and the recovery secret fit
together.

### Keys

The bank key lives in `.env`, which git ignores. `start.sh` sets the
file to `0600` on every run, so only your account can read it. You put
the bank key there by hand. The admin panel can save the Anthropic and
OpenAI keys into the same file, and it writes the file back at `0600`
too.

Only `sync.py` uses the bank key. It's the one place the app builds a
Redbark client, and it runs as a separate process that exits when the
sync is done.

### What the agent tools can do

The MCP server's tools only read, and a test pins the exact list of
them, so a tool that writes can't appear without review.
[docs/MCP.md](docs/MCP.md#the-tool-surface-is-the-security-surface)
says how far that promise reaches into the database.

### What leaves your computer

There's no telemetry. At runtime the app only talks to the providers
you set up.

Redbark serves the bank data, and only `sync.py` contacts it.

Anthropic is optional, and it identifies merchants. Four passes send it
data about a merchant, and none of them sends a balance or an account
id.

- The lookup agent sends the cleaned descriptor, up to three of the
  merchant's raw transaction descriptions, how many times it was seen,
  its typical charge in dollars, the first and last dates seen, and the
  list of subcategories you already use. It searches the web through
  Anthropic, so the descriptor can become a search query.
- The identity sweep sends the cleaned descriptor, up to two raw
  descriptions, the merchant's category, and the subcategory list.
- The category classifier sends the cleaned name, up to three raw
  descriptions, the average debit and the number of debits, along with
  the list of bank categories.
- Propagation, after you approve merchants in review, sends the names,
  summaries and subcategories of the pending and just-approved
  merchants, each with its merchant key.

Raw descriptions go as the bank wrote them. They can hold a card or
reference number, an amount in a foreign currency, or the name of the
person you paid. For a merchant seen once, the typical charge is that
one transaction's amount.

Saving a key in the admin panel sends it to that provider once, to
check it works, before it's written to `.env`. An OpenAI key can be
saved this way, but nothing in the app uses it yet. Unless you save
one, the app never contacts OpenAI.

### What's in scope

A report is in scope when it breaks one of the protections in the
threat model. That covers a network listener beyond loopback, a write path through the
agent tools, a key leaking into logs or responses, and a way past the
lock screen.

An attack that needs an already compromised computer, or physical
access to it, is out of scope. Anyone using your own account can read
`data/` directly, which is the same trust boundary as any other file
you own.

## What these controls don't do

The servers still hold the bank key in memory. The web app and the MCP
server both call `Config.load()` when they start, and it reads the
whole of `.env`. No code in either of them uses the key, but anything
that can read their memory has it. The web app also reads the
Anthropic key for each miner run, so that key sits in its memory too.

The first run prints the recovery secret in full, including one you
set yourself in `SPENDGLASS_RECOVERY_SECRET`. Later starts only say
which kind of secret is in force. Under the supervisor that first
banner lands in `data/service.log`. Treat terminal output and server
logs as sensitive, and don't paste them unredacted into a public issue.

The backup mirror sits outside this protection. A folder you set in
`SPENDGLASS_BACKUP_MIRROR_DIR` gets a full copy of the store with each
snapshot. Each copy is `0600`, but the app doesn't tighten the folder
or anything else in it.

## Your data folder

Everything in `data/` is sensitive, and git ignores all of it. It holds
the store and its `-wal` and `-shm` files, the backups, the password,
session and passkey files, and the service log when the supervisor
runs the app. `SPENDGLASS_DB` moves the whole folder with the store.

At startup the web app makes the folder owner-only, `0700`, and every
file and folder in it `0600` or `0700`. Everything it writes after that
is `0600` from the first byte, backups included. To reset a forgotten
password, follow
[docs/CONFIG.md](docs/CONFIG.md#passkey-password-and-the-recovery-secret).
