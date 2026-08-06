"""spendglass — local, read-only store for Redbark open-banking data.

Three layers, one privilege split:
  client.py  — the only code that talks to api.redbark.com or holds the API key
  store.py   — SQLite (WAL) under data/; idempotent upserts; the system of record
  sync.py    — scheduled pull: client → store, consent status tracked loudly

The MCP server and UI read the store only — never the network.
"""

__version__ = "0.1.0"
