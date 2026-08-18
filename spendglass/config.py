"""Configuration — .env + environment, no third-party deps.

Environment always wins over .env, so tests and one-off overrides work
without editing files. The .env parser is deliberately minimal: KEY=VALUE
lines, # comments, optional single/double quotes. Anything fancier belongs
in a real config system we don't need yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "store.db"


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
        )
