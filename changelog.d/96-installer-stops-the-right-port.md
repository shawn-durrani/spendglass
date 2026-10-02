- The supervisor installer stops only a hand-started copy on 8903, the
  port the supervised server always uses (#96). It used to read
  `SPENDGLASS_UI_PORT` from `.env`, which the server never reads, so
  with that set it could stop an unrelated program on another port and
  leave 8903 held.
