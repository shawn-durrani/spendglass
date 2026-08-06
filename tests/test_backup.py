"""Backups — consistent snapshots, rotation, change-skips, best-effort
mirror. No timers under test: the scheduler loop is a thin wrapper around
tick logic that is exercised directly."""

import os
import sqlite3
from pathlib import Path

from spendglass import backup
from spendglass.store import Store


def _mkstore(tmp_path) -> Path:
    db = tmp_path / "data" / "store.db"
    db.parent.mkdir(parents=True)
    with Store(db) as s:
        s.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('probe','1')")
        s.con.commit()
    return db


def test_snapshot_is_consistent_and_openable(tmp_path):
    db = _mkstore(tmp_path)
    dest = backup.backup(db)
    assert dest is not None and dest.parent == db.parent / "backups"
    con = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute(
            "SELECT value FROM meta WHERE key='probe'").fetchone()[0] == "1"
    finally:
        con.close()


def test_missing_db_is_a_noop(tmp_path):
    assert backup.backup(tmp_path / "data" / "absent.db") is None


def test_change_detection_skips_quiet_ticks(tmp_path):
    db = _mkstore(tmp_path)
    assert backup.changed_since_last_snapshot(db)      # no snapshot yet
    dest = backup.backup(db)
    # Snapshot is newer than the db → nothing to do.
    os.utime(db, (dest.stat().st_mtime - 60, dest.stat().st_mtime - 60))
    assert not backup.changed_since_last_snapshot(db)
    # A later write (db or WAL) makes the next tick back up again.
    os.utime(db, (dest.stat().st_mtime + 60, dest.stat().st_mtime + 60))
    assert backup.changed_since_last_snapshot(db)


def test_rotation_keeps_newest_n(tmp_path):
    db = _mkstore(tmp_path)
    bdir = db.parent / "backups"
    bdir.mkdir()
    for i in range(5):
        (bdir / f"store-2026010{i}-000000.db").write_bytes(b"old")
    backup.backup(db, keep=3)                          # rotates after writing
    snaps = sorted(p.name for p in bdir.glob("store-*.db"))
    assert len(snaps) == 3
    assert snaps[0] == "store-20260103-000000.db"      # oldest survivors
    assert backup.last_snapshot(db).name == snaps[-1]


def test_mirror_is_best_effort(tmp_path):
    db = _mkstore(tmp_path)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("a file where a folder should be")
    dest = backup.backup(db, mirror_dir=blocked)       # mkdir fails inside
    assert dest is not None and dest.exists()          # local snapshot fine


def test_mirror_receives_copies(tmp_path):
    db = _mkstore(tmp_path)
    mirror = tmp_path / "synced" / "spendglass-backups"
    dest = backup.backup(db, mirror_dir=mirror)
    assert (mirror / dest.name).exists()


def test_disabled_interval_starts_nothing(tmp_path):
    db = _mkstore(tmp_path)
    stop = backup.start(db, interval_hours=0)
    assert not stop.is_set()
    assert backup.last_snapshot(db) is None            # no startup snapshot


def test_health_reports_last_backup(tmp_path):
    """The banner's data: /api/health carries last_backup (epoch ms or
    None) and the display-only interval. create_app starts no scheduler."""
    from fastapi.testclient import TestClient

    from spendglass.auth import Auth
    from spendglass.ui import create_app

    db = _mkstore(tmp_path)
    auth = Auth(auth_file=tmp_path / "ui_auth.json", recovery_secret="s3cret-t3st")
    client = TestClient(create_app(db, auth), base_url="http://127.0.0.1:8903")
    client.post("/api/setup", json={"recovery_secret": "s3cret-t3st",
                                    "password": "correct-horse-battery"})
    h = client.get("/api/health").json()
    assert h["last_backup"] is None and h["backup_interval_hours"] is None

    snap = backup.backup(db)
    h = client.get("/api/health").json()
    assert h["last_backup"] == int(snap.stat().st_mtime * 1000)
