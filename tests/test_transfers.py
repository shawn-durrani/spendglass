"""Transfer matching — the facts must be provable, never coincidental."""

import pytest

from spendglass.enrich import merchant_key
from spendglass.store import Store
from spendglass.transfers import ensure_schema, match_transfers


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "store.db") as s:
        ensure_schema(s)

        def txn(i, acct, day, cents, direction, desc, cat):
            s.con.execute(
                """INSERT INTO transactions (id, account_id, date, description,
                   merchant_name, amount, amount_cents, direction, category,
                   status, raw, connection_id, first_seen_at, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'posted','{}','c1',?,?)""",
                (f"t{i}", acct, day, desc, desc, str(cents / 100), cents,
                 direction, cat, day, day))

        # A clean internal pair: -$500 out of a1, +$500 into a2 next day.
        txn(1, "a1", "2026-01-10", -50000, "debit",
            "WITHDRAWAL ONLINE 111 TFR Savings", "TRANSFER_OUT")
        txn(2, "a2", "2026-01-11", 50000, "credit",
            "DEPOSIT ONLINE 222 TFR Savings", "TRANSFER_IN")
        # A one-sided payment to a person — no matching credit anywhere.
        txn(3, "a1", "2026-01-12", -900000, "debit",
            "Transfer To A Tradie, App 123", "TRANSFER_OUT")
        # Equal amounts that are NOT transfers must never pair.
        txn(4, "a1", "2026-01-13", -7700, "debit",
            "DEBIT CARD PURCHASE Some Cafe", "FOOD_AND_DRINK")
        txn(5, "a2", "2026-01-13", 7700, "credit",
            "DEBIT CARD REFUND Some Shop", "INCOME")
        # A pair outside the window must not match.
        txn(6, "a1", "2026-02-01", -30000, "debit",
            "WITHDRAWAL MOBILE 333 TFR Holiday", "TRANSFER_OUT")
        txn(7, "a2", "2026-02-09", 30000, "credit",
            "DEPOSIT ONLINE 444 TFR Holiday", "TRANSFER_IN")
        s.con.commit()
        yield s


def test_two_leg_match_links_both_and_only_transfers(store):
    r = match_transfers(store)
    assert r["pairs"] == 1
    linked = {row["txn_id"] for row in store.con.execute(
        "SELECT txn_id FROM transfer_links")}
    assert linked == {"t1", "t2"}          # both legs, nothing coincidental
    assert r["unmatched_outbound"] == 2    # the tradie + the out-of-window leg


def test_rerun_is_idempotent(store):
    match_transfers(store)
    r2 = match_transfers(store)
    assert r2["pairs"] == 1
    assert store.con.execute(
        "SELECT COUNT(*) FROM transfer_links").fetchone()[0] == 2


def test_reference_numbers_no_longer_split_merchants():
    a = merchant_key(None, "WITHDRAWAL ONLINE 1000001 TFR Joint Music Lessons")
    b = merchant_key(None, "WITHDRAWAL ONLINE 1000002 TFR Joint Music Lessons")
    assert a == b == "tfr joint music lessons"
    assert merchant_key(None, "WITHDRAWAL-OSKO PAYMENT 1234567 Rainy Day Saver") \
        == "rainy day saver"
