"""Am-I-busy: the in-flight work a restart would cut short.

The fleet's deploy watcher asks `GET /api/busy` before it restarts the
service and waits while the answer is true. Work inside the server process
registers here under one of a closed set of labels, so the route can name
the kind of work and never its content. A bank sync runs as its own
process (sync.py) and is read from the store instead.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The closed set, in the order the route reports them. A new kind of work
# needs its label here first: `working` refuses anything else, so a stray
# string can never reach the wire.
LABELS = ("sync", "enrich", "lookup", "classify", "sweep", "propagate", "backup")

# A sync_runs row left open past this is a crash, not a run. The scheduler
# kills its sync subprocess at 30 minutes; a hand-run backfill that outlives
# an hour stops counting, which is the trade for never holding every deploy.
STALE_RUN_SECONDS = 3600

_lock = threading.Lock()
_counts: Counter[str] = Counter()


@contextmanager
def working(label: str) -> Iterator[None]:
    """Count `label` as in flight for the block's duration. A count, not a
    flag, so two overlapping jobs of one kind stay busy until both end."""
    if label not in LABELS:
        raise ValueError(f"unknown busy label {label!r}; add it to busy.LABELS")
    with _lock:
        _counts[label] += 1
    try:
        yield
    finally:
        with _lock:
            _counts[label] -= 1


def active() -> list[str]:
    """Labels with work in flight in this process, in LABELS order."""
    with _lock:
        return [label for label in LABELS if _counts[label] > 0]


def sync_run_open(db_path: Path, max_age_seconds: int = STALE_RUN_SECONDS) -> bool:
    """Is sync.py writing the store right now? It runs as its own process,
    so its open sync_runs row is the only sign of it from here. A row older
    than max_age_seconds does not count (see STALE_RUN_SECONDS).

    Read-only, and the store is WAL, so a writer never makes this wait. A
    store that cannot be opened, or has no sync_runs table yet, reads as
    idle: nothing observable is running in it."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=max_age_seconds)).isoformat(timespec="seconds")
    try:
        con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return False
    try:
        row = con.execute(
            "SELECT 1 FROM sync_runs WHERE finished_at IS NULL AND started_at > ? "
            "LIMIT 1", (cutoff,)).fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return row is not None


def status(db_path: Path) -> dict:
    """The route's answer: {"busy": bool, "reasons": [labels]}, labels in
    LABELS order and each named once."""
    reasons = set(active())
    if sync_run_open(db_path):
        reasons.add("sync")
    ordered = [label for label in LABELS if label in reasons]
    return {"busy": bool(ordered), "reasons": ordered}
