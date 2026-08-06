# Architecture

Spendglass pulls your bank data through the Redbark API into a local
SQLite file — raw transaction rows, upserted by source id and never
edited — and derives everything else from them in SQL: merchant
identities, transfer links, recurring charges, trends, themes. A
loopback-only web UI and a read-only MCP server sit on top of that file,
so local agents can query your finances without anything leaving the
machine. Security is enforced by construction: the sync process that
holds the bank key is short-lived and separate from the servers, the MCP
layer opens the database read-only, and no code path can move money.
Below are the decisions that are settled and unlikely to change.

## The shape

```
client.py     — the only code that talks to the Redbark API or holds its key
store.py      — SQLite (WAL) under data/; idempotent upserts; system of record
sync.py       — pull: client → store, run as its own short-lived process
enrich / transfers / themes / subscriptions / trends — derivation, in SQL
ui.py         — loopback web UI + spending page (reads store; writes labels)
mcp_server.py — read-only tools over the store, never the network
```

## Local only

Everything binds `127.0.0.1`. There is no remote mode, no cloud
component, no telemetry, and the chart library is vendored, so a page
load fetches nothing from the internet. Remote access is permanently out
of scope.

## The bank key stays in a short-lived process

Syncing runs as a subprocess that reads `.env`, pulls, writes the store,
and exits. The long-running UI and MCP servers never load the key, so
even a fully compromised server process would not hold the bank
credential.

## Nothing can move money

The Redbark client implements only GET requests, so it cannot change
anything upstream. The MCP server opens the database read-only at the
SQLite layer (`mode=ro`), and a test pins its exact tool list, so any
new tool has to be added deliberately. The only writes in the app are
labels: merchant identities, category corrections, and themes.

## Money math happens in SQL, in integer cents

The store keeps the raw decimal string exactly as the bank sent it,
plus integer cents. All aggregation runs in SQL over the cents column,
and models and browser code only read the finished numbers, so neither
floating-point arithmetic nor a language model ever computes an amount.

## Raw rows are the system of record

Bank rows are upserted idempotently by source id and never edited. Every
derived table — merchant keys, recurring charges, transfer links, trend
data — can be deleted and rebuilt from the raw rows, and user decisions
are carried across rebuilds by migration. A database written by newer
code is refused rather than migrated downward.

## Deterministic features work without an AI key

Merchant identification uses a model; nothing else does. With no key
configured, sync, transfer matching, recurring detection, trends,
themes, and backups all run normally, and the full test suite passes.
CI runs without credentials to keep it that way.

## Freshness is tracked and reported

If sync stops, agents would otherwise keep answering from old data
without anyone noticing. Every MCP response therefore carries `as_of`
and `stale` fields with warnings, and the UI banner shows the last sync
and the last backup.

## No investment advice

The API can serve holdings and trades and the store syncs them, but no
analytics are derived from them, because recommendations about a
portfolio are regulated financial advice.
