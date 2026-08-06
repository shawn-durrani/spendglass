# Architecture

Spendglass pulls your bank data through the Redbark API into a local
SQLite file (raw transaction rows, upserted by source id and never
edited) and derives everything else from them in SQL: merchant
identities, transfer links, recurring charges, trends, themes. A
loopback-only web UI and a read-only MCP server sit on top of that file,
so local agents can query your finances without anything leaving the
machine. Security is enforced by construction: the sync process that uses
the bank key is short-lived and separate from the servers, the MCP layer
offers only read-only tools, and no code path can move money. Below are
the decisions that are settled and unlikely to change.

## The shape

```
client.py     - the only code that speaks to the Redbark API; sync.py is its
                only caller, so the bank key is used nowhere else
store.py      - SQLite (WAL) under data/; idempotent upserts; system of record
sync.py       - pull: client to store, run as its own short-lived process
enrich / transfers / themes / subscriptions / trends - derivation, in SQL
ui.py         - loopback web UI + spending page (reads store; writes labels,
                operator settings, and provider keys; never bank data)
mcp_server.py - read-only tools over the store, never the network
```

## Local only

Everything binds `127.0.0.1`. There is no remote mode, no cloud
component, no telemetry, and the chart library is vendored, so a page
load fetches nothing from the internet. Remote access is permanently out
of scope.

## Only the sync process uses the bank key

Syncing runs as a subprocess that reads `.env`, pulls, writes the store,
and exits. `sync.py` is the only code that ever constructs a Redbark
client, so the key is never used anywhere else in the app.

The honest limit: `Config.load()` reads the whole of `.env`, and both
long-running servers call it at startup, so the key does sit in their
memory for the life of the process. What the split buys is narrower than
"the servers never hold the credential": it is that no server code path
uses it, so there is nothing to trigger. Anything that reads a server's
memory has the key.

## Nothing can move money

The Redbark client implements only GET requests, so it cannot change
anything upstream. The MCP server's tool surface is read-only: no tool
writes, and a test pins the exact tool list, so any new tool has to be
added deliberately. Note that this is a property of the tools, not of the
database handle. Per-query connections open with `mode=ro`, but the
freshness envelope every tool response carries goes through `Store`,
which opens read-write and runs migrations, so "a write raises at the
SQLite layer" is not something to rely on.

Writes in the app fall into three groups, none of them bank data:
labels (merchant identities, category corrections, themes), operator
settings (miner thresholds, sync interval, and the miner status rows the
admin panel writes when it starts a run), and provider keys written into
`.env`.

## Money maths happens in SQL, in integer cents

The store keeps the raw decimal string exactly as the bank sent it,
plus integer cents. All aggregation runs in SQL over the cents column,
and models and browser code only read the finished numbers, so neither
floating-point arithmetic nor a language model ever computes an amount.

## Raw rows are the system of record

Bank rows are upserted idempotently by source id and never edited. Every
derived table (merchant keys, recurring charges, transfer links, trend
data) can be deleted and rebuilt from the raw rows, and user decisions
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
and the last backup. The one exception is `store_health`, which returns
the store's health record as-is: it carries its own `stale` flag, the
last sync run, and per-connection warnings, but no `as_of` and no
`staleness_warnings` list.

## No investment advice

The API can serve holdings and trades and the store syncs them, but no
analytics are derived from them, because recommendations about a
portfolio are regulated financial advice.
