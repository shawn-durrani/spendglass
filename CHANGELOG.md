# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

- The recovery secret prints only on a true first run, before a password
  is enrolled. Later starts print a redacted status line naming which
  kind of secret is in force and how to reset a forgotten password, so
  redirected server logs stop accumulating the secret in plaintext. The
  store directory also goes owner-only (0700) at startup.

## v0.1.1 (2026-08-07)

- start.sh now restores .env to owner-only permissions (0600) on every
  start. Previously a fresh write could leave the file readable by
  other local users (0644) until the next manual fix.
- Docs and source no longer describe the app as read-only with one
  write path: module docstrings, UI strings and section headers now
  match the real write surface (the store is written by sync,
  enrichment, review decisions, themes, admin settings and auth).

## v0.1.0 (2026-08-06)

First public release.

- Read-only sync from the Redbark open-banking API into local SQLite
  (WAL), with idempotent upserts, an overlap re-fetch window, consent
  tracking, and scheduled background syncs owned by the app.
- Password-protected loopback UI: transaction table with server-side
  search/filter/sort and per-cell label correction, review queue,
  admin panel, and a spending page (vendored ECharts) with drill-down
  to actual transactions and plain-English explainers on every chart.
- Merchant-identity pipeline: deterministic normaliser, optional
  web-search lookup agent with auto-approve threshold, batch sweep and
  classifier, human review queue with provenance decorators.
- Derivation in SQL: internal-transfer matcher, recurring-charge and
  subscription detection, eight trend devices, user-defined themes
  (created blank or from templates; new stores start with none).
- Read-only MCP server: sixteen pinned tools over the store, every
  response carrying freshness state.
- Automatic backups: consistent snapshots with rotation,
  change-detection, and an optional mirror folder; restore is a file
  copy.
- Keyless degradation throughout: without an AI key every deterministic
  feature works, and the full test suite runs with no credentials.
