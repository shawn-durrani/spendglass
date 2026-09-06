"""The am-I-busy route: what a restart would cut short.

No timers or subprocesses under test. The scheduler's subprocess call is
faked in place, a backup is probed from inside its own run, and the rows
sync.py would write are written directly.
"""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from spendglass import autosync, backup, busy
from spendglass.auth import Auth
from spendglass.store import Store
from spendglass.ui import create_app

IDLE = {"busy": False, "reasons": []}
SECRET = "s3cret-t3st"
PASSWORD = "correct-horse-battery"


def _mkstore(tmp_path) -> Path:
    db = tmp_path / "data" / "store.db"
    with Store(db) as s:
        s.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('probe','1')")
        s.con.commit()
    return db


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


@pytest.fixture()
def probe(tmp_path):
    """An anonymous client against a fresh store: no setup, no login."""
    db = _mkstore(tmp_path)
    auth = Auth(auth_file=tmp_path / "ui_auth.json", recovery_secret=SECRET)
    return TestClient(create_app(db, auth), base_url="http://127.0.0.1:8903"), db


def test_fresh_store_is_idle_and_needs_no_session(probe):
    client, _ = probe
    r = client.get("/api/busy")
    assert r.status_code == 200 and r.json() == IDLE
    assert client.get("/api/health").status_code == 401   # the gate still holds


def test_open_sync_run_is_busy_until_it_finishes(probe):
    client, db = probe
    with Store(db) as s:
        run_id = s.start_sync_run()
    assert client.get("/api/busy").json() == {"busy": True, "reasons": ["sync"]}
    with Store(db) as s:
        s.finish_sync_run(run_id, "ok", {})
    assert client.get("/api/busy").json() == IDLE


def test_a_run_left_open_by_a_crash_stops_counting_after_an_hour(probe):
    client, db = probe
    with Store(db) as s:
        s.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (_ago(hours=2),))
        s.con.commit()
    assert client.get("/api/busy").json() == IDLE
    with Store(db) as s:
        s.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (_ago(minutes=50),))
        s.con.commit()
    assert client.get("/api/busy").json()["reasons"] == ["sync"]


def test_backup_in_progress_is_busy(probe, monkeypatch):
    """Probed from inside backup(): rotation runs after the copy and still
    inside the busy window."""
    client, db = probe
    seen, real_rotate = [], backup._rotate

    def rotate(folder, keep):
        seen.append(client.get("/api/busy").json())
        real_rotate(folder, keep)

    monkeypatch.setattr(backup, "_rotate", rotate)
    assert backup.backup(db) is not None
    assert seen == [{"busy": True, "reasons": ["backup"]}]
    assert client.get("/api/busy").json() == IDLE


def test_scheduled_sync_is_busy_through_both_steps(probe, monkeypatch):
    """The scheduler's sync-then-enrich pair is one busy window, so the
    enrich step, which records no sync_runs row, is covered too."""
    client, _ = probe
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd[-1], client.get("/api/busy").json()["reasons"]))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(autosync.subprocess, "run", fake_run)
    autosync.run_subprocesses()
    assert seen == [("spendglass.sync", ["sync"]), ("spendglass.enrich", ["sync"])]
    assert client.get("/api/busy").json() == IDLE


def test_run_now_miner_is_busy_under_its_own_name(probe, monkeypatch):
    """A Run-now miner writes the store from a thread in this process."""
    from spendglass import enrich as enrich_mod
    client, _ = probe
    client.post("/api/setup", json={"recovery_secret": SECRET, "password": PASSWORD})
    seen = []
    monkeypatch.setattr(enrich_mod, "rebuild_merchants",
                        lambda store: seen.append(busy.active()) or 0)
    assert client.post("/api/admin/run", json={"miner": "enrich"}).status_code == 200
    for _ in range(500):                      # the thread is quick; bound the wait
        if seen and not busy.active():
            break
        time.sleep(0.01)
    assert seen == [["enrich"]]
    assert client.get("/api/busy").json() == IDLE


def test_propagation_after_an_approval_is_busy(tmp_path):
    """The follow-up pass an approval starts writes proposals from a thread
    in this process, so it counts too."""
    from spendglass.lookup import ensure_schema
    db = _mkstore(tmp_path)
    with Store(db) as s:
        ensure_schema(s)
        s.con.execute(
            """INSERT INTO merchant_lookups (merchant_key, proposed_name,
               proposed_subcategory, confidence, status)
               VALUES ('cafe x', 'Cafe X', 'Cafe', 0.5, 'pending')""")
        s.con.commit()
    seen = []
    auth = Auth(auth_file=tmp_path / "ui_auth.json", recovery_secret=SECRET)
    app = create_app(db, auth, propagator=lambda confirmed: seen.append(busy.active()))
    client = TestClient(app, base_url="http://127.0.0.1:8903")
    client.post("/api/setup", json={"recovery_secret": SECRET, "password": PASSWORD})
    r = client.post("/api/lookups/decide", json={"decisions": [
        {"merchant_key": "cafe x", "action": "approve"}]})
    assert r.status_code == 200
    for _ in range(500):
        if seen and not busy.active():
            break
        time.sleep(0.01)
    assert seen == [["propagate"]]
    assert client.get("/api/busy").json() == IDLE


def test_reasons_are_a_fixed_ordered_set_named_once(probe):
    client, db = probe
    with Store(db) as s:
        s.start_sync_run()                     # the store says sync as well
    with busy.working("backup"), busy.working("sync"), busy.working("lookup"):
        assert client.get("/api/busy").json()["reasons"] == ["sync", "lookup", "backup"]
    assert client.get("/api/busy").json()["reasons"] == ["sync"]


def test_overlapping_work_of_one_kind_stays_busy_until_both_end():
    with busy.working("enrich"):
        with busy.working("enrich"):
            pass
        assert busy.active() == ["enrich"]
    assert busy.active() == []


def test_only_known_labels_can_reach_the_wire():
    with pytest.raises(ValueError):
        with busy.working("a merchant name"):
            pass
    assert busy.active() == []
