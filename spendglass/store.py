"""SQLite store — WAL mode, idempotent upserts, the system of record.

Design rules:
- Amounts are stored twice: the raw decimal STRING exactly as the API sent
  it (never lossy) and integer cents for arithmetic/sorting. SQL does all
  math; no float ever touches money.
- Every row keeps the full raw JSON — the schema extracts what we query,
  the raw column means an API field we didn't anticipate is never lost.
- Upserts are keyed on the provider's own ids, so re-syncing an overlapping
  window is harmless (this is what makes the sync layer simple).
- Consent reality (from the API docs): there is NO consentExpiresAt field.
  Expiry surfaces as connection status 'invalidated'. We therefore track
  status transitions loudly, and separately estimate a worst-case deadline
  as createdAt + 365 days (CDR consents max out at 12 months) — clearly
  labelled an estimate in health output.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS connections (
  id TEXT PRIMARY KEY, provider TEXT, category TEXT,
  institution_id TEXT, institution_name TEXT, status TEXT,
  last_refreshed_at TEXT, created_at TEXT,
  raw TEXT NOT NULL, synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, provider TEXT,
  name TEXT, type TEXT, institution_name TEXT, account_number TEXT,
  currency TEXT, raw TEXT NOT NULL, synced_at TEXT NOT NULL
);

-- Point-in-time snapshots, append-only: balance history is a feature.
CREATE TABLE IF NOT EXISTS balances (
  account_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
  current_balance TEXT, current_balance_cents INTEGER,
  available_balance TEXT, available_balance_cents INTEGER,
  currency TEXT, raw TEXT NOT NULL,
  PRIMARY KEY (account_id, fetched_at)
);

CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, connection_id TEXT,
  account_name TEXT, status TEXT, date TEXT, datetime TEXT,
  post_date TEXT, value_date TEXT, description TEXT,
  amount TEXT, amount_cents INTEGER, direction TEXT,
  category TEXT, custom_category TEXT, custom_category_group TEXT,
  merchant_name TEXT, merchant_category_code TEXT,
  raw TEXT NOT NULL, first_seen_at TEXT NOT NULL, synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_account_date ON transactions (account_id, date);
CREATE INDEX IF NOT EXISTS idx_txn_merchant ON transactions (merchant_name);
CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions (category);

CREATE TABLE IF NOT EXISTS holdings (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_name TEXT,
  symbol TEXT, name TEXT, exchange TEXT, currency TEXT,
  quantity TEXT, average_price TEXT, current_price TEXT,
  market_value TEXT, unrealized_pnl TEXT,
  raw TEXT NOT NULL, synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_name TEXT,
  symbol TEXT, name TEXT, type TEXT, quantity TEXT, price TEXT,
  currency TEXT, total_amount TEXT, total_amount_cents INTEGER,
  fees TEXT, trade_date TEXT, settlement_date TEXT, description TEXT,
  raw TEXT NOT NULL, synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (key TEXT PRIMARY KEY, label TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
  error TEXT, details TEXT
);

CREATE TABLE IF NOT EXISTS account_sync_state (
  account_id TEXT PRIMARY KEY,
  last_synced_to TEXT,       -- date the last successful window reached
  last_success_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_cents(value: str | None) -> int | None:
    """'12.34' -> 1234, '-5' -> -500. None/garbage -> None, never a guess."""
    if value is None:
        return None
    try:
        return int(Decimal(str(value)) * 100)
    except InvalidOperation:
        return None


class Store:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.db_path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self.con.executescript(_SCHEMA)
        row = self.con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.con.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif int(row["value"]) > SCHEMA_VERSION:
            # A DB newer than the code is refused, never mangled.
            raise RuntimeError(
                f"data/store.db is schema v{row['value']} but this code is v{SCHEMA_VERSION} — "
                "upgrade the code, don't downgrade the database"
            )
        self.con.commit()

    # ── upserts (all idempotent on provider ids) ────────────────────────────

    def upsert_connections(self, items: list[dict]) -> int:
        now = _now()
        for c in items:
            self.con.execute(
                """INSERT INTO connections
                   (id, provider, category, institution_id, institution_name,
                    status, last_refreshed_at, created_at, raw, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     provider=excluded.provider, category=excluded.category,
                     institution_id=excluded.institution_id,
                     institution_name=excluded.institution_name,
                     status=excluded.status,
                     last_refreshed_at=excluded.last_refreshed_at,
                     created_at=excluded.created_at,
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (
                    c["id"], c.get("provider"), c.get("category"),
                    c.get("institutionId"), c.get("institutionName"),
                    c.get("status"), c.get("lastRefreshedAt"), c.get("createdAt"),
                    json.dumps(c, sort_keys=True), now,
                ),
            )
        self.con.commit()
        return len(items)

    def upsert_accounts(self, items: list[dict]) -> int:
        now = _now()
        for a in items:
            self.con.execute(
                """INSERT INTO accounts
                   (id, connection_id, provider, name, type, institution_name,
                    account_number, currency, raw, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     connection_id=excluded.connection_id, provider=excluded.provider,
                     name=excluded.name, type=excluded.type,
                     institution_name=excluded.institution_name,
                     account_number=excluded.account_number,
                     currency=excluded.currency,
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (
                    a["id"], a.get("connectionId"), a.get("provider"), a.get("name"),
                    a.get("type"), a.get("institutionName"), a.get("accountNumber"),
                    a.get("currency"), json.dumps(a, sort_keys=True), now,
                ),
            )
        self.con.commit()
        return len(items)

    def insert_balance_snapshots(self, items: list[dict]) -> int:
        now = _now()
        for b in items:
            self.con.execute(
                """INSERT OR REPLACE INTO balances
                   (account_id, fetched_at, current_balance, current_balance_cents,
                    available_balance, available_balance_cents, currency, raw)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    b["accountId"], now,
                    b.get("currentBalance"), to_cents(b.get("currentBalance")),
                    b.get("availableBalance"), to_cents(b.get("availableBalance")),
                    b.get("currency"), json.dumps(b, sort_keys=True),
                ),
            )
        self.con.commit()
        return len(items)

    def upsert_transactions(self, items: list[dict], connection_id: str) -> int:
        now = _now()
        for t in items:
            self.con.execute(
                """INSERT INTO transactions
                   (id, account_id, connection_id, account_name, status, date, datetime,
                    post_date, value_date, description, amount, amount_cents, direction,
                    category, custom_category, custom_category_group, merchant_name,
                    merchant_category_code, raw, first_seen_at, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     account_id=excluded.account_id, connection_id=excluded.connection_id,
                     account_name=excluded.account_name, status=excluded.status,
                     date=excluded.date, datetime=excluded.datetime,
                     post_date=excluded.post_date, value_date=excluded.value_date,
                     description=excluded.description, amount=excluded.amount,
                     amount_cents=excluded.amount_cents, direction=excluded.direction,
                     category=excluded.category, custom_category=excluded.custom_category,
                     custom_category_group=excluded.custom_category_group,
                     merchant_name=excluded.merchant_name,
                     merchant_category_code=excluded.merchant_category_code,
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (
                    t["id"], t.get("accountId"), connection_id, t.get("accountName"),
                    t.get("status"), t.get("date"), t.get("datetime"),
                    t.get("postDate"), t.get("valueDate"), t.get("description"),
                    t.get("amount"), to_cents(t.get("amount")), t.get("direction"),
                    t.get("category"), t.get("customCategory"),
                    t.get("customCategoryGroup"), t.get("merchantName"),
                    t.get("merchantCategoryCode"),
                    json.dumps(t, sort_keys=True), now, now,
                ),
            )
        self.con.commit()
        return len(items)

    def upsert_holdings(self, items: list[dict]) -> int:
        now = _now()
        for h in items:
            self.con.execute(
                """INSERT INTO holdings
                   (id, account_id, account_name, symbol, name, exchange, currency,
                    quantity, average_price, current_price, market_value,
                    unrealized_pnl, raw, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     account_id=excluded.account_id, account_name=excluded.account_name,
                     symbol=excluded.symbol, name=excluded.name,
                     exchange=excluded.exchange, currency=excluded.currency,
                     quantity=excluded.quantity, average_price=excluded.average_price,
                     current_price=excluded.current_price,
                     market_value=excluded.market_value,
                     unrealized_pnl=excluded.unrealized_pnl,
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (
                    h["id"], h.get("accountId"), h.get("accountName"), h.get("symbol"),
                    h.get("name"), h.get("exchange"), h.get("currency"),
                    h.get("quantity"), h.get("averagePrice"), h.get("currentPrice"),
                    h.get("marketValue"), h.get("unrealizedPnl"),
                    json.dumps(h, sort_keys=True), now,
                ),
            )
        self.con.commit()
        return len(items)

    def upsert_trades(self, items: list[dict]) -> int:
        now = _now()
        for t in items:
            self.con.execute(
                """INSERT INTO trades
                   (id, account_id, account_name, symbol, name, type, quantity, price,
                    currency, total_amount, total_amount_cents, fees, trade_date,
                    settlement_date, description, raw, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     account_id=excluded.account_id, account_name=excluded.account_name,
                     symbol=excluded.symbol, name=excluded.name, type=excluded.type,
                     quantity=excluded.quantity, price=excluded.price,
                     currency=excluded.currency, total_amount=excluded.total_amount,
                     total_amount_cents=excluded.total_amount_cents, fees=excluded.fees,
                     trade_date=excluded.trade_date,
                     settlement_date=excluded.settlement_date,
                     description=excluded.description,
                     raw=excluded.raw, synced_at=excluded.synced_at""",
                (
                    t["id"], t.get("accountId"), t.get("accountName"), t.get("symbol"),
                    t.get("name"), t.get("type"), t.get("quantity"), t.get("price"),
                    t.get("currency"), t.get("totalAmount"),
                    to_cents(t.get("totalAmount")), t.get("fees"), t.get("tradeDate"),
                    t.get("settlementDate"), t.get("description"),
                    json.dumps(t, sort_keys=True), now,
                ),
            )
        self.con.commit()
        return len(items)

    def upsert_categories(self, items: list[dict]) -> int:
        for c in items:
            self.con.execute(
                "INSERT OR REPLACE INTO categories (key, label) VALUES (?,?)",
                (c["key"], c.get("label", c["key"])),
            )
        self.con.commit()
        return len(items)

    # ── sync bookkeeping ────────────────────────────────────────────────────

    def start_sync_run(self) -> int:
        cur = self.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (_now(),))
        self.con.commit()
        return int(cur.lastrowid)

    def finish_sync_run(self, run_id: int, status: str, details: dict, error: str | None = None) -> None:
        self.con.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, details=?, error=? WHERE id=?",
            (_now(), status, json.dumps(details, sort_keys=True), error, run_id),
        )
        self.con.commit()

    def mark_account_synced(self, account_id: str, synced_to: str) -> None:
        self.con.execute(
            """INSERT INTO account_sync_state (account_id, last_synced_to, last_success_at)
               VALUES (?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                 last_synced_to=excluded.last_synced_to,
                 last_success_at=excluded.last_success_at""",
            (account_id, synced_to, _now()),
        )
        self.con.commit()

    def get_account_sync_state(self, account_id: str) -> sqlite3.Row | None:
        return self.con.execute(
            "SELECT * FROM account_sync_state WHERE account_id=?", (account_id,)
        ).fetchone()

    # ── health: staleness must be LOUD ──────────────────────────────────────

    def health(self) -> dict:
        counts = {
            table: self.con.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            for table in ("connections", "accounts", "transactions", "balances",
                          "holdings", "trades", "categories")
        }
        last_run = self.con.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        connections = []
        for c in self.con.execute("SELECT * FROM connections").fetchall():
            warnings = []
            if c["status"] != "active":
                warnings.append(
                    f"status is '{c['status']}' — data is NOT updating; reconnect via Redbark"
                )
            deadline = None
            if c["created_at"]:
                try:
                    created = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))
                    deadline = created + timedelta(days=365)
                    days_left = (deadline - datetime.now(timezone.utc)).days
                    # The API has no consentExpiresAt; CDR consents cap at 12
                    # months, so createdAt+365d is the worst-case deadline.
                    if days_left <= 90:
                        warnings.append(
                            f"consent may expire in ~{days_left} days (estimate: "
                            "created + 12 months) — expect renewal prompts from Redbark"
                        )
                except ValueError:
                    pass
            connections.append(
                {
                    "id": c["id"],
                    "institution": c["institution_name"],
                    "category": c["category"],
                    "status": c["status"],
                    "last_refreshed_at": c["last_refreshed_at"],
                    "estimated_consent_deadline": deadline.date().isoformat() if deadline else None,
                    "warnings": warnings,
                }
            )
        return {
            "db": str(self.db_path),
            "counts": counts,
            "last_sync": dict(last_run) if last_run else None,
            "connections": connections,
            "stale": last_run is None or last_run["status"] != "ok",
        }
