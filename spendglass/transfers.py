"""Transfer matching — facts about money moving between accounts.

Two-leg matching is arithmetic, not judgement: a debit in one account and a
credit of the same amount in another of the user's accounts within a small
window is one internal move, and both legs get linked. Everything else is
one-sided, and one-sided transfers are NOT all alike:

- payments to businesses or people by bank transfer are genuine spending
  (the payee is the merchant);
- scheduled contributions to shared family accounts are committed
  obligations — visible, part of the fixed floor, never consumption;
- transfers to the user's own unsynced accounts are internal in spirit but
  can't be proven here.

The matcher records only the provable class (`internal`); the one-sided
classes are expressed through the existing identity machinery — labels and
categories the user controls. Links are derived and fully re-derivable:
delete the table and re-run.

Run:  .venv/bin/python -m spendglass.transfers
"""

from __future__ import annotations

import re
import sys
import time
from datetime import date

from .config import Config
from .store import Store

WINDOW_DAYS = 3

# A leg participates in matching only if it looks like a transfer — pairing
# arbitrary equal amounts would link a grocery run to a salary top-up.
TRANSFERISH = re.compile(
    r"transfer|tfr\b|osko|withdrawal|deposit|repayment|payment received"
    r"|top ?up|ln repay|loan repayment",
    re.IGNORECASE,
)
TRANSFER_CATS = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfer_links (
  txn_id TEXT PRIMARY KEY,
  peer_txn_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('internal')),
  matched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfer_links_peer ON transfer_links(peer_txn_id);
"""


def ensure_schema(store: Store) -> None:
    store.con.executescript(_SCHEMA)
    store.con.commit()


def _transferish(row) -> bool:
    return (row["category"] in TRANSFER_CATS
            or bool(TRANSFERISH.search(row["description"] or "")))


def match_transfers(store: Store) -> dict:
    """Rebuild transfer_links: greedy nearest-date pairing of opposite-amount
    transfer-shaped legs across different accounts within WINDOW_DAYS."""
    ensure_schema(store)
    store.con.execute("DELETE FROM transfer_links")  # derived: rebuild
    rows = store.con.execute(
        """SELECT id, account_id, date, amount_cents, direction, description,
                  category
           FROM transactions
           WHERE date IS NOT NULL AND COALESCE(amount_cents, 0) != 0
           ORDER BY date"""
    ).fetchall()

    credits: dict[int, list] = {}
    for r in rows:
        if r["direction"] == "credit" and _transferish(r):
            credits.setdefault(abs(r["amount_cents"]), []).append(
                {"id": r["id"], "account": r["account_id"],
                 "date": date.fromisoformat(r["date"]), "used": False})

    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    linked = 0
    linked_cents = 0
    for r in rows:
        if r["direction"] != "debit" or not _transferish(r):
            continue
        pool = credits.get(abs(r["amount_cents"]))
        if not pool:
            continue
        d = date.fromisoformat(r["date"])
        best = None
        for c in pool:
            if c["used"] or c["account"] == r["account_id"]:
                continue
            gap = abs((c["date"] - d).days)
            if gap <= WINDOW_DAYS and (best is None or gap < best[0]):
                best = (gap, c)
        if best is None:
            continue
        c = best[1]
        c["used"] = True
        store.con.executemany(
            "INSERT OR REPLACE INTO transfer_links "
            "(txn_id, peer_txn_id, kind, matched_at) VALUES (?,?,?,?)",
            [(r["id"], c["id"], "internal", now),
             (c["id"], r["id"], "internal", now)])
        linked += 1
        linked_cents += abs(r["amount_cents"])
    store.con.commit()

    unmatched = store.con.execute(
        f"""SELECT COUNT(*) n, ABS(COALESCE(SUM(amount_cents),0)) c
            FROM transactions t
            LEFT JOIN transfer_links tl ON tl.txn_id = t.id
            WHERE t.direction='debit' AND tl.txn_id IS NULL
              AND t.category IN ('TRANSFER_OUT','LOAN_PAYMENTS')"""
    ).fetchone()
    return {"pairs": linked, "internal_cents": linked_cents,
            "unmatched_outbound": unmatched["n"],
            "unmatched_outbound_cents": unmatched["c"]}


def main(argv: list[str] | None = None) -> int:
    cfg = Config.load()
    with Store(cfg.db_path) as store:
        r = match_transfers(store)
        print(f"pairs={r['pairs']} internal=A${r['internal_cents']/100:,.0f} "
              f"unmatched_outbound={r['unmatched_outbound']} "
              f"(A${r['unmatched_outbound_cents']/100:,.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
