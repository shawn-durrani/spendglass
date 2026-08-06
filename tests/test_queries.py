"""MCP query layer: structural read-only, loud freshness, SQL-exact money."""

import sqlite3

import pytest

from spendglass import queries
from spendglass.store import Store
from spendglass.sync import sync
from tests.conftest import ACCOUNTS, CATEGORIES, CONNECTIONS, TXNS


@pytest.fixture()
def populated(tmp_path):
    db = tmp_path / "store.db"
    with Store(db) as s:
        s.upsert_connections(CONNECTIONS)
        s.upsert_accounts(ACCOUNTS)
        s.upsert_categories(CATEGORIES)
        s.upsert_transactions(TXNS, "conn-bank-1")
        s.insert_balance_snapshots([
            {"accountId": "acc-1", "currentBalance": "2500.10",
             "availableBalance": "2400.00", "currency": "AUD"},
        ])
    return db


def test_readonly_connection_refuses_writes(populated):
    """The invariant, enforced by SQLite itself — not by convention."""
    con = queries.open_readonly(populated)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("DELETE FROM transactions")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("INSERT INTO categories (key, label) VALUES ('X','X')")
    con.close()


def test_freshness_screams_when_never_synced(populated):
    f = queries.freshness(populated)
    assert f["stale"] is True
    assert any("no successful sync" in w for w in f["staleness_warnings"])
    assert f["as_of"] is None


def test_freshness_carries_connection_warnings(populated, client):
    with Store(populated) as s:
        sync(s, client)  # fake API: includes the invalidated Lapsed Bank
    f = queries.freshness(populated)
    assert any("Lapsed Bank" in w for w in f["staleness_warnings"])


def test_list_accounts_joins_balance_and_consent(populated):
    con = queries.open_readonly(populated)
    accounts = {a["id"]: a for a in queries.list_accounts(con)}
    con.close()
    assert accounts["acc-1"]["current_balance"] == "2500.10"
    assert accounts["acc-1"]["connection_status"] == "active"
    assert accounts["acc-dead"]["connection_status"] == "invalidated"


def test_search_net_amount_is_sql_exact(populated):
    con = queries.open_readonly(populated)
    r = queries.search_transactions(con)
    con.close()
    assert r["matched"] == len(TXNS)
    assert r["net_amount"] == "995.50"     # -4.50 + 1000.00, in SQL cents


def test_search_filters_compose(populated):
    con = queries.open_readonly(populated)
    r = queries.search_transactions(con, direction="debit", q="coffee")
    assert r["matched"] == 1
    assert r["transactions"][0]["merchant_name"] == "Coffee Co"
    r = queries.search_transactions(con, direction="credit", q="coffee")
    assert r["matched"] == 0
    con.close()


def test_summary_groups_and_totals(populated):
    con = queries.open_readonly(populated)
    r = queries.spending_summary(con, group_by="category", direction="both")
    con.close()
    by_group = {g["group"]: g for g in r["groups"]}
    assert by_group["EATING_OUT"]["amount"] == "-4.50"
    assert by_group["INCOME"]["amount"] == "1000.00"
    assert r["total_amount"] == "995.50"


def test_summary_rejects_unknown_group(populated):
    con = queries.open_readonly(populated)
    with pytest.raises(ValueError, match="group_by"):
        queries.spending_summary(con, group_by="1; DROP TABLE transactions")
    con.close()


def test_mcp_server_exposes_only_readonly_tools():
    """The tool surface IS the security surface — pin it."""
    import asyncio

    from spendglass.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = sorted(t.name for t in tools)
    # Eight trend tools added 2026-08-04, theme_spend + subscriptions on
    # 2026-08-05 — deterministic SQL analytics, still read-only, still no
    # network. A reviewed decision, not drift.
    assert names == ["category_momentum", "fixed_vs_variable",
                     "list_accounts", "list_recurring_charges",
                     "merchant_pareto", "price_creep", "savings_wins",
                     "search_transactions", "spend_change_waterfall",
                     "spending_anomalies", "spending_deltas",
                     "spending_run_rate", "spending_summary",
                     "store_health", "subscriptions", "theme_spend"]
    # Nothing here writes, transfers, pays, or connects — by name or nature.
    forbidden = ("write", "create", "delete", "update", "transfer", "pay", "send")
    assert not [n for n in names for f in forbidden if f in n]
