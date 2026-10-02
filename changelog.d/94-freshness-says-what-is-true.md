- The freshness note on every agent answer, and the banner in the web
  app, now say what's true while a sync runs or after one fails (#94).
  Both used to treat the newest sync as the only one, so an agent was
  told "no successful sync has ever completed" while a routine sync was
  running, and the banner showed a date in 1970. They now date the data
  by the last good sync, say when one is running or the latest failed,
  and keep "never" for a store that has never synced. A sync in progress
  no longer marks recent data as stale.
