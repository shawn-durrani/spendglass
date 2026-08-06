"""Trend-device math — synthetic store, deterministic assertions.

Months are generated relative to today because the devices anchor on 'the
most recent complete months'; no wall-clock values are asserted, only the
arithmetic."""

from datetime import date

import pytest

from spendglass import trends
from spendglass.enrich import ensure_schema as enrich_schema
from spendglass.lookup import ensure_schema as lookup_schema
from spendglass.store import Store
from spendglass.transfers import ensure_schema as transfers_schema


def _month_offset(n: int) -> str:
    """YYYY-MM for n complete months before the current month."""
    y, m = date.today().year, date.today().month
    m -= n
    while m < 1:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


@pytest.fixture()
def con(tmp_path):
    with Store(tmp_path / "store.db") as s:
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)

        def txn(i, day, cents, key, cat):
            s.con.execute(
                """INSERT INTO transactions (id, account_id, date, description,
                   merchant_name, amount, amount_cents, direction, category,
                   status, raw, connection_id, first_seen_at, synced_at)
                   VALUES (?,?,?,?,?,?,?,'debit',?,'posted','{}','c1',?,?)""",
                (f"t{i}", "a1", day, key, key, str(cents / -100), -cents, cat,
                 day, day))
            s.con.execute(
                "UPDATE transactions SET merchant_key=? WHERE id=?", (key, f"t{i}"))

        i = 0
        # Two complete months with a known category delta for the waterfall:
        # groceries flat at $500, dining $200 -> $500 (+$300).
        for n, dining in ((2, 20000), (1, 50000)):
            mo = _month_offset(n)
            txn(i := i + 1, f"{mo}-10", 50000, "grocer", "FOOD_AND_DRINK")
            txn(i := i + 1, f"{mo}-12", dining, "diner", "ENTERTAINMENT")
        # A recurring stream that crept $10.00 -> $13.00 monthly.
        for n in range(6, 0, -1):
            cents = 1000 if n >= 4 else 1300
            txn(i := i + 1, f"{_month_offset(n)}-05", cents, "streamer", "SERVICES")
        s.con.execute(
            """INSERT INTO recurring_charges (merchant_key, display_name,
               cadence, occurrences, typical_amount, amount_min, amount_max,
               first_seen, last_seen, next_expected, median_interval_days)
               VALUES ('streamer','Streamer','monthly',6,'13.00','10.00',
                       '13.00',?,?,?,30)""",
            (f"{_month_offset(6)}-05", f"{_month_offset(1)}-05",
             f"{_month_offset(0)}-05"))
        s.con.commit()
        yield s.con


def test_waterfall_decomposes_the_delta(con):
    w = trends.waterfall(con)
    assert w["available"]
    assert w["cur_total_cents"] - w["prev_total_cents"] == 30000
    movers = {m["category"]: m["delta_cents"] for m in w["movers"]}
    assert movers["ENTERTAINMENT"] == 30000       # the story, not just a total
    assert movers.get("FOOD_AND_DRINK", 0) == 0 or "FOOD_AND_DRINK" not in movers


def test_price_creep_annualises(con):
    c = trends.price_creep(con)
    assert c["available"]
    item = next(x for x in c["items"] if x["merchant"] == "streamer")
    assert item["early_cents"] == 1000 and item["late_cents"] == 1300
    assert item["annualised_cents"] == 300 * 12   # monthly cadence


def test_transfers_never_count_as_spend(con):
    con.execute(
        """INSERT INTO transactions (id, account_id, date, description,
           merchant_name, amount, amount_cents, direction, category, status,
           raw, connection_id, first_seen_at, synced_at)
           VALUES ('tr1','a1',?,'transfer','transfer','-999.00',-99900,
                   'debit','TRANSFER_OUT','posted','{}','c1',?,?)""",
        (f"{_month_offset(1)}-15", f"{_month_offset(1)}-15",
         f"{_month_offset(1)}-15"))
    con.commit()
    w = trends.waterfall(con)
    assert all(m["category"] != "TRANSFER_OUT" for m in w["movers"])


def test_loan_interest_counts_as_spend_principal_does_not(tmp_path):
    """Interest is money gone forever; principal moves the user's own
    balance sheet. The spend gate splits LOAN_PAYMENTS at the subcategory."""
    with Store(tmp_path / "loans.db") as s:
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)
        for i, (key, sub, cents) in enumerate([
                ("interest charged", "Mortgage Interest", 400000),
                ("loan repayment", "Principal", 150000)]):
            s.con.execute(
                """INSERT INTO transactions (id, account_id, date, description,
                   merchant_name, amount, amount_cents, direction, category,
                   status, raw, connection_id, first_seen_at, synced_at)
                   VALUES (?,?,?,?,?,?,?,'debit','LOAN_PAYMENTS','posted','{}','c1',?,?)""",
                (f"loan{i}", "a1", "2026-01-15", key, key,
                 str(cents / -100), -cents, "2026-01-15", "2026-01-15"))
            s.con.execute(
                "UPDATE transactions SET merchant_key=? WHERE id=?",
                (key, f"loan{i}"))
            s.con.execute(
                """INSERT OR REPLACE INTO merchant_lookups
                   (merchant_key, status, resolved_name, resolved_category,
                    resolved_subcategory)
                   VALUES (?,?,?,?,?)""",
                (key, "overridden", key.title(), "LOAN_PAYMENTS", sub))
        s.con.commit()
        row = s.con.execute(
            f"""SELECT ABS(COALESCE(SUM(t.amount_cents),0)) c {trends.JOIN}
                WHERE {trends.SPEND}""").fetchone()
        assert row["c"] == 400000  # interest in, principal out
