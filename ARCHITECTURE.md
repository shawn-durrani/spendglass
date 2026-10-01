# Architecture

Spendglass copies your bank data into a [SQLite](https://sqlite.org)
file on your computer and works everything else out from that copy.
Sync pulls the data through the [Redbark](https://redbark.com/docs)
API and stores each row as the bank sent it. The app's own code works
out transfer links, recurring charges, trends and themes from those
rows. Merchant names come from a model, and you review what it
proposes. A web app and a read-only
[MCP](https://modelcontextprotocol.io) server sit on top of the file,
so the AI agents on your computer can ask about your money.

The safety comes from how the parts are split. The process that uses
the bank key is short-lived and separate from the servers, the MCP
server only offers tools that read, and no code can move money. These
decisions are settled and unlikely to change.

## The shape

```
client.py       - the only code that speaks to the Redbark API. sync.py is
                  its only caller, so the bank key is used nowhere else
store.py        - SQLite in WAL mode under data/, idempotent upserts, the
                  system of record
sync.py         - pull: client to store, run as its own short-lived process
autosync.py     - runs sync, then enrich, as subprocesses on a timer
enrich / transfers / themes / subscriptions / trends - worked out from the
                  raw rows. enrich.py also holds the category classifier
lookup.py       - the merchant lookup agent, the identity sweep,
                  propagation and your review decisions
backup.py       - snapshots of the store, by the clock
ui.py           - the web app and its spending page. Reads the store, runs
                  the miners, and writes labels, settings, keys and
                  sign-ins, never what the bank sent
auth.py, passkeys.py - the lock screen: password, sessions, passkeys
capabilities.py - the providers a key can be saved for, and how each key
                  is checked
busy.py         - the work a restart would cut short, for /api/busy
app_links.py    - the header's links to your other apps
queries.py      - the read-only queries behind the MCP tools
mcp_server.py   - read-only tools over the store, never the network
config.py       - settings from the environment and .env
```

## Local only

The web app listens on `127.0.0.1` and nowhere else. The MCP server
talks to your agent over standard input and output, so it listens on
no port at all. There's no remote mode, no cloud part and no
telemetry. The chart library ships in the repo, so loading a page
fetches nothing from the internet. Remote access will stay out of
scope.

## Only the sync process uses the bank key

A sync runs as its own process. It reads `.env`, pulls from the bank,
writes the store and exits. `sync.py` is the only code that builds a
Redbark client, so no other part of the app ever uses the key. What
this split protects, and where it stops, is in
[SECURITY.md](SECURITY.md#what-these-controls-dont-do).

## Nothing can move money

The Redbark client only makes `GET` requests, so it can't change anything
at the bank. The MCP server's tools only read. None of them writes, and
a test pins the exact list of tools, so a new tool means changing that
test in a reviewed pull request. That promise covers the tools, and
[docs/MCP.md](docs/MCP.md#the-tool-surface-is-the-security-surface)
says how far it reaches into the database.

The web app does write, but never what the bank sent. It writes these:

- your labels: merchant names, category corrections and themes
- its settings: the miner settings, the sync interval, and the status
  of each miner and sync run
- provider keys, into `.env`
- the lock screen's records: the password, the sessions and the
  passkeys
- the derived tables, when it runs the miners
- backups of the store

## Amounts are kept and summed in integer cents

The store keeps each amount twice. One copy is the decimal string as
the bank sent it, and the other is the same amount in integer cents. Totals are summed
in SQL over the cents. Medians, averages and yearly figures are worked
out in Python, and some of those steps pass through floating point. The
spending page also adds up and averages in the browser for its charts.
No language model ever works out an amount. The model passes are only
told amounts, and the tools hand back finished numbers.

## Raw rows are the system of record

Sync writes each bank row by its source id, so pulling the same row
twice changes nothing. The app never edits what the bank sent. It drops
a pending row once the bank stops returning it, because a pending
purchase comes back posted under a new id. The one column the app sets
on a bank row is the merchant key, which enrichment works out from the
description.

The merchant table, each row's merchant key, recurring charges and
transfer links can all be deleted and rebuilt from the raw rows. Trends
aren't stored at all, and each request works them out afresh. Deleting
the merchant table also drops the categories a model cached there,
which then cost a paid run to get back.

Your own decisions live in tables a rebuild never touches: merchant
names and their review status, subcategories and themes. Each decision
is tied to a merchant key. If a change to the cleaning rules gives a
merchant a new key, the decision stays under the old key and stops
applying.
A database written by newer code is refused, and nothing migrates it
downward.

## Deterministic features work without an AI key

Only the merchant passes use a model. Those are the lookup agent, the
identity sweep, the category classifier and propagation. With no key,
sync, transfer matching, recurring detection, trends, themes and
backups all run as normal, and the whole test suite passes. CI runs
without credentials to keep it that way.

## Freshness is tracked and reported

If sync stops, agents would keep answering from old data and nobody
would notice. Every MCP response carries how fresh the data is, and
the web app's banner shows the last sync and the last backup. The
fields, and the one tool that reports freshness its own way, are in
[docs/MCP.md](docs/MCP.md#freshness).

## No investment advice

The Redbark API can serve holdings and trades, and the store syncs
them. Nothing is worked out from them, because a recommendation about a
portfolio is regulated financial advice.
