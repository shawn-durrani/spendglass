# Connecting your agents

Spendglass comes with a read-only MCP server, so the AI agents on your
computer can answer questions about your money. MCP, the
[Model Context Protocol](https://modelcontextprotocol.io), is how an
agent such as Claude Code calls tools. The server reads the store on
your computer and makes no network call of its own. What it answers
goes to whichever model your agent runs on, so with Claude Code it
reaches Anthropic.

Register the server once on each computer, with your checkout's path
in place of `<repo>`:

```sh
claude mcp add -s user spendglass -e PYTHONPATH=<repo> -- <repo>/.venv/bin/python -m spendglass.mcp_server
```

`PYTHONPATH` lets the agent load the app from any folder, because
nothing is installed with pip.

## What the tools can do

There are sixteen tools, and every one of them only reads. They cover
your accounts, transaction search, spending summaries, recurring
charges, store health, eight trend views, themes and subscriptions.
Each tool hands back finished numbers, so the model never works out an
amount itself. Totals are summed in SQL over integer cents. Medians,
averages and yearly figures are worked out in Python, and some of
those steps pass through floating point.

## The tool surface is the security surface

No tool writes, and a test pins the exact list of tools, so a tool that
writes can't appear without review. Adding a tool is a reviewed
decision.

That promise covers the tools and stops short of the database
connection. Each query opens the store read-only, with `mode=ro`. The
freshness note on each response comes from the store's own class,
`Store`, which opens the file read-write and runs migrations. The
pinned tool list is what keeps the tools read-only, and SQLite doesn't
back it up.

## Freshness

If sync stops, an agent would keep answering from old data and nobody
would notice. Every response carries `as_of`, when the last good sync
finished, and `stale`, with the reasons in `staleness_warnings`.
`stale` is true when no sync has worked yet, when the latest one
failed, and once the last good one is more than 26 hours old. It's
also true when a bank connection has a warning, such as a connection
that has stopped updating or a consent that may run out within 90
days. A sync that's still running doesn't make the data stale by
itself.

`store_health` is the one exception, because it returns the store's
own health record. That record has the last sync run, the time the
last good one finished as `last_success_at`, each connection's warnings
and its own `stale` flag. The flag is true when no sync has worked yet
or the latest one failed. The record has no `as_of` and no
`staleness_warnings` list.
