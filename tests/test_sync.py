"""End-to-end sync against the fake API — the whole flow, keyless."""

from spendglass.sync import sync
from tests.conftest import TXNS


def test_full_sync_populates_every_table(store, client):
    result = sync(store, client)
    assert result["status"] == "ok"
    assert result["connections"] == 3
    assert result["accounts"] == 3
    assert result["transactions"] == len(TXNS)
    assert result["balances"] == 2
    assert result["holdings"] == 1
    assert result["trades"] == 1
    assert result["categories"] == 2
    # the invalidated connection is warned about, not silently skipped
    assert any("Lapsed Bank" in w for w in result["warnings"])


def test_second_sync_is_idempotent(store, client):
    sync(store, client)
    result = sync(store, client)
    assert result["status"] == "ok"
    count = store.con.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    assert count == len(TXNS)  # overlap window re-fetched, nothing duplicated


def test_dead_connection_accounts_not_fetched(store, client, api):
    sync(store, client)
    fetched_accounts = {
        dict(c.url.params).get("accountId")
        for c in api.calls if c.url.path == "/v1/transactions"
    }
    assert "acc-dead" not in fetched_accounts  # invalidated conn can't serve data
    assert "acc-1" in fetched_accounts


def test_plan_gated_brokerage_degrades_gracefully(store, client, api):
    api.plan_gated = True
    result = sync(store, client)
    assert result["status"] == "ok"          # the run still succeeds
    assert result["holdings"] == 0
    assert result["plan_gated"]              # ...and says why, loudly
    assert result["transactions"] == len(TXNS)  # banking side unaffected


def test_incremental_sync_uses_overlap_window(store, client, api):
    # Posted rows only: a pending row older than the overlap would pull the
    # window back to itself (#47), which is the next test's subject.
    api.txns = [TXNS[0]]
    sync(store, client)
    api.calls.clear()
    sync(store, client)
    txn_calls = [c for c in api.calls if c.url.path == "/v1/transactions"]
    assert txn_calls, "second sync must still fetch transactions"
    from_param = dict(txn_calls[0].url.params)["from"]
    # Second run starts from (last_synced_to - overlap), not the full backfill.
    # Basis must be UTC, matching sync._today(): comparing against a LOCAL
    # date.today() passes only while the two agree, then fails for the hours
    # each day a UTC+10 locale sits a date ahead of UTC. The watermark is UTC
    # on purpose — a sync boundary that moves with the local timezone would
    # re-fetch or skip a day at every DST shift and every flight.
    from datetime import datetime, timedelta, timezone
    expected = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    assert from_param == expected


def test_sync_run_recorded(store, client):
    sync(store, client)
    health = store.health()
    assert health["last_sync"]["status"] == "ok"
    assert health["stale"] is False


def _acc1_rows(store) -> dict:
    return {r["id"]: r["status"] for r in store.con.execute(
        "SELECT id, status FROM transactions WHERE account_id='acc-1'")}


def test_settled_pending_row_is_pruned(store, client, api):
    """The #47 ghost: a pending authorisation posts under a NEW id, the old
    id stops coming back, and the store used to keep both rows forever."""
    from spendglass.transfers import ensure_schema
    sync(store, client)
    assert _acc1_rows(store) == {"txn-1": "posted", "txn-2": "pending"}
    ensure_schema(store)
    store.con.executemany(
        "INSERT INTO transfer_links (txn_id, peer_txn_id, kind) VALUES (?,?,'internal')",
        [("txn-2", "txn-x"), ("txn-x", "txn-2")])
    store.con.commit()
    posted_twin = dict(TXNS[1], id="txn-2-posted", status="posted",
                       date="2026-08-02", postDate="2026-08-02",
                       description="SALARY, POSTED")
    api.txns = [TXNS[0], posted_twin]
    result = sync(store, client)
    assert result["pruned"] == 1
    assert _acc1_rows(store) == {"txn-1": "posted", "txn-2-posted": "posted"}
    # links on either side of the ghost went with it; the match rebuilds them
    assert store.con.execute("SELECT COUNT(*) FROM transfer_links").fetchone()[0] == 0


def test_posted_row_missing_from_response_survives(store, client, api):
    sync(store, client)
    api.txns = [TXNS[1]]  # the provider goes quiet about the posted txn-1
    result = sync(store, client)
    assert result["pruned"] == 0
    assert _acc1_rows(store) == {"txn-1": "posted", "txn-2": "pending"}


def test_pending_row_still_returned_survives(store, client, api):
    sync(store, client)
    result = sync(store, client)
    assert result["pruned"] == 0
    assert _acc1_rows(store)["txn-2"] == "pending"


def test_window_reaches_back_to_the_oldest_pending_row(store, client, api):
    """Ghosts older than the overlap (the installed base) fall inside the
    window on the next sync and are pruned by the same rule; after that the
    window shrinks back towards the overlap on its own."""
    api.txns = [TXNS[0]]
    sync(store, client)
    ghost = dict(TXNS[1], id="txn-old", date="2026-06-01",
                 datetime="2026-06-01T00:00:00Z")
    store.upsert_transactions([ghost], "conn-bank-1")
    api.calls.clear()
    result = sync(store, client)
    first = [c for c in api.calls if c.url.path == "/v1/transactions"][0]
    assert dict(first.url.params)["from"] == "2026-06-01"
    assert result["pruned"] == 1
    assert "txn-old" not in _acc1_rows(store)
    api.calls.clear()
    sync(store, client)
    again = [c for c in api.calls if c.url.path == "/v1/transactions"][0]
    assert dict(again.url.params)["from"] > "2026-06-01"
