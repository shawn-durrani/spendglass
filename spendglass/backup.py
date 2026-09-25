"""Backups — consistent online snapshots of the store, owned by the app.

The store holds two kinds of content: bank rows, re-fetchable only within
the provider's backfill window, and human decisions — merchant identities,
overrides, themes — which no API can re-send. So the server snapshots
data/backups/ at startup and on a timer; scheduling belongs in the app,
not the OS, same as autosync. The timer goes by the wall clock, so a
snapshot that fell due while the machine slept is taken soon after it
wakes.

sqlite3's backup API takes a consistent copy under WAL with the server
live. Rotation keeps the newest N; a tick where nothing changed is skipped,
so rotation depth is real history rather than identical copies. An optional
mirror folder receives completed snapshots only — never the live WAL DB —
and is best-effort: a failed mirror logs and moves on, the local snapshot
already exists.

Restore is a file copy: stop the server, copy a snapshot over
data/store.db (removing store.db-wal / store.db-shm), start the server.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from . import busy

log = logging.getLogger(__name__)

_PREFIX = "store-"


def backup(db_path: Path, keep: int = 10,
           mirror_dir: Path | None = None) -> Path | None:
    """One consistent snapshot; local rotation + optional best-effort mirror."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    # Busy for the whole snapshot, mirror copy included: a restart mid-copy
    # leaves a partial file in either folder.
    with busy.working("backup"):
        bdir = db_path.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        dest = bdir / time.strftime(f"{_PREFIX}%Y%m%d-%H%M%S.db")
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(dest)
            with dst:
                src.backup(dst)
            dst.close()
        finally:
            src.close()
        _rotate(bdir, keep)
        if mirror_dir:
            try:
                mdir = Path(mirror_dir)
                mdir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, mdir / dest.name)
                _rotate(mdir, keep)
            except OSError:
                log.warning("backup mirror to %s failed; local snapshot is %s",
                            mirror_dir, dest)
        return dest


def _rotate(folder: Path, keep: int) -> None:
    snaps = sorted(folder.glob(f"{_PREFIX}*.db"))
    for old in snaps[:-max(keep, 1)]:
        old.unlink(missing_ok=True)


def last_snapshot(db_path: Path) -> Path | None:
    snaps = sorted((Path(db_path).parent / "backups").glob(f"{_PREFIX}*.db"))
    return snaps[-1] if snaps else None


def changed_since_last_snapshot(db_path: Path) -> bool:
    """True when the DB (or its WAL) was written after the newest snapshot.
    Errs toward backing up: a WAL checkpoint bumps mtime without a content
    change, and that false positive just costs one extra snapshot."""
    db_path = Path(db_path)
    snap = last_snapshot(db_path)
    if snap is None:
        return True
    last = snap.stat().st_mtime
    for p in (db_path, db_path.with_suffix(".db-wal")):
        if p.exists() and p.stat().st_mtime > last:
            return True
    return False


# How often the backup timer wakes to ask whether a snapshot is due. The
# wait itself runs on the monotonic clock, which stops while macOS sleeps,
# so the interval is judged by the wall clock on each tick instead: a
# snapshot that fell due during sleep is taken within one tick of waking.
# A tick costs a directory listing and a few stat calls.
TICK_S = 300.0


def snapshot_due(db_path: Path, now: float, interval_s: float,
                 last_try: float | None) -> bool:
    """True when the newest snapshot, and the timer's last try, are both at
    least `interval_s` old by the wall clock. The last try counts so a
    failing backup waits an interval before it tries again. A time ahead of
    `now` means the clock was set back, and is ignored so backups never
    stall until the clock catches up."""
    snap = last_snapshot(db_path)
    newest = snap.stat().st_mtime if snap else None
    marks = [t for t in (newest, last_try) if t is not None and t <= now]
    return not marks or now - max(marks) >= interval_s


def tick(db_path: Path, now: float, interval_s: float, last_try: float | None,
         keep: int = 10, mirror_dir: Path | None = None) -> float | None:
    """One wake of the backup timer; returns the new last-try time. A due
    snapshot is still skipped when nothing changed since the newest one.
    An `interval_s` of 0 makes any change due, which is the startup
    snapshot."""
    try:
        if not (snapshot_due(db_path, now, interval_s, last_try)
                and changed_since_last_snapshot(db_path)):
            return last_try
        backup(db_path, keep, mirror_dir)
    except Exception:
        log.exception("backup failed; will retry next interval")
    return now


def start(db_path: Path, interval_hours: float = 24.0, keep: int = 10,
          mirror_dir: Path | None = None, *, clock=time.time,
          tick_s: float = TICK_S) -> threading.Event:
    """Snapshot now, then on a timer — a long-running instance must never
    sit on a stale restore point. `clock` is the wall clock, injectable for
    tests. Returns a stop Event; interval <= 0 disables entirely (the Event
    is still returned)."""
    stop = threading.Event()
    interval_s = interval_hours * 3600
    if interval_s <= 0:
        return stop

    def _loop() -> None:
        last_try = tick(db_path, clock(), 0, None, keep, mirror_dir)
        while not stop.wait(min(tick_s, interval_s)):
            last_try = tick(db_path, clock(), interval_s, last_try,
                            keep, mirror_dir)

    threading.Thread(target=_loop, daemon=True, name="backup-scheduler").start()
    return stop
