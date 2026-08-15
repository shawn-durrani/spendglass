# CLAUDE.md

Instructions for AI sessions working in this repository.

## Process

The pipeline is documented once, in [CONTRIBUTING.md](CONTRIBUTING.md).
Session-specific rules, for a session with write access working for the
maintainer:

- Merge your own PRs once CI is green; don't wait for human approval. The
  maintainer comments asynchronously. External contributors: the maintainer
  merges yours.
- Never commit directly to `main`, and never branch off another open PR.
- Restart the supervised service only with
  `launchctl kickstart -k gui/$(id -u)/dev.spendglass.server`.

## Rules that override convenience

- `data/` holds real bank transactions. Never read, copy or quote its
  contents into code, tests, docs, commits or chat. Debug with a disposable
  `SPENDGLASS_DB`. Real merchant names and amounts are personal data, and so
  are account nicknames.
- No real personal data in any diff. The fleet synthetic roster: people Alex,
  Sam, Dave, Mateo; place Fairhaven; companies AcmeCo, Initech, Globex; banks
  and accounts `Example Bank`, `acc-1`. `tests/conftest.py` shows the house
  style.
- Nothing may add a write path. The Redbark client implements only GET, and
  the MCP tool surface is read-only with the exact tool list pinned by a test.
  Both are deliberate; widening either is a reviewed decision, not a
  refactor.
- The suite stays keyless. Every deterministic feature runs with no model
  configured, and CI runs without credentials to keep it that way.
- No investment-advice derivation. The API can serve holdings and trades and
  the store syncs them, but no analytics are derived from them, because
  recommendations about a portfolio are regulated financial advice. See
  [ARCHITECTURE.md](ARCHITECTURE.md).
- Money arithmetic happens in SQL over integer cents. Neither floating-point
  arithmetic nor a language model ever computes an amount.

## Orientation

Read [ARCHITECTURE.md](ARCHITECTURE.md), then browse
[docs/README.md](docs/README.md), which indexes every document by what you are
trying to do. Open issues hold the active work.
