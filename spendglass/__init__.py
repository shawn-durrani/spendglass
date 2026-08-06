"""spendglass: a local, read-only copy of Redbark open-banking data.

Three layers, one privilege split:
  client.py  - the only code that talks to api.redbark.com; sync.py is its
               only caller, so the bank key is used nowhere else
  store.py   - SQLite (WAL) under data/; idempotent upserts; the system of record
  sync.py    - scheduled pull: client to store, consent status tracked loudly

The limit of that split: Config.load() parses the whole of `.env`, and both
long-running servers call it at startup, so the bank key sits in their
memory even though no code path there uses it.

The MCP server reads the store and makes no network call. The UI reads the
store too, but it is not read-only: it writes labels, operator settings and
provider keys, and it reaches the network for merchant identification and
to validate a provider key before saving it.
"""

__version__ = "0.1.0"
