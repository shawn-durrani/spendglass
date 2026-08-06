"""Sync — pull everything the API offers into the store, loudly.

Flow per run:
  1. connections  → upsert; non-active status is a warning, not a failure
  2. accounts     → upsert (paginated)
  3. categories   → upsert
  4. balances     → append snapshot for every account
  5. per banking account: transactions, windowed from last success (with
     overlap) or backfill_days on first run; per-account fan-out because
     accountId is required upstream
  6. per brokerage account: holdings + trades (366-day windows); a 403
     means the plan doesn't include them — recorded, not fatal
  7. sync_run + per-account state recorded either way

Run it:  .venv/bin/python -m spendglass.sync
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone

from .client import PlanGated, RedbarkClient, RedbarkError
from .config import Config
from .store import Store

# Re-fetch this many days before the last synced date: pending transactions
# settle (status/postDate change) and providers late-post; upserts make the
# overlap free.
OVERLAP_DAYS = 7


def _today() -> date:
    return datetime.now(timezone.utc).date()


def sync(store: Store, client: RedbarkClient, backfill_days: int = 365,
         trades_max_window_days: int = 366) -> dict:
    run_id = store.start_sync_run()
    details: dict = {"connections": 0, "accounts": 0, "categories": 0, "balances": 0,
                     "transactions": 0, "holdings": 0, "trades": 0,
                     "warnings": [], "plan_gated": []}
    try:
        connections = client.connections()
        details["connections"] = store.upsert_connections(connections)
        for c in connections:
            if c.get("status") != "active":
                details["warnings"].append(
                    f"connection {c.get('institutionName', c['id'])}: status "
                    f"'{c.get('status')}' — its data is not updating"
                )

        accounts = client.accounts()
        details["accounts"] = store.upsert_accounts(accounts)
        details["categories"] = store.upsert_categories(client.categories())

        if accounts:
            details["balances"] = store.insert_balance_snapshots(
                client.balances([a["id"] for a in accounts])
            )

        by_connection = {c["id"]: c for c in connections}
        today = _today().isoformat()

        for account in accounts:
            conn = by_connection.get(account.get("connectionId") or "")
            if conn is None or conn.get("status") != "active":
                continue  # inactive connections can't serve data; warned above

            if conn.get("category") == "brokerage":
                try:
                    details["holdings"] += store.upsert_holdings(
                        client.holdings(conn["id"], account["id"])
                    )
                    details["trades"] += _sync_trades(
                        store, client, conn["id"], account["id"],
                        backfill_days, trades_max_window_days,
                    )
                    store.mark_account_synced(account["id"], today)
                except PlanGated as e:
                    details["plan_gated"].append(str(e))
                continue

            # banking: transactions from the last success (minus overlap) or backfill
            state = store.get_account_sync_state(account["id"])
            if state and state["last_synced_to"]:
                from_date = (
                    date.fromisoformat(state["last_synced_to"]) - timedelta(days=OVERLAP_DAYS)
                ).isoformat()
            else:
                from_date = (_today() - timedelta(days=backfill_days)).isoformat()
            details["transactions"] += store.upsert_transactions(
                list(client.transactions(conn["id"], account["id"], from_date)),
                conn["id"],
            )
            store.mark_account_synced(account["id"], today)

        store.finish_sync_run(run_id, "ok", details)
        return {"status": "ok", "run_id": run_id, **details}
    except (RedbarkError, OSError) as e:
        store.finish_sync_run(run_id, "error", details, error=str(e))
        return {"status": "error", "run_id": run_id, "error": str(e), **details}


def _sync_trades(store: Store, client: RedbarkClient, connection_id: str,
                 account_id: str, backfill_days: int, max_window: int) -> int:
    """Trades are capped at 366-day windows upstream — chunk the backfill."""
    total = 0
    end = _today()
    start = end - timedelta(days=backfill_days)
    while start < end:
        window_end = min(start + timedelta(days=max_window), end)
        total += store.upsert_trades(
            list(client.trades(connection_id, account_id,
                               start.isoformat(), window_end.isoformat()))
        )
        start = window_end
    return total


def main() -> int:
    cfg = Config.load()
    if not cfg.api_key:
        print("REDBARK_API_KEY is not set — copy .env.example to .env and add your key.",
              file=sys.stderr)
        return 2
    with Store(cfg.db_path) as store, RedbarkClient(cfg.api_key, cfg.api_url) as client:
        result = sync(store, client, cfg.backfill_days, cfg.trades_max_window_days)
        ok = result["status"] == "ok"
        print(f"sync {'✓' if ok else '✖'} run={result['run_id']} "
              + " ".join(f"{k}={result[k]}" for k in
                         ("connections", "accounts", "transactions", "balances",
                          "holdings", "trades")))
        for w in result.get("warnings", []):
            print(f"  ⚠ {w}")
        for p in result.get("plan_gated", []):
            print(f"  ○ {p}")
        if not ok:
            print(f"  error: {result.get('error')}", file=sys.stderr)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
