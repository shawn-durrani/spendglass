- Pending purchases no longer linger after they post (#47). A card
  authorisation arrives first as a pending row and later as the posted
  transaction under a new id, and the store kept both, so hotels,
  subscriptions and transfers showed twice a day apart. Each sync now
  drops the pending rows the provider has stopped returning inside the
  window it fetched, and reaches that window back to the oldest pending
  row, so the ghosts already in the ledger clear on the first sync after
  upgrading. Posted rows are never removed on the provider's silence.
