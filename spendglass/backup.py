"""Backups — consistent online snapshots of the store, owned by the app.

The store holds two kinds of content: bank rows, re-fetchable only within
the provider's backfill window, and human decisions — merchant identities,
overrides, themes — which no API can re-send. So the server snapshots
data/backups/ at startup and on a timer; scheduling belongs in the app,
not the OS, same as autosync.

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

log = logging.getLogger(__name__)

_PREFIX = "store-"


def backup(db_path: Path, keep: int = 10,
           mirror_dir: Path | None = None) -> Path | None:
    """One consistent snapshot; local rotation + optional best-effort mirror."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
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


def start(db_path: Path, interval_hours: float = 24.0, keep: int = 10,
          mirror_dir: Path | None = None) -> threading.Event:
    """Snapshot now, then on a timer — a long-running instance must never
    sit on a stale restore point. Returns a stop Event; interval <= 0
    disables entirely (the Event is still returned)."""
    stop = threading.Event()
    if interval_hours <= 0:
        return stop

    def _tick() -> None:
        try:
            if changed_since_last_snapshot(db_path):
                backup(db_path, keep, mirror_dir)
        except Exception:
            log.exception("backup failed; will retry next interval")

    def _loop() -> None:
        _tick()
        while not stop.wait(interval_hours * 3600):
            _tick()

    threading.Thread(target=_loop, daemon=True, name="backup-scheduler").start()
    return stop
