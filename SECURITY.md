# Security

## Reporting

Please report suspected vulnerabilities privately via GitHub: **Security →
Report a vulnerability** on this repository. You'll get an acknowledgement
as soon as the maintainer sees it (solo-maintained; usually within a few
days). Please don't open public issues for security reports.

## Threat model, briefly

Spendglass is a **local-only** application handling **read-only** copies
of financial data:

- The server binds `127.0.0.1` exclusively, with a Host-header allowlist
  as DNS-rebinding defence; cross-site POSTs are rejected.
- The UI is password-protected (scrypt-hashed; sessions stored as SHA-256
  digests, so the file on disk never contains a usable credential).
- The bank API key lives in `.env` (gitignored; `start.sh` sets the file
  to 0600 on every run, and saving a provider key from the admin panel
  rewrites it at 0600 too, so both routes land in the same place). Only
  `sync.py` ever uses it: it is the one place a Redbark client is built,
  and it runs as a short-lived subprocess. Worth stating plainly, because
  it is the limit of that split: the long-running server and the MCP
  process both call `Config.load()`, which parses the whole of `.env`, so
  the key sits in their memory for the life of the process even though no
  code path there uses it. Anything that can read a server process's
  memory has the key.
- The MCP server's **tool surface** is read-only: no tool writes, and a
  test pins the exact tool list, so a write path cannot appear without
  review. That is a guarantee about the tools, not about the database
  handle. Per-query connections open with `mode=ro`, but the freshness
  envelope each tool attaches goes through `Store`, which opens
  read-write and runs migrations, so SQLite is not the backstop here.
- No telemetry. The only remote endpoints at runtime are the providers
  you configure:
  - **Redbark**, for bank data, contacted by `sync.py` alone.
  - **Anthropic**, optional, for merchant identification. What goes:
    the cleaned descriptor, up to three of that merchant's raw
    transaction descriptions, the number of times it was seen, the
    typical charge in dollars, and the first and last dates seen. The
    propagation pass also sends the proposed names, summaries and
    subcategories of pending and just-confirmed merchants. Balances,
    account ids, account numbers and individual transaction amounts are
    never sent.
  - **OpenAI**. A provider entry is registered so a key can be saved,
    and saving one sends that key to `api.openai.com` once to validate
    it before writing it to `.env`. Nothing in the app uses an OpenAI key
    today, so unless you save one, this endpoint is never contacted.

**In scope:** anything that breaks the properties above: a network
listener beyond loopback, a write path through the MCP surface, key
leakage into logs or responses, an auth bypass.

**Out of scope:** attacks requiring an already-compromised local machine
or physical access; a local attacker with your user account can read
`data/` directly, which is the same trust boundary as any local file.

## Operational notes

- The startup banner prints the recovery secret on first run so you can
  enroll a password. Treat terminal output and server logs as sensitive;
  don't paste them into public issues unredacted.
- `data/` in its entirety is sensitive: the store, its WAL/SHM sidecars,
  backups, and logs all live there, and all are gitignored.
