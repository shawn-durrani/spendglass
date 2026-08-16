# Connecting your agents

Spendglass ships a read-only MCP server so local agents can answer questions
about your money without anything leaving the machine.

```sh
claude mcp add -s user spendglass -e PYTHONPATH=<repo> -- <repo>/.venv/bin/python -m spendglass.mcp_server
```

## What the tools can do

Sixteen read-only tools: accounts, transaction search, spending summaries,
recurring charges, store health, eight trend devices, themes, and
subscriptions. All money arithmetic runs in SQL over integer cents, so the
model only ever reads finished numbers.

## The tool surface is the security surface

No tool writes, and a test pins the exact tool list, so a write path cannot
appear without review. Adding a tool is a reviewed decision.

That guarantee is about the tools, not the database handle. Per-query
connections open with `mode=ro`, but the freshness envelope each response
carries goes through `Store`, which opens read-write and runs migrations. So
SQLite is not the backstop here; the pinned tool list is.

## Freshness

If sync stops, an agent would otherwise keep answering from old data with
nobody noticing. Every response carries `as_of` and `stale` fields with
warnings.

The one exception is `store_health`, which returns the store's own health
record: it has its own `stale` flag, the last sync run, and per-connection
warnings, but no `as_of` and no `staleness_warnings` list.
