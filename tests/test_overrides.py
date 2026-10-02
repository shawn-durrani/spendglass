"""Category overrides (issue #90) — a derived, reversible rules layer that
fixes known bank mislabelling. The two properties that matter most: the raw
transaction row never changes, and a deleted rule stays deleted."""

import sqlite3

import pytest

from spendglass import overrides, trends
from spendglass.enrich import ensure_schema as enrich_schema
from spendglass.lookup import ensure_schema as lookup_schema
from spendglass.store import Store
from spendglass.transfers import ensure_schema as transfers_schema


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "store.db") as s:
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)
        overrides.ensure_schema(s)
        yield s


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


# ── schema: additive, idempotent, seeded once ───────────────────────────────

def test_virgin_store_seeds_builtin_rules(tmp_path):
    with Store(tmp_path / "virgin.db") as s:
        overrides.ensure_schema(s)
        rules = overrides.list_rules(s.con)
        assert len(rules) == len(overrides.BUILTIN_RULES)
        cats = {r["key"] for r in s.con.execute("SELECT key FROM categories")}
        assert cats >= {k for k, _ in overrides.NEW_CATEGORIES}


def test_ensure_schema_is_idempotent(store):
    before = overrides.list_rules(store.con)
    overrides.ensure_schema(store)
    overrides.ensure_schema(store)
    assert overrides.list_rules(store.con) == before


def test_deleted_rule_stays_deleted(store):
    rule_id = overrides.list_rules(store.con)[0]["id"]
    assert overrides.remove_rule(store, rule_id)
    overrides.ensure_schema(store)          # never resurrects
    assert rule_id not in {r["id"] for r in overrides.list_rules(store.con)}
    assert not overrides.remove_rule(store, rule_id)   # already gone


def test_add_rule_validates_and_is_idempotent(store):
    with pytest.raises(ValueError):
        overrides.add_rule(store, "colour", "x%", "ALCOHOL")
    with pytest.raises(ValueError):
        overrides.add_rule(store, "merchant", "  ", "ALCOHOL")
    with pytest.raises(ValueError):
        overrides.add_rule(store, "merchant", "x%", "")
    before = len(overrides.list_rules(store.con))
    overrides.add_rule(store, "merchant", "%cellarbrations%", "ALCOHOL", "Bottle Shop")
    overrides.add_rule(store, "merchant", "%cellarbrations%", "ALCOHOL", "Bottle Shop")
    assert len(overrides.list_rules(store.con)) == before + 1   # no dupe


# ── reclassification: before/after totals, raw rows untouched ──────────────

def test_interest_reclassified_out_of_bank_fees(store):
    _txn(store, 1, "2026-01-05", "Interest charged", "Interest charged",
         460000, "BANK_FEES")
    _txn(store, 2, "2026-01-07", "INTEREST ON CASH ADV", "INTEREST ON CASH ADV",
         3200, "BANK_FEES")
    _txn(store, 3, "2026-01-10", "Monthly account fee", "Monthly account fee",
         500, "BANK_FEES")
    store.con.commit()

    raw_before = [dict(r) for r in store.con.execute(
        "SELECT * FROM transactions ORDER BY id")]

    before = dict(store.con.execute(
        f"SELECT t.category k, ABS(SUM(t.amount_cents)) c {trends.JOIN} "
        "WHERE t.direction='debit' GROUP BY k").fetchall())
    assert before == {"BANK_FEES": 463700}

    after = {r["k"]: r["c"] for r in store.con.execute(
        f"SELECT {trends.ECAT} k, ABS(SUM(t.amount_cents)) c {trends.JOIN} "
        "WHERE t.direction='debit' GROUP BY k")}
    assert after == {"BANK_FEES": 500, "LOAN_INTEREST": 463200}

    raw_after = [dict(r) for r in store.con.execute(
        "SELECT * FROM transactions ORDER BY id")]
    assert raw_after == raw_before             # not one raw column moved


def test_bottle_shops_reclassified_out_of_eating_out(store):
    _txn(store, 1, "2026-01-05", "LIQUORLAND FAIRHAVEN", "Liquorland",
         6500, "EATING_OUT")
    _txn(store, 2, "2026-01-06", "BWS FAIRHAVEN", "BWS", 4200, "EATING_OUT")
    _txn(store, 3, "2026-01-07", "DAN MURPHY'S FAIRHAVEN", "Dan Murphy's",
         9000, "EATING_OUT")
    _txn(store, 4, "2026-01-08", "SAMPLE CAFE FAIRHAVEN", "Sample Cafe",
         1500, "EATING_OUT")
    store.con.commit()
    raw_before = [dict(r) for r in store.con.execute(
        "SELECT * FROM transactions ORDER BY id")]

    after = {r["k"]: r["c"] for r in store.con.execute(
        f"SELECT {trends.ECAT} k, ABS(SUM(t.amount_cents)) c {trends.JOIN} "
        "WHERE t.direction='debit' GROUP BY k")}
    assert after == {"ALCOHOL": 19700, "EATING_OUT": 1500}
    assert [dict(r) for r in store.con.execute(
        "SELECT * FROM transactions ORDER BY id")] == raw_before


def test_ai_and_family_transfers_reclassified_out_of_shopping(store):
    _txn(store, 1, "2026-01-05", "ANTHROPIC API", "Anthropic", 16000,
         "MERCHANDISE")
    _txn(store, 2, "2026-01-06", "SPRIGGY TOPUP", "Spriggy", 3400,
         "MERCHANDISE")
    _txn(store, 3, "2026-01-07", "SAMPLE STORE", "Sample Store", 2200,
         "MERCHANDISE")
    store.con.commit()

    after = {r["k"]: r["c"] for r in store.con.execute(
        f"SELECT {trends.ECAT} k, ABS(SUM(t.amount_cents)) c {trends.JOIN} "
        "WHERE t.direction='debit' GROUP BY k")}
    assert after == {"SOFTWARE_AI": 16000, "FAMILY_TRANSFERS": 3400,
                      "MERCHANDISE": 2200}


def test_manual_override_still_wins_over_builtin_rule(store):
    """A user's explicit merchant decision outranks the deterministic rule —
    COALESCE(ml.resolved_category, OCAT, ...)."""
    _txn(store, 1, "2026-01-05", "BWS FAIRHAVEN", "BWS", 4200, "EATING_OUT")
    store.con.execute(
        """INSERT INTO merchant_lookups (merchant_key, status, resolved_name,
           resolved_category) VALUES ('bws', 'overridden', 'BWS', 'EATING_OUT')""")
    store.con.commit()
    row = store.con.execute(
        f"SELECT {trends.ECAT} k {trends.JOIN} WHERE t.id='t1'").fetchone()
    assert row["k"] == "EATING_OUT"


# ── fixed_vs_variable and merchant_pareto treat interest as fixed ──────────

def test_fixed_floor_counts_loan_interest_as_fixed(store):
    for n in range(1, 4):
        _txn(store, n, f"2026-0{n}-05", "Interest charged", "Interest charged",
             400000 + n * 100, "BANK_FEES")
    store.con.commit()
    f = trends.fixed_floor(store.con, months_back=24)
    assert f["available"]
    assert sum(f["variable_cents"]) == 0
    assert sum(f["fixed_cents"]) > 0


def test_pareto_excludes_loan_interest_from_variable_spend(store):
    _txn(store, 1, "2026-01-05", "Interest charged", "Interest charged",
         460000, "BANK_FEES")
    _txn(store, 2, "2026-01-06", "SAMPLE STORE", "Sample Store", 5000,
         "MERCHANDISE")
    store.con.commit()
    p = trends.pareto(store.con, days=365)
    names = {m["merchant"] for m in p["merchants"]}
    assert "Interest charged" not in names
    assert p["variable_total_cents"] == 5000
