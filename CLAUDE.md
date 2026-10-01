# CLAUDE.md

Instructions for AI sessions working in this repository.

## Process

The pipeline is written down once, in [CONTRIBUTING.md](CONTRIBUTING.md).
These rules are for a session with write access, working for the
maintainer:

- Merge your own pull requests once CI is green, without waiting for a
  human to approve them. The maintainer comments when they can. An
  external contributor's pull request is merged by the maintainer.
- Never commit straight to `main`, and never branch off another open
  pull request.
- Restart the supervised service only with
  `launchctl kickstart -k gui/$(id -u)/dev.spendglass.server`, and ask
  `/api/busy` first, as [docs/OPERATIONS.md](docs/OPERATIONS.md#before-you-restart)
  describes.

## Rules that come before convenience

- `data/` holds real bank transactions. Never read, copy or quote what's
  in it into code, tests, docs, commits or chat. Debug with a
  disposable `SPENDGLASS_DB` in a folder of its own. Real merchant names
  and amounts are personal data, and so are account nicknames.
- No real personal data goes in any diff. Use the fleet's synthetic
  roster. The people are Alex, Sam, Dave and Mateo, the place is
  Fairhaven, and the companies are AcmeCo, Initech and Globex.
  `tests/conftest.py` shows the house style for banks and accounts,
  such as `Example Bank` and `acc-1`.
- Nothing may add a way to write to the bank or through the agent tools.
  The Redbark client only makes `GET` requests, and a test pins the
  exact list of read-only MCP tools. Widening either one is a decision
  for review, and a refactor never does it.
- The suite stays green without keys. Every deterministic feature runs
  with no model configured, and CI runs without credentials to keep it
  that way.
- Nothing is worked out from holdings or trades. The API can serve them
  and the store syncs them, but a recommendation about a portfolio is
  regulated financial advice. [ARCHITECTURE.md](ARCHITECTURE.md) has
  the decision.
- Sum money in SQL over integer cents, and never let a language model
  work out an amount. Some trend and subscription maths runs in Python
  through floating point today, so don't copy it into new code.

## Orientation

Read [ARCHITECTURE.md](ARCHITECTURE.md), then browse
[docs/README.md](docs/README.md), which lists every document by what
you're trying to do. Open issues hold the active work.
