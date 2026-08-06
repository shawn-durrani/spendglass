# Changelog

House convention: user-visible change, one line each, newest first.

## Unreleased

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
