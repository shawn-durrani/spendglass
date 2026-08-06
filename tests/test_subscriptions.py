"""Subscription detector — annualisation, duplicates, stopped, renewals."""

from datetime import date

import pytest

from spendglass import subscriptions
from spendglass.enrich import ensure_schema as enrich_schema
from spendglass.lookup import ensure_schema as lookup_schema
from spendglass.store import Store

TODAY = date(2026, 8, 5)


@pytest.fixture()
def con(tmp_path):
    with Store(tmp_path / "store.db") as s:
        enrich_schema(s)
        lookup_schema(s)

        def rec(key, cadence, amount, last_seen, next_expected, interval):
            s.con.execute(
                """INSERT INTO recurring_charges (merchant_key, display_name,
                   cadence, occurrences, typical_amount, amount_min, amount_max,
                   first_seen, last_seen, next_expected, median_interval_days)
                   VALUES (?,?,?,6,?,?,?,'2025-01-01',?,?,?)""",
                (key, key.title(), cadence, amount, amount, amount,
                 last_seen, next_expected, interval))

        # Active monthly; renews inside the 14-day window.
        rec("vendor a monthly", "monthly", "-31.00", "2026-07-15", "2026-08-14", 30)
        # Active yearly — the "don't read a spike as monthly" case.
        rec("vendor b yearly", "yearly", "-520.00", "2026-06-13", "2027-06-13", 365)
        # Duplicate vendor: two active lines resolving to one name.
        rec("vendor a second line", "monthly", "-30.00", "2026-07-20", "2026-08-19", 30)
        s.con.execute(
            """INSERT INTO merchant_lookups (merchant_key, status, resolved_name)
               VALUES ('vendor a monthly','approved','Vendor A'),
                      ('vendor a second line','approved','Vendor A')""")
        # Stopped: last seen far beyond its cadence grace.
        rec("vendor c dead", "fortnightly", "-55.00", "2025-03-28", "2025-04-11", 14)
        s.con.commit()
        yield s.con


def test_annualised_costs_are_cadence_aware(con):
    o = subscriptions.overview(con, today=TODAY)
    by = {a["merchant_key"]: a for a in o["active"]}
    assert by["vendor b yearly"]["annualised_cents"] == 52000
    assert by["vendor b yearly"]["monthly_equiv_cents"] == 4333
    assert by["vendor b yearly"]["billed_infrequently"]
    assert by["vendor a monthly"]["annualised_cents"] == 3100 * 12


def test_duplicate_vendor_lines_are_flagged(con):
    o = subscriptions.overview(con, today=TODAY)
    assert len(o["duplicates"]) == 1
    d = o["duplicates"][0]
    assert d["name"] == "Vendor A" and d["lines"] == 2
    assert d["combined_annualised_cents"] == (3100 + 3000) * 12


def test_stopped_charges_leave_the_active_list(con):
    o = subscriptions.overview(con, today=TODAY)
    assert all(a["merchant_key"] != "vendor c dead" for a in o["active"])
    assert any(x["merchant_key"] == "vendor c dead" for x in o["stopped"])


def test_renewing_soon_window(con):
    o = subscriptions.overview(con, today=TODAY)
    soon = {a["merchant_key"] for a in o["renewing_soon"]}
    assert "vendor a monthly" in soon          # 9 days out
    assert "vendor b yearly" not in soon       # next June
