"""Backups — consistent snapshots, rotation, change-skips, best-effort
mirror. No real timers under test: the scheduler loop is a thin wrapper
around tick logic that is exercised directly, and the loop itself only
runs on an injected clock."""

import os
import sqlite3
import threading
import time
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


# ── the timer goes by the wall clock (#66) ──────────────────────────────────
# The wait between ticks is monotonic and stops while macOS sleeps, so each
# tick judges the interval by the wall clock. Ticks run with explicit times,
# and the loop runs on an injected clock.

HOUR = 3600.0
DAY = 24 * HOUR


def _set_mtime(path: Path, t: float) -> None:
    os.utime(path, (t, t))


def _set_db_mtime(db: Path, t: float) -> None:
    for p in (db, db.with_suffix(".db-wal")):
        if p.exists():
            _set_mtime(p, t)


def _standing_snapshot(db: Path, taken_at: float) -> Path:
    """A restore point taken at `taken_at`, renamed to an older stamp so a
    fresh snapshot in the same second can't land on its file name."""
    snap = backup.backup(db)
    old = snap.with_name("store-20200101-000000.db")
    snap.rename(old)
    _set_mtime(old, taken_at)
    return old


def _snaps(db: Path) -> list[Path]:
    return sorted((db.parent / "backups").glob("store-*.db"))


def test_an_overdue_snapshot_is_taken_on_the_first_tick_after_sleep(tmp_path):
    """The Mac sleeps for three days: the wall clock jumps and awake time
    barely moves. The first tick after waking takes the overdue snapshot."""
    db = _mkstore(tmp_path)
    t0 = time.time()
    _standing_snapshot(db, t0 - HOUR)
    _set_db_mtime(db, t0)                              # the store changed after it
    last = backup.tick(db, t0 + 300, DAY, t0)
    assert len(_snaps(db)) == 1                        # five minutes on: not due
    last = backup.tick(db, t0 + 3 * DAY, DAY, last)
    assert len(_snaps(db)) == 2
    assert last == t0 + 3 * DAY


def test_a_snapshot_that_is_not_overdue_is_not_taken(tmp_path):
    db = _mkstore(tmp_path)
    t0 = time.time()
    _standing_snapshot(db, t0 - HOUR)
    _set_db_mtime(db, t0)
    assert backup.tick(db, t0 + 23 * HOUR - 1, DAY, None) is None
    assert len(_snaps(db)) == 1


def test_an_unchanged_store_takes_no_snapshot(tmp_path):
    db = _mkstore(tmp_path)
    t0 = time.time()
    _standing_snapshot(db, t0 - 2 * DAY)
    _set_db_mtime(db, t0 - 3 * DAY)                    # last written before it
    assert backup.tick(db, t0, DAY, None) is None
    assert len(_snaps(db)) == 1


def test_a_failed_try_waits_a_full_interval(tmp_path, monkeypatch):
    db = _mkstore(tmp_path)
    t0 = time.time()
    _standing_snapshot(db, t0 - 2 * DAY)
    _set_db_mtime(db, t0)
    calls = []

    def failing(*a):
        calls.append(a)
        raise OSError("disk full")

    monkeypatch.setattr(backup, "backup", failing)
    assert backup.tick(db, t0, DAY, None) == t0
    assert backup.tick(db, t0 + 300, DAY, t0) == t0
    assert len(calls) == 1


def test_times_ahead_of_the_clock_are_ignored(tmp_path):
    """A clock set back must not stall backups until it catches up."""
    db = _mkstore(tmp_path)
    t0 = time.time()
    _standing_snapshot(db, t0 + 30 * DAY)
    assert backup.snapshot_due(db, t0, DAY, t0 + 30 * DAY)
    assert not backup.snapshot_due(db, t0, DAY, t0 - HOUR)


def test_the_scheduler_runs_on_the_wall_clock(tmp_path, monkeypatch):
    """The loop waits on the monotonic clock, so a jump in the injected wall
    clock alone must be enough for the next tick to snapshot. The clock
    steps the loop one reading at a time."""
    db = _mkstore(tmp_path)
    t0 = time.time()
    _standing_snapshot(db, t0 - HOUR)
    _set_db_mtime(db, t0 - 2 * HOUR)                   # quiet at startup
    wall = [t0]
    asking, answer, took = (threading.Semaphore(0), threading.Semaphore(0),
                            threading.Event())

    def clock():
        asking.release()
        answer.acquire(timeout=5)
        return wall[0]

    def next_reading():
        """Wait until the loop asks the time, so its last step has finished."""
        assert asking.acquire(timeout=5)

    monkeypatch.setattr(backup, "backup", lambda *a: took.set())
    before = threading.enumerate()
    stop = backup.start(db, interval_hours=24, clock=clock, tick_s=0.01)
    try:
        next_reading()
        answer.release()                               # startup: nothing changed
        next_reading()
        _set_db_mtime(db, t0 - 60)                     # changed, but not due
        answer.release()                               # a tick an hour on
        next_reading()
        assert not took.is_set()
        wall[0] = t0 + 3 * DAY                         # asleep for three days
        answer.release()
        assert took.wait(5)
    finally:
        stop.set()
        answer.release()
    _scheduler_thread(before).join(5)


def test_the_startup_snapshot_still_ignores_the_interval(tmp_path):
    db = _mkstore(tmp_path)
    t0 = time.time()
    _standing_snapshot(db, t0 - 60)                    # a minute old
    _set_db_mtime(db, t0)
    assert backup.tick(db, t0, 0, None) == t0
    assert len(_snaps(db)) == 2


def test_stop_event_halts_the_loop_promptly(tmp_path):
    db = _mkstore(tmp_path)
    before = threading.enumerate()
    stop = backup.start(db, interval_hours=24)         # the five-minute tick
    thread = _scheduler_thread(before)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()


def _scheduler_thread(before):
    (thread,) = [t for t in threading.enumerate()
                 if t.name == "backup-scheduler" and t not in before]
    return thread
