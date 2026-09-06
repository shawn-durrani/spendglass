- A deploy no longer restarts spendglass in the middle of a bank sync or a
  backup (#49). The service now answers `GET /api/busy` on loopback with no
  login: `{"busy": false, "reasons": []}` when idle, otherwise fixed labels
  for the work in flight (a sync, an enrichment or merchant-lookup pass,
  the propagation pass after approvals, or a backup) and never content.
  The fleet's deploy watcher waits on that answer before a restart. A sync
  run left open by a crash stops counting after an hour.
