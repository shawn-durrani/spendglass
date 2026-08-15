# Spendglass documentation: start here

Every document in this repo, and the order to read it in, split by what you
are trying to do.

## I want to run Spendglass

1. [README](../README.md): what this is, what it promises, quick start.
2. [docs/CONFIG.md](CONFIG.md): every setting and its default. The keys, the
   two that are environment-only, and how the passkey, password and recovery
   secret relate.
3. [docs/OPERATIONS.md](OPERATIONS.md): keeping a live instance up. The
   launchd supervisor, syncing, backups, restore, updating.

## I want my agents to answer questions about my money

- [docs/MCP.md](MCP.md): the sixteen read-only tools, why the tool list is
  pinned by a test, and how freshness is reported.

## I want to understand what the numbers mean

- [docs/MERCHANTS.md](MERCHANTS.md): how a bank descriptor becomes a merchant
  name, what the miners cost, what counts as spend, and what a theme is.

## Safety, security, history

- [SECURITY.md](../SECURITY.md): the threat model, what leaves the machine,
  and how to report an issue.
- [CONTRIBUTING.md](../CONTRIBUTING.md): setup, the keyless suite, the writing
  rules, and how work lands.
- [ARCHITECTURE.md](../ARCHITECTURE.md): the settled design decisions.
- [CHANGELOG.md](../CHANGELOG.md): user-facing history.
- [ACKNOWLEDGEMENTS.md](../ACKNOWLEDGEMENTS.md): the one vendored component
  and its licence.

## For an AI session working in this repo

[CLAUDE.md](../CLAUDE.md) is the entry point and is loaded automatically. The
process rules live in CONTRIBUTING.md; follow them from there rather than
re-deriving. One meta-rule about this index: **every document in `docs/` must
be listed on this page**, and `tests/` enforces it, so adding a doc without
indexing it turns CI red.
