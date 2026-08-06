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
- The bank API key lives in `.env` (0600, gitignored) and is only ever
  read by the short-lived sync subprocess, never the long-running
  server, never the MCP process.
- The MCP server opens the database read-only at the SQLite layer and
  exposes a pinned, test-enforced list of read-only tools.
- No telemetry, no remote endpoints at runtime beyond the two APIs you
  configure (Redbark for bank data; Anthropic, optional, for merchant
  identification, which receives merchant descriptors, never balances
  or account data).

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
