"""Configuration — .env + environment, no third-party deps.

Environment always wins over .env, so tests and one-off overrides work
without editing files. The .env parser is deliberately minimal: KEY=VALUE
lines, # comments, optional single/double quotes. Anything fancier belongs
in a real config system we don't need yet.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "store.db"

# The owner's other apps, each at the health route it answers on loopback
# without a session. The ports are the fleet's allocation.
DEFAULT_SIBLING_APPS = {
    "crossband": "http://127.0.0.1:8902/api/auth/session",
    "membro": "http://127.0.0.1:8901/v1/health",
    "threadfold": "http://127.0.0.1:8904/health",
}


def _sibling_apps(raw: str | None) -> dict:
    """SPENDGLASS_SIBLING_APPS is a JSON object of name -> health URL. Unset
    or unparseable keeps the defaults; `{}` turns the header row off."""
    if not raw:
        return dict(DEFAULT_SIBLING_APPS)
    try:
        value = json.loads(raw)
    except ValueError:
        return dict(DEFAULT_SIBLING_APPS)
    return value if isinstance(value, dict) else dict(DEFAULT_SIBLING_APPS)


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass
class Config:
    api_key: str | None
    api_url: str = "https://api.redbark.com"
    db_path: Path = field(default_factory=lambda: REPO_ROOT / "data" / "store.db")
    backfill_days: int = 365
    # Trades queries are capped at 366-day windows by the API; the sync layer
    # chunks anything larger, but keep the constant where both sides can see it.
    trades_max_window_days: int = 366
    backup_interval_hours: float = 24.0
    # SPENDGLASS_AUTOSYNC: "0" opts out of scheduled sync entirely; "1"
    # overrides the scratch guard (see autosync.suppressed); "" = default.
    autosync: str = ""
    backup_keep: int = 10
    backup_mirror_dir: Path | None = None
    # The header links these apps, each only when it answers its health
    # route on loopback (app_links.py).
    sibling_apps: dict = field(default_factory=lambda: dict(DEFAULT_SIBLING_APPS))

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        file_vars = _parse_env_file(env_file or REPO_ROOT / ".env")

        def get(name: str, default: str | None = None) -> str | None:
            return os.environ.get(name) or file_vars.get(name) or default

        mirror = get("SPENDGLASS_BACKUP_MIRROR_DIR")
        return cls(
            api_key=get("REDBARK_API_KEY"),
            api_url=(get("REDBARK_API_URL", "https://api.redbark.com") or "").rstrip("/"),
            db_path=Path(get("SPENDGLASS_DB") or DEFAULT_DB_PATH),
            autosync=(get("SPENDGLASS_AUTOSYNC") or "").strip(),
            backfill_days=int(get("SPENDGLASS_BACKFILL_DAYS", "365") or 365),
            backup_interval_hours=float(get("SPENDGLASS_BACKUP_INTERVAL_HOURS", "24") or 24),
            backup_keep=int(get("SPENDGLASS_BACKUP_KEEP", "10") or 10),
            backup_mirror_dir=Path(mirror) if mirror else None,
            sibling_apps=_sibling_apps(get("SPENDGLASS_SIBLING_APPS")),
        )
