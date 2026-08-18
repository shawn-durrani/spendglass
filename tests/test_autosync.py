"""Scheduler due-logic — pure arithmetic, no subprocesses in tests."""

from spendglass.autosync import due
from spendglass.lookup import SETTING_DEFAULTS

NOW = 1_800_000_000.0  # any fixed epoch


def _iso(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def test_never_synced_is_due_immediately():
    assert due(None, 6, NOW)


def test_zero_interval_disables_even_when_never_synced():
    assert not due(None, 0, NOW)
    assert not due(_iso(NOW - 999_999), 0, NOW)


def test_due_exactly_at_interval_boundary():
    assert not due(_iso(NOW - 6 * 3600 + 1), 6, NOW)
    assert due(_iso(NOW - 6 * 3600), 6, NOW)


def test_unparseable_timestamp_counts_as_never():
    assert due("not-a-date", 6, NOW)


def test_interval_setting_has_a_default():
    assert SETTING_DEFAULTS["sync_interval_hours"] == 6


# ---------- the scratch guard and the explicit switch (#1) ----------

def _store_with_rows(tmp_path, name="alt.db", rows=True):
    from pathlib import Path

    from spendglass.store import Store
    db = tmp_path / name
    with Store(db) as s:
        if rows:
            s.con.execute(
                "INSERT INTO transactions (id, account_id, description, raw,"
                " first_seen_at, synced_at) VALUES ('t1', 'a1', 'coffee',"
                " '{}', '2026-01-01T00:00:00+00:00',"
                " '2026-01-01T00:00:00+00:00')")
            s.con.commit()
    return Path(db)


def test_scratch_guard_blocks_an_empty_override_store(tmp_path):
    """The field incident: a scratch instance inheriting the repo .env pulled
    the full real bank history into its throwaway store within a minute of
    boot. Empty + non-default path = inert until a human says otherwise."""
    from spendglass.autosync import suppressed
    empty = _store_with_rows(tmp_path, rows=False)
    default = tmp_path / "data" / "store.db"
    reason = suppressed(empty, "", default_db_path=default)
    assert "scratch guard" in reason and "SPENDGLASS_AUTOSYNC=1" in reason


def test_explicit_optin_and_data_lift_the_guard(tmp_path):
    from spendglass.autosync import suppressed
    default = tmp_path / "data" / "store.db"
    empty = _store_with_rows(tmp_path, rows=False)
    # =1 is the human confirmation the guard asks for
    assert suppressed(empty, "1", default_db_path=default) == ""
    # a store that already holds transactions is no scratch instance
    populated = _store_with_rows(tmp_path, name="real.db")
    assert suppressed(populated, "", default_db_path=default) == ""


def test_fresh_real_install_still_syncs_and_optout_always_wins(tmp_path):
    """An empty store AT the default path is a first boot, not a scratch
    instance - it must keep syncing. And =0 switches everything off,
    default path or not."""
    from spendglass.autosync import suppressed
    default = _store_with_rows(tmp_path, name="data/store.db", rows=False)
    assert suppressed(default, "", default_db_path=default) == ""
    assert "SPENDGLASS_AUTOSYNC=0" in suppressed(default, "0",
                                                 default_db_path=default)


def test_health_reports_why_autosync_is_off(tmp_path):
    """The banner's data: /api/health carries autosync_off so the UI states
    the reason instead of nagging about staleness."""
    from fastapi.testclient import TestClient

    from spendglass.auth import Auth
    from spendglass.ui import create_app
    db = _store_with_rows(tmp_path, name="scratch.db", rows=False)
    auth = Auth(auth_file=tmp_path / "ui_auth.json",
                recovery_secret="s3cret-t3st")
    client = TestClient(create_app(db, auth), base_url="http://127.0.0.1:8903")
    client.post("/api/setup", json={"recovery_secret": "s3cret-t3st",
                                    "password": "correct-horse-battery"})
    h = client.get("/api/health").json()
    assert "scratch guard" in h["autosync_off"]
