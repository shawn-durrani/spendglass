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
