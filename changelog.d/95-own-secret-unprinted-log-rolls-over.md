- A recovery secret you set yourself in `SPENDGLASS_RECOVERY_SECRET` is
  never printed now, even on the first start (#95). The first start
  tells you to use it instead. A secret the app makes up is still
  printed on the first start, because you can't learn it any other way.
  The service log the supervisor writes, `data/service.log`, also rolls
  over at 10MB into `data/service.log.1`, so it no longer grows without
  end and an old banner ages out. The lock screen now says where the
  secret comes from, and how to reset with one.
