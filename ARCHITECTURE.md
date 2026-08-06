# Architecture

Spendglass is small, and most of it could be rebuilt differently without
anyone noticing. This page covers the handful of decisions that are
settled — the ones the code is organised around, and that a change would
have to argue with rather than drift past.

## The shape

```
client.py     — the only code that talks to the Redbark API or holds its key
store.py      — SQLite (WAL) under data/; idempotent upserts; system of record
sync.py       — pull: client → store, run as its own short-lived process
enrich / transfers / themes / subscriptions / trends — derivation, in SQL
ui.py         — loopback web UI + spending page (reads store; writes labels)
mcp_server.py — read-only tools over the store, never the network
```

## Local-only is the product, not a deployment option

Everything binds `127.0.0.1`. There is no remote mode, no cloud half, no
telemetry, and the chart library is vendored so a page load fetches
nothing from the internet. If a feature needs the data to leave the
machine, it isn't a feature of this app.

## The bank key never enters a long-running process

Syncing runs as a short-lived subprocess that reads `.env`, pulls,
writes the store, and exits. The UI server and the MCP server never load
the key. That is a process boundary, not a code-review promise: the
server could be fully compromised and still not hold the credential.

## Nothing here can move money

The Redbark client has no non-GET path, so it cannot mutate anything
upstream. The MCP server opens the database read-only at the SQLite
layer (`mode=ro`), so a write raises no matter who asks — and its tool
list is pinned by a test, so adding a tool is a reviewed decision rather
than drift. The only writes in the whole app are labels: merchant
identities, category corrections, themes.

## Money math happens in SQL, in integer cents

The store keeps the raw decimal string exactly as the bank sent it, plus
integer cents. All aggregation runs in SQL over the cents column; models
and browser code only read finished numbers. There is no code path in
which a float — or a language model — does arithmetic on money.

## Raw rows are the record; everything else is disposable

Bank rows are upserted idempotently by source id and never edited. Every
derived table (merchant keys, recurring charges, transfer links, trend
devices) can be deleted and rebuilt from the raw rows, and rebuilds are
wholesale, never patched in place; user decisions are carried across by
migration. The corollary runs both ways: a database written by newer
code is refused, never "fixed".

## Everything deterministic works without an AI key

Merchant identification uses a model; nothing else does. With no key
configured, sync, transfer matching, recurring detection, trends,
themes, and backups all work, and the entire test suite passes — CI runs
keyless on purpose. Intelligence is an enhancement, never a dependency.

## Freshness is part of the data model

The natural failure mode of a local store is quiet: sync stops, and
agents keep answering confidently from old data. So freshness is part of
the data model — every MCP response carries `as_of` and `stale` with
warnings, and the UI banner shows when the store last synced and last
backed up.

## One deliberate absence: investment advice

The API can serve holdings and trades, and the store syncs them. No
analytics are derived from them. "Ways to improve your portfolio" is
regulated advice; staying out is a scope decision, not a gap.
