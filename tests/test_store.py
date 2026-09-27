"""Store invariants: idempotent upserts, exact money, WAL, refuse-newer-schema."""

import os
import sqlite3
import stat

import pytest

from spendglass.store import SCHEMA_VERSION, Store, to_cents
from tests.conftest import ACCOUNTS, CONNECTIONS, TXNS


def test_wal_mode(store):
    assert store.con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_new_store_is_owner_only(tmp_path):
    """Whoever makes the store first, the server or a first `sync` from the
    command line, it's private from the first byte (issue #71). SQLite
    gives -wal and -shm the database's own mode."""
    old = os.umask(0o022)
    try:
        with Store(tmp_path / "data" / "store.db") as s:
            s.con.execute("INSERT OR REPLACE INTO meta (key, value) "
                          "VALUES ('probe', '1')")
            s.con.commit()
            modes = {p.name: stat.S_IMODE(p.stat().st_mode)
                     for p in (tmp_path / "data").iterdir()}
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "data").stat().st_mode) == 0o700
    assert modes == {"store.db": 0o600, "store.db-wal": 0o600,
                     "store.db-shm": 0o600}


def test_to_cents_exact():
    assert to_cents("12.34") == 1234
    assert to_cents("-4.50") == -450
    assert to_cents("1000.00") == 100000
    assert to_cents("0.1") == 10          # the classic float trap, exactly
    assert to_cents(None) is None
    assert to_cents("not-a-number") is None


def test_transaction_upsert_idempotent(store):
    store.upsert_transactions(TXNS, "conn-bank-1")
    store.upsert_transactions(TXNS, "conn-bank-1")  # overlap window re-sync
    count = store.con.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    assert count == len(TXNS)


def test_pending_settles_to_posted(store):
    """A pending txn later re-synced as posted updates in place — same id."""
    store.upsert_transactions(TXNS, "conn-bank-1")
    settled = {**TXNS[1], "status": "posted", "postDate": "2026-08-02"}
    store.upsert_transactions([settled], "conn-bank-1")
    row = store.con.execute(
        "SELECT status, post_date FROM transactions WHERE id='txn-2'"
    ).fetchone()
    assert row["status"] == "posted"
    assert row["post_date"] == "2026-08-02"


def test_amounts_stored_raw_and_cents(store):
    store.upsert_transactions(TXNS, "conn-bank-1")
    row = store.con.execute(
        "SELECT amount, amount_cents FROM transactions WHERE id='txn-1'"
    ).fetchone()
    assert row["amount"] == "-4.50"       # raw string preserved exactly
    assert row["amount_cents"] == -450    # integer for SQL arithmetic


def test_newer_schema_refused(tmp_path):
    """A DB newer than the code is refused, never mangled."""
    path = tmp_path / "future.db"
    with Store(path) as s:
        s.con.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
        s.con.commit()
    with pytest.raises(RuntimeError, match="upgrade the code"):
        Store(path)


def test_health_flags_dead_connection(store):
    store.upsert_connections(CONNECTIONS)
    store.upsert_accounts(ACCOUNTS)
    health = store.health()
    by_id = {c["id"]: c for c in health["connections"]}
    assert by_id["conn-dead-1"]["warnings"], "invalidated connection must warn"
    assert "NOT updating" in by_id["conn-dead-1"]["warnings"][0]
    assert not any("NOT updating" in w for w in by_id["conn-bank-1"]["warnings"])


def test_health_estimates_consent_deadline(store):
    store.upsert_connections(CONNECTIONS)
    health = store.health()
    by_id = {c["id"]: c for c in health["connections"]}
    # conn-dead-1 was created 2025-04-01 → estimate 2026-04-01, already past.
    assert by_id["conn-dead-1"]["estimated_consent_deadline"] == "2026-04-01"
    # A fresh store with no sync run yet must read as stale — loudly.
    assert health["stale"] is True
