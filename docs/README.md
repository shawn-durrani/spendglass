# Spendglass documentation: start here

Every document in the repo, and the order to read them in, split by
what you're trying to do.

## I want to run Spendglass

1. [README.md](../README.md): what Spendglass is, what it promises, and
   the quick start.
2. [docs/CONFIG.md](CONFIG.md): every setting and its default. It
   covers the keys, the two settings read only from the environment,
   and how the passkey, the password and the recovery secret fit
   together.
3. [docs/OPERATIONS.md](OPERATIONS.md): keeping a live copy up. It
   covers the launchd supervisor, the busy check before a restart,
   syncing, backups, restoring one, and updating.

## I want my agents to answer questions about my money

- [docs/MCP.md](MCP.md): what the sixteen read-only tools cover, why a
  test pins the list, and how each answer says how fresh the data is.

## I want to understand what the numbers mean

- [docs/MERCHANTS.md](MERCHANTS.md): how a bank's description becomes
  a merchant name, what the miners cost, what counts as spend, and what
  a theme is.

## Safety, security, history

- [SECURITY.md](../SECURITY.md): the threat model, what leaves your
  computer, and how to report a problem.
- [CONTRIBUTING.md](../CONTRIBUTING.md): setup, the suite that runs
  without keys, the writing rules, and how a change lands.
- [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md): how people treat each
  other here.
- [ARCHITECTURE.md](../ARCHITECTURE.md): the design decisions that are
  settled.
- [CHANGELOG.md](../CHANGELOG.md): what changed for people using the
  app, release by release.
- [ACKNOWLEDGEMENTS.md](../ACKNOWLEDGEMENTS.md): the one third-party
  component and its licence.

## For an AI session working in the repo

[CLAUDE.md](../CLAUDE.md) is the entry point, and Claude Code loads it
by itself. The process rules live in CONTRIBUTING.md, so follow them
from there. Every document in `docs/` has to be linked from
`docs/README.md`, and README.md and CLAUDE.md have to link to
`docs/README.md` too. `tests/test_doc_style.py` checks both, so a new
doc that isn't linked turns CI red.
