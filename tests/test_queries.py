"""MCP query layer: structural read-only, loud freshness, SQL-exact money."""

import sqlite3

import pytest

from spendglass import queries
from spendglass.enrich import ensure_schema as enrich_schema
from spendglass.lookup import ensure_schema as lookup_schema
from spendglass.overrides import ensure_schema as overrides_schema
from spendglass.store import Store
from spendglass.sync import sync
from spendglass.transfers import ensure_schema as transfers_schema
from tests.conftest import ACCOUNTS, CATEGORIES, CONNECTIONS, TXNS


@pytest.fixture()
def populated(tmp_path):
    db = tmp_path / "store.db"
    with Store(db) as s:
        # Same derived schemas ui.py's create_app and mcp_server.py's
        # _ensure_derived_schema ensure at startup — queries.py's effective-
        # category formula (issue #90/#91) joins against these.
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)
        overrides_schema(s)
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


# ── effective category/subcategory (issue #90/#91 follow-up) ───────────────
# Ground-truth review found search_transactions/spending_summary still
# filtering/grouping by the bank's raw t.category — exactly the tools that
# produced the misleading BANK_FEES/Eating Out/MERCHANDISE figures #90's
# override rules were meant to fix. These prove the MCP-facing read paths
# now show the corrected figures, with the raw bank value still reachable
# explicitly, and that not one raw transaction column moved underneath.

def _txn(s, i, date, description, merchant_name, cents, category,
         direction="debit"):
    key = merchant_name.strip().casefold()
    s.con.execute(
        """INSERT INTO transactions (id, account_id, date, description,
           merchant_name, merchant_key, amount, amount_cents, direction,
           category, status, raw, connection_id, first_seen_at, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,'posted','{"category":"%s"}','c1',?,?)"""
        % category,
        (f"t{i}", "a1", date, description, merchant_name, key,
         str(cents / 100), cents if direction == "credit" else -abs(cents),
         direction, category, date, date))


@pytest.fixture()
def reclassified(tmp_path):
    """A mix of merchants: some caught by a built-in override rule, one
    overridden further by a user's own merchant decision, and one that
    matches no rule at all — proving rule preference and non-interference
    together, not just each in isolation."""
    with Store(tmp_path / "store.db") as s:
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)
        overrides_schema(s)
        _txn(s, 1, "2026-01-05", "Interest charged", "Interest charged",
             460000, "BANK_FEES")
        _txn(s, 2, "2026-01-06", "BWS FAIRHAVEN", "BWS", 4200, "EATING_OUT")
        _txn(s, 3, "2026-01-07", "SAMPLE CAFE FAIRHAVEN", "Sample Cafe",
             1500, "EATING_OUT")
        # A user's own merchant decision still outranks the built-in rule.
        s.con.execute(
            """INSERT INTO merchant_lookups (merchant_key, status,
               resolved_name, resolved_category, resolved_subcategory)
               VALUES ('bws', 'overridden', 'BWS', 'EATING_OUT', 'Takeaway')""")
        s.con.commit()
        raw_before = [dict(r) for r in s.con.execute(
            "SELECT * FROM transactions ORDER BY id")]
    return tmp_path / "store.db", raw_before


def test_search_transactions_filters_by_effective_category(reclassified):
    db, _ = reclassified
    con = queries.open_readonly(db)
    r = queries.search_transactions(con, category="LOAN_INTEREST")
    assert r["matched"] == 1
    assert r["transactions"][0]["id"] == "t1"
    # Searching the bank's own raw code still finds it under BANK_FEES —
    # nothing about the raw column changed, only what's reported as current.
    raw = queries.search_transactions(con, category="BANK_FEES")
    assert raw["matched"] == 0        # effective, not raw: this is correct
    con.close()


def test_search_transactions_exposes_effective_and_raw_category(reclassified):
    db, _ = reclassified
    con = queries.open_readonly(db)
    r = queries.search_transactions(con, q="interest")
    con.close()
    row = r["transactions"][0]
    assert row["category"] == "LOAN_INTEREST"       # corrected
    assert row["raw_category"] == "BANK_FEES"        # bank's own code, intact
    assert row["subcategory"] == "Mortgage Interest"


def test_search_transactions_manual_override_beats_builtin_rule(reclassified):
    db, _ = reclassified
    con = queries.open_readonly(db)
    r = queries.search_transactions(con, q="bws")
    con.close()
    row = r["transactions"][0]
    assert row["category"] == "EATING_OUT"           # the user's own call
    assert row["subcategory"] == "Takeaway"
    assert row["raw_category"] == "EATING_OUT"        # bank agreed, here


def test_spending_summary_groups_by_effective_category(reclassified):
    db, _ = reclassified
    con = queries.open_readonly(db)
    r = queries.spending_summary(con, group_by="category", direction="debit")
    con.close()
    by_group = {g["group"]: g["amount"] for g in r["groups"]}
    assert by_group["LOAN_INTEREST"] == "-4600.00"
    assert by_group["EATING_OUT"] == "-57.00"         # BWS (user) + café
    assert "BANK_FEES" not in by_group                # no longer misfiled here


def test_spending_summary_raw_category_still_reachable(reclassified):
    db, _ = reclassified
    con = queries.open_readonly(db)
    r = queries.spending_summary(con, group_by="raw_category", direction="debit")
    con.close()
    by_group = {g["group"]: g["amount"] for g in r["groups"]}
    assert by_group["BANK_FEES"] == "-4600.00"        # the bank's own figure
    assert by_group["EATING_OUT"] == "-57.00"


def test_spending_summary_groups_by_effective_subcategory(reclassified):
    db, _ = reclassified
    con = queries.open_readonly(db)
    r = queries.spending_summary(con, group_by="subcategory", direction="debit")
    con.close()
    by_group = {g["group"]: g["amount"] for g in r["groups"]}
    assert by_group["Mortgage Interest"] == "-4600.00"
    assert by_group["Takeaway"] == "-42.00"
    assert by_group["(no subcategory)"] == "-15.00"   # Sample Cafe: no rule


def test_reclassification_leaves_raw_transaction_rows_unchanged(reclassified):
    db, raw_before = reclassified
    con = queries.open_readonly(db)
    queries.search_transactions(con)
    queries.spending_summary(con, group_by="category")
    con.close()
    with Store(db) as s:
        raw_after = [dict(r) for r in s.con.execute(
            "SELECT * FROM transactions ORDER BY id")]
    assert raw_after == raw_before


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
