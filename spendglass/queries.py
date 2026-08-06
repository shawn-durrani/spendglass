"""Read-only queries shared by the MCP server (and testable without it).

Two properties the MCP layer leans on, each with its real limit stated:

1. READ-ONLY, BY THE CODE FIRST: nothing in this module writes, and
   open_readonly() hands out connections with SQLite's mode=ro, so a write
   through one of those raises OperationalError no matter what a caller
   (or a confused model) asks for. That is not a guarantee about the
   process: freshness() below opens the store through Store, which is
   read-write and runs migrations on connect. Treat mode=ro as a backstop
   on the query path, not as a lock on the file.
2. FRESHNESS IS LOUD: freshness() returns `as_of` (when the store last
   synced successfully) plus `stale` and `staleness_warnings`, and the
   server merges it into every tool response bar one. A consumer that
   ignores them was at least told. The exception is store_health, which
   returns the store's health record as-is: its own `stale` flag, but no
   `as_of` and no `staleness_warnings`.

All aggregation is SQL over integer cents, so the model reads answers, it
never does arithmetic.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

STALE_AFTER_HOURS = 26  # a daily sync plus slack; beyond this, shout


def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def freshness(db_path: Path | str) -> dict:
    """The envelope the server merges into every tool response bar
    store_health."""
    # Store opens read-write and migrates on connect; this call only reads.
    with Store(db_path) as s:
        h = s.health()
    warnings: list[str] = []
    as_of = None
    if h["last_sync"] and h["last_sync"]["status"] == "ok":
        as_of = h["last_sync"]["finished_at"]
        age_h = (
            datetime.now(timezone.utc) - datetime.fromisoformat(as_of)
        ).total_seconds() / 3600
        if age_h > STALE_AFTER_HOURS:
            warnings.append(
                f"data is {age_h:.0f} hours old — run "
                "`.venv/bin/python -m spendglass.sync` before trusting totals"
            )
    else:
        warnings.append("no successful sync has ever completed — the store is empty or broken")
    for c in h["connections"]:
        warnings.extend(f"{c['institution']}: {w}" for w in c["warnings"])
    return {"as_of": as_of, "stale": bool(warnings), "staleness_warnings": warnings}


def list_accounts(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        """SELECT a.id, a.name, a.type, a.institution_name, a.currency,
                  c.status AS connection_status, c.category AS connection_category,
                  b.current_balance, b.available_balance, b.fetched_at AS balance_as_of
           FROM accounts a
           LEFT JOIN connections c ON c.id = a.connection_id
           LEFT JOIN (
             SELECT account_id, current_balance, available_balance,
                    MAX(fetched_at) AS fetched_at
             FROM balances GROUP BY account_id
           ) b ON b.account_id = a.id
           ORDER BY a.name"""
    ).fetchall()
    return [dict(r) for r in rows]


def search_transactions(
    con: sqlite3.Connection,
    q: str | None = None,
    account_id: str | None = None,
    merchant: str | None = None,
    category: str | None = None,
    direction: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    limit: int = 50,
) -> dict:
    limit = max(1, min(int(limit), 500))
    where, params = ["1=1"], []
    if q:
        where.append("(t.description LIKE ? OR t.merchant_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if account_id:
        where.append("t.account_id = ?"); params.append(account_id)
    if merchant:
        where.append("t.merchant_name LIKE ?"); params.append(f"%{merchant}%")
    if category:
        where.append("t.category = ?"); params.append(category)
    if direction:
        where.append("t.direction = ?"); params.append(direction)
    if status:
        where.append("t.status = ?"); params.append(status)
    if date_from:
        where.append("t.date >= ?"); params.append(date_from)
    if date_to:
        where.append("t.date <= ?"); params.append(date_to)
    if amount_min is not None:
        where.append("t.amount_cents >= ?"); params.append(int(amount_min * 100))
    if amount_max is not None:
        where.append("t.amount_cents <= ?"); params.append(int(amount_max * 100))
    cond = " AND ".join(where)

    totals = con.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s FROM transactions t WHERE {cond}",
        params,
    ).fetchone()
    rows = con.execute(
        f"""SELECT t.id, t.date, t.description, t.merchant_name, t.amount,
                   t.direction, t.category, t.custom_category, t.status,
                   a.name AS account_name
            FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
            WHERE {cond} ORDER BY t.date DESC, t.id LIMIT ?""",
        [*params, limit],
    ).fetchall()
    return {
        "matched": totals["n"],
        "returned": len(rows),
        "net_amount": f"{totals['s'] / 100:.2f}",
        "transactions": [dict(r) for r in rows],
    }


def spending_summary(
    con: sqlite3.Connection,
    group_by: str = "category",
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str = "debit",
    top: int = 25,
) -> dict:
    groups = {
        "category": "COALESCE(t.category, '(uncategorised)')",
        "merchant": "COALESCE(t.merchant_name, '(no merchant)')",
        "month": "substr(t.date, 1, 7)",
        "account": "COALESCE(a.name, t.account_id)",
    }
    if group_by not in groups:
        raise ValueError(f"group_by must be one of {sorted(groups)}")
    if direction not in ("debit", "credit", "both"):
        raise ValueError("direction must be debit, credit, or both")
    top = max(1, min(int(top), 200))

    where, params = ["1=1"], []
    if direction != "both":
        where.append("t.direction = ?"); params.append(direction)
    if date_from:
        where.append("t.date >= ?"); params.append(date_from)
    if date_to:
        where.append("t.date <= ?"); params.append(date_to)
    cond = " AND ".join(where)
    key = groups[group_by]

    rows = con.execute(
        f"""SELECT {key} AS grp, COUNT(*) n,
                   COALESCE(SUM(t.amount_cents),0) sum_cents
            FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
            WHERE {cond}
            GROUP BY {key}
            ORDER BY ABS(SUM(t.amount_cents)) DESC
            LIMIT ?""",
        [*params, top],
    ).fetchall()
    total = con.execute(
        f"SELECT COALESCE(SUM(t.amount_cents),0) s, COUNT(*) n FROM transactions t "
        f"LEFT JOIN accounts a ON a.id = t.account_id WHERE {cond}",
        params,
    ).fetchone()
    return {
        "group_by": group_by,
        "direction": direction,
        "total_amount": f"{total['s'] / 100:.2f}",
        "total_transactions": total["n"],
        "groups": [
            {"group": r["grp"], "transactions": r["n"], "amount": f"{r['sum_cents'] / 100:.2f}"}
            for r in rows
        ],
    }


def list_recurring_charges(con: sqlite3.Connection) -> dict:
    """Detected recurring charges — derived by enrich.py's interval math."""
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recurring_charges'"
    ).fetchone()
    if not exists:
        return {"recurring": [], "note": "enrichment has never run — "
                "`.venv/bin/python -m spendglass.enrich`"}
    rows = con.execute(
        """SELECT display_name, cadence, occurrences, typical_amount,
                  amount_min, amount_max, first_seen, last_seen, next_expected
           FROM recurring_charges
           ORDER BY CAST(typical_amount AS REAL)"""
    ).fetchall()
    return {"recurring": [dict(r) for r in rows]}


def spending_deltas(con: sqlite3.Connection, months: int = 6) -> dict:
    """Month-over-month debit changes by category. Sums happen in SQL over
    integer cents; the only python arithmetic is subtracting two exact ints."""
    months = max(2, min(int(months), 24))
    rows = con.execute(
        """SELECT substr(date,1,7) AS month,
                  COALESCE(category,'(uncategorised)') AS category,
                  SUM(amount_cents) AS cents, COUNT(*) AS n
           FROM transactions
           WHERE direction='debit' AND date >= date('now', ?)
           GROUP BY month, category ORDER BY month""",
        (f"-{months + 1} months",),
    ).fetchall()
    by_month: dict[str, dict] = {}
    for r in rows:
        by_month.setdefault(r["month"], {"total_cents": 0, "categories": {}})
        by_month[r["month"]]["total_cents"] += r["cents"]
        by_month[r["month"]]["categories"][r["category"]] = r["cents"]
    ordered = sorted(by_month)[-months:]
    out = []
    for i, month in enumerate(ordered):
        cur = by_month[month]
        entry = {"month": month, "total_spend": f"{-cur['total_cents'] / 100:.2f}"}
        if i > 0:
            prev = by_month[ordered[i - 1]]
            entry["change_vs_previous"] = f"{(prev['total_cents'] - cur['total_cents']) / 100:.2f}"
            moves = []
            for cat in set(cur["categories"]) | set(prev["categories"]):
                delta = prev["categories"].get(cat, 0) - cur["categories"].get(cat, 0)
                if delta:
                    moves.append((cat, delta))
            moves.sort(key=lambda x: abs(x[1]), reverse=True)
            entry["biggest_category_moves"] = [
                {"category": c, "change": f"{d / 100:.2f}"} for c, d in moves[:5]
            ]
        out.append(entry)
    return {"months": out,
            "note": "positive change = spent more than the previous month"}
