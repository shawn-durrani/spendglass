"""Scheduled freshness — the server keeps the store current, portably.

Scheduling lives in the app, not the OS: a launchd plist or systemd timer
would silently not exist for the next person who clones this. The UI server
is already the long-running process, so a background thread checks once a
minute whether the last successful sync is older than the configured
interval and, when due, runs `python -m spendglass.sync` then
`python -m spendglass.enrich` as SUBPROCESSES, which is what preserves the
privilege split: sync.py reads `.env` itself and is the only code that
calls the bank, so no bank API call is ever made from the server process.
The honest limit is that the server's own Config.load() parses the whole of
`.env` at startup, so the key does sit in its memory; what the split buys is
that nothing there uses it.

Status is written to the same `miner.status.sync` slot the admin panel
reads, so scheduled and manual runs share one display. Interval is the
`sync_interval_hours` miner setting (0 disables). Failures back off to
15 minutes rather than hammering the bank.

Headless installs (no UI running) can use an OS scheduler on the same
commands — see README.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import DEFAULT_DB_PATH, REPO_ROOT
from .store import Store

CHECK_SECONDS = 60
FAIL_BACKOFF_SECONDS = 15 * 60
SUBPROCESS_TIMEOUT = 30 * 60

# One sync at a time, whether the scheduler or an admin click asks for it.
_run_lock = threading.Lock()


def _store_has_transactions(db_path: Path) -> bool:
    try:
        with Store(db_path) as s:
            return bool(s.con.execute(
                "SELECT EXISTS(SELECT 1 FROM transactions)").fetchone()[0])
    except Exception:
        return False  # unreadable reads as empty: the guard stays cautious


def suppressed(db_path: Path, autosync_env: str,
               default_db_path: Path = DEFAULT_DB_PATH) -> str:
    """Why scheduled sync must not run, or '' to run normally (#1).

    Two shapes, both from the field:
    - SPENDGLASS_AUTOSYNC=0: the explicit opt-out for demos and offline
      development.
    - The scratch guard: the store is EMPTY and SPENDGLASS_DB points away
      from the default location. A scratch instance that inherited the repo
      .env pulled the full real bank history into its throwaway store
      within a minute of boot; an empty override store is almost always a
      test instance, so filling it with real money data needs a human's
      say-so first - SPENDGLASS_AUTOSYNC=1, or the admin Run now button
      (which is one). A fresh REAL install (empty store at the default
      path) still syncs on first boot, exactly as before.
    """
    v = (autosync_env or "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return "switched off by SPENDGLASS_AUTOSYNC=0"
    if v in ("1", "true", "yes", "on"):
        return ""
    if Path(db_path).resolve() != Path(default_db_path).resolve()             and not _store_has_transactions(db_path):
        return ("scratch guard: this store is empty and SPENDGLASS_DB points "
                "away from the default, so scheduled sync will not pull real "
                "bank data into it; set SPENDGLASS_AUTOSYNC=1 to allow, or "
                "use the admin Run now button")
    return ""


def due(last_ok_iso: str | None, interval_hours: float, now_ts: float) -> bool:
    """Is a sync due? Never-synced stores are due immediately; 0 disables."""
    if interval_hours <= 0:
        return False
    if not last_ok_iso:
        return True
    try:
        last = datetime.fromisoformat(last_ok_iso).timestamp()
    except ValueError:
        return True
    return now_ts - last >= interval_hours * 3600


def last_ok_sync(store: Store) -> str | None:
    try:
        row = store.con.execute(
            "SELECT MAX(finished_at) f FROM sync_runs WHERE status='ok'"
        ).fetchone()
        return row["f"]
    except Exception:
        return None


def run_subprocesses(repo_root: Path = REPO_ROOT) -> dict:
    """Bank sync, then deterministic enrichment, each in its own process.
    Returns a summary dict; raises RuntimeError with the output tail on
    failure so callers surface a real reason, not a shrug."""
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("a sync is already running")
    try:
        result = {}
        for step, mod in (("sync", "spendglass.sync"),
                          ("enrich", "spendglass.enrich")):
            p = subprocess.run(
                [sys.executable, "-m", mod], cwd=repo_root,
                capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
            tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-4:])
            if p.returncode != 0:
                raise RuntimeError(f"{step} failed: {tail[:260]}")
            result[step] = tail.splitlines()[0][:200] if tail else "ok"
        return result
    finally:
        _run_lock.release()


def _set_status(db_path: Path, payload: dict) -> None:
    with Store(db_path) as s:
        s.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                      ("miner.status.sync", json.dumps(payload)))
        s.con.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def run_once(db_path: Path) -> None:
    """One sync+enrich with status bookkeeping — used by the scheduler loop
    and by the admin run-now button."""
    started = _now_iso()
    _set_status(db_path, {"state": "running", "started_at": started})
    try:
        result = run_subprocesses()
        _set_status(db_path, {"state": "done", "started_at": started,
                              "result": result, "finished_at": _now_iso()})
    except Exception as e:
        _set_status(db_path, {"state": "error", "started_at": started,
                              "error": str(e)[:300]})
        raise


def start(db_path: Path, autosync_env: str = "") -> threading.Thread:
    """Start the scheduler thread (daemon — dies with the server)."""

    def loop() -> None:
        last_attempt = 0.0
        off_reason_written = None
        while True:
            try:
                # Re-checked every tick (#1): the scratch guard lifts by
                # itself the moment the store holds data (a human ran sync),
                # with no restart needed.
                reason = suppressed(db_path, autosync_env)
                if reason:
                    if reason != off_reason_written:
                        _set_status(db_path, {"state": "off",
                                              "reason": reason})
                        off_reason_written = reason
                    time.sleep(CHECK_SECONDS)
                    continue
                off_reason_written = None
                with Store(db_path) as s:
                    from .lookup import get_settings
                    interval = float(get_settings(s)["sync_interval_hours"])
                    last_ok = last_ok_sync(s)
                now = time.time()
                min_gap = min(interval * 3600, FAIL_BACKOFF_SECONDS) or \
                    FAIL_BACKOFF_SECONDS
                if due(last_ok, interval, now) and now - last_attempt >= min_gap:
                    last_attempt = now
                    try:
                        run_once(db_path)
                    except Exception:
                        pass  # status already records the reason
            except Exception:
                pass  # a broken store read must never kill the scheduler
            time.sleep(CHECK_SECONDS)

    t = threading.Thread(target=loop, name="autosync", daemon=True)
    t.start()
    return t
