"""Trend analytics — deterministic SQL, no judgement.

Eight devices, all computed from the store; the UI renders them and the MCP
server serves them to whatever model is looking. Every one compares the user
against their OWN trailing history — never a budget (self-history baselines
change behaviour; budget displays can backfire). Amounts are integer cents
summed in SQL; callers report figures as given.

All functions take a sqlite3 connection with row_factory=Row (read-only is
fine — nothing here writes).
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

# Effective category: user override > identity-informed model > bank.
ECAT = ("COALESCE(ml.resolved_category, m.llm_category, m.cdr_category, "
        "t.category)")
JOIN = """FROM transactions t
    LEFT JOIN merchants m ON m.key = t.merchant_key
    LEFT JOIN merchant_lookups ml ON ml.merchant_key = t.merchant_key
      AND ml.status IN ('auto','approved','overridden')
    LEFT JOIN transfer_links tl ON tl.txn_id = t.id AND tl.kind='internal'"""
# Matched internal moves are never spend; unmatched transfer-category rows
# stay excluded from CONSUMPTION until someone recategorises them (a paid
# tradie becomes Home Improvement; a family contribution stays a transfer).
# Loan payments split at the subcategory: INTEREST is money gone forever and
# counts as spend; principal, car-finance instalments, and drawdowns move the
# user's own balance sheet and stay out of consumption (visible via the
# include-transfers toggle and the committed floor, never hidden — just not
# double-counted against the things the borrowed money bought).
NON_SPEND = "('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS')"
LOAN_SPEND_SUBS = "('Mortgage Interest','Card Interest','Loan Fees')"
SPEND = (f"t.direction='debit' AND t.date IS NOT NULL "
         f"AND tl.txn_id IS NULL "
         f"AND ({ECAT} NOT IN {NON_SPEND} "
         f"     OR ({ECAT} = 'LOAN_PAYMENTS' "
         f"         AND ml.resolved_subcategory IN {LOAN_SPEND_SUBS}))")
DISPLAY = ("COALESCE(ml.resolved_name, m.display_name, t.merchant_name, "
           "t.description)")

CADENCE_PER_YEAR = {"weekly": 52, "fortnightly": 26, "monthly": 12,
                    "quarterly": 4, "yearly": 1}


def _month(d: date) -> str:
    return d.strftime("%Y-%m")


def _complete_months(con: sqlite3.Connection, n: int,
                     before: str | None = None) -> list[str]:
    """The n most recent COMPLETE months (current calendar month excluded),
    oldest first."""
    today = date.today()
    cur = _month(today)
    rows = con.execute(
        f"""SELECT DISTINCT substr(date,1,7) mo FROM transactions
            WHERE date IS NOT NULL AND substr(date,1,7) < ?
            {'AND substr(date,1,7) < ?' if before else ''}
            ORDER BY mo DESC LIMIT ?""",
        ([cur, before, n] if before else [cur, n])).fetchall()
    return sorted(r["mo"] for r in rows)


def _month_cat_totals(con: sqlite3.Connection, months: list[str]) -> dict:
    """{month: {category: cents}} for spend debits."""
    if not months:
        return {}
    qmarks = ",".join("?" * len(months))
    out: dict[str, dict[str, int]] = {m: {} for m in months}
    for r in con.execute(
            f"""SELECT substr(t.date,1,7) mo, {ECAT} k,
                       ABS(SUM(t.amount_cents)) cents
                {JOIN} WHERE {SPEND} AND substr(t.date,1,7) IN ({qmarks})
                GROUP BY mo, k""", months):
        out[r["mo"]][r["k"] or "(uncategorised)"] = r["cents"] or 0
    return out


# ── 1. month-over-month waterfall: WHY spend changed ────────────────────────

def waterfall(con: sqlite3.Connection, movers: int = 6) -> dict:
    months = _complete_months(con, 2)
    if len(months) < 2:
        return {"available": False, "reason": "needs two complete months"}
    prev_m, cur_m = months
    totals = _month_cat_totals(con, months)
    cats = set(totals[prev_m]) | set(totals[cur_m])
    deltas = sorted(
        ({"category": k,
          "prev_cents": totals[prev_m].get(k, 0),
          "cur_cents": totals[cur_m].get(k, 0),
          "delta_cents": totals[cur_m].get(k, 0) - totals[prev_m].get(k, 0)}
         for k in cats),
        key=lambda d: abs(d["delta_cents"]), reverse=True)
    top = [d for d in deltas if d["delta_cents"] != 0][:movers]
    other = sum(d["delta_cents"] for d in deltas[len(top):])
    return {"available": True, "prev_month": prev_m, "cur_month": cur_m,
            "prev_total_cents": sum(totals[prev_m].values()),
            "cur_total_cents": sum(totals[cur_m].values()),
            "movers": top, "other_delta_cents": other}


# ── 2. run-rate pacing: feedback inside the month ───────────────────────────

def run_rate(con: sqlite3.Connection, history_months: int = 6) -> dict:
    today = date.today()
    cur_m, dom = _month(today), today.day
    mtd = con.execute(
        f"""SELECT ABS(COALESCE(SUM(t.amount_cents),0)) c {JOIN}
            WHERE {SPEND} AND substr(t.date,1,7)=?""", [cur_m]).fetchone()["c"]
    months = _complete_months(con, history_months)
    if not months:
        return {"available": False, "reason": "no complete months of history"}
    rests, fulls = [], []
    for m in months:
        row = con.execute(
            f"""SELECT
                  ABS(COALESCE(SUM(CASE WHEN CAST(substr(t.date,9,2) AS INT) > ?
                      THEN t.amount_cents END),0)) rest,
                  ABS(COALESCE(SUM(t.amount_cents),0)) full_c
                {JOIN} WHERE {SPEND} AND substr(t.date,1,7)=?""",
            [dom, m]).fetchone()
        rests.append(row["rest"] or 0)
        fulls.append(row["full_c"] or 0)
    # Not a naive linear projection: rent and annual bills make intra-month
    # spend lumpy, so pace against the user's own typical rest-of-month.
    projected = mtd + int(statistics.median(rests))
    median_month = int(statistics.median(fulls))
    return {"available": True, "month": cur_m, "day_of_month": dom,
            "mtd_cents": mtd, "projected_cents": projected,
            "median_month_cents": median_month,
            "delta_cents": projected - median_month,
            "history_months": len(months)}


# ── 3. recurring price creep: each rise is invisible; annualised it isn't ───

def price_creep(con: sqlite3.Connection, min_delta_cents: int = 50) -> dict:
    items = []
    for rc in con.execute("SELECT * FROM recurring_charges"):
        txns = con.execute(
            f"""SELECT ABS(t.amount_cents) c {JOIN}
                WHERE t.merchant_key=? AND t.direction='debit'
                  AND t.date IS NOT NULL ORDER BY t.date""",
            [rc["merchant_key"]]).fetchall()
        if len(txns) < 4:
            continue
        vals = [t["c"] for t in txns]
        k = min(3, len(vals) // 2)
        early = int(statistics.median(vals[:k]))
        late = int(statistics.median(vals[-k:]))
        delta = late - early
        if abs(delta) < max(min_delta_cents, early * 0.02):
            continue
        per_year = CADENCE_PER_YEAR.get(rc["cadence"], 12)
        name = con.execute(
            f"""SELECT {DISPLAY} n {JOIN}
                WHERE t.merchant_key=? LIMIT 1""",
            [rc["merchant_key"]]).fetchone()
        items.append({
            "merchant": (name["n"] if name else rc["display_name"]),
            "cadence": rc["cadence"], "occurrences": rc["occurrences"],
            "early_cents": early, "late_cents": late, "delta_cents": delta,
            "annualised_cents": delta * per_year})
    items.sort(key=lambda i: i["annualised_cents"], reverse=True)
    return {"available": True, "items": items,
            "total_annualised_cents": sum(i["annualised_cents"] for i in items)}


# ── 4. pareto focus: a few merchants are most of variable spend ─────────────

def pareto(con: sqlite3.Connection, days: int = 90, cut: float = 0.8,
           max_rows: int = 12) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    recurring = {r["merchant_key"] for r in con.execute(
        "SELECT merchant_key FROM recurring_charges")}
    rows = con.execute(
        f"""SELECT t.merchant_key mk, {DISPLAY} n,
                   ABS(SUM(t.amount_cents)) cents, COUNT(*) txns
            {JOIN} WHERE {SPEND} AND t.date >= ?
            GROUP BY t.merchant_key ORDER BY cents DESC""", [since]).fetchall()
    rows = [r for r in rows if r["mk"] not in recurring]
    total = sum(r["cents"] or 0 for r in rows) or 1
    months = _complete_months(con, 6)
    out, cum = [], 0
    for r in rows[:max_rows * 2]:
        cum += r["cents"] or 0
        if len(out) >= max_rows or (cum / total > cut and len(out) >= 5):
            break
        spark_rows = con.execute(
            f"""SELECT substr(t.date,1,7) mo, ABS(SUM(t.amount_cents)) c
                {JOIN} WHERE {SPEND} AND t.merchant_key=?
                  AND substr(t.date,1,7) >= ? GROUP BY mo""",
            [r["mk"], months[0] if months else "0000"]).fetchall()
        by_mo = {s["mo"]: s["c"] for s in spark_rows}
        out.append({"merchant": r["n"], "cents": r["cents"], "txns": r["txns"],
                    "share": round((r["cents"] or 0) / total, 3),
                    "cum_share": round(cum / total, 3),
                    "monthly_cents": [by_mo.get(m, 0) for m in months]})
    return {"available": True, "days": days,
            "variable_total_cents": total, "merchants": out}


# ── 5. category momentum: heating up / cooling down ─────────────────────────

def momentum(con: sqlite3.Connection, floor_cents: int = 5000) -> dict:
    months = _complete_months(con, 6)
    if len(months) < 6:
        return {"available": False, "reason": "needs six complete months"}
    totals = _month_cat_totals(con, months)
    older, recent = months[:3], months[3:]
    cats = {k for m in months for k in totals[m]}
    moves = []
    for k in cats:
        a = sum(totals[m].get(k, 0) for m in older) / 3
        b = sum(totals[m].get(k, 0) for m in recent) / 3
        if max(a, b) < floor_cents:
            continue
        moves.append({"category": k, "older_avg_cents": int(a),
                      "recent_avg_cents": int(b),
                      "delta_cents": int(b - a),
                      "pct": round((b - a) / a * 100) if a else None})
    moves.sort(key=lambda x: abs(x["delta_cents"]), reverse=True)
    return {"available": True,
            "recent_months": recent, "older_months": older,
            "heating": [x for x in moves if x["delta_cents"] > 0][:8],
            "cooling": [x for x in moves if x["delta_cents"] < 0][:8]}


# ── 6. anomalies: 'this month is 2.4x typical — and here's the culprit' ─────

def anomalies(con: sqlite3.Connection, lookback: int = 12,
              min_cents: int = 10000) -> dict:
    months = _complete_months(con, lookback + 1)
    if len(months) < 7:
        return {"available": False, "reason": "needs several months of history"}
    latest, history = months[-1], months[:-1]
    totals = _month_cat_totals(con, months)
    flags = []
    for k, cur in totals[latest].items():
        hist = [totals[m].get(k, 0) for m in history]
        med = statistics.median(hist)
        mad = statistics.median([abs(h - med) for h in hist]) or med * 0.2 or 1
        if cur < min_cents or cur <= med + 3 * mad or med <= 0:
            continue
        culprits = [dict(r) for r in con.execute(
            f"""SELECT t.date, ABS(t.amount_cents) cents, {DISPLAY} merchant
                {JOIN} WHERE {SPEND} AND substr(t.date,1,7)=? AND {ECAT}=?
                ORDER BY ABS(t.amount_cents) DESC LIMIT 3""", [latest, k])]
        flags.append({"category": k, "month": latest, "cents": cur,
                      "typical_cents": int(med),
                      "ratio": round(cur / med, 1) if med else None,
                      "culprits": culprits})
    flags.sort(key=lambda f: f["cents"] - f["typical_cents"], reverse=True)
    return {"available": True, "month": latest, "flags": flags}


# ── 7. savings wins: reward the behaviour the other devices prompt ──────────

def wins(con: sqlite3.Connection) -> dict:
    today = date.today()
    out = []
    for rc in con.execute("SELECT * FROM recurring_charges"):
        try:
            last = date.fromisoformat(rc["last_seen"])
            interval = float(rc["median_interval_days"] or 30)
            typical = int(float(rc["typical_amount"]) * 100)
        except (TypeError, ValueError):
            continue
        if (today - last).days > 2 * interval and typical > 0:
            per_year = CADENCE_PER_YEAR.get(rc["cadence"], 12)
            out.append({"kind": "cancelled_recurring",
                        "merchant": rc["display_name"],
                        "last_seen": rc["last_seen"],
                        "annualised_cents": typical * per_year})
    months = _complete_months(con, 15)
    if len(months) >= 15:
        totals = _month_cat_totals(con, months)
        recent, year_ago = months[-3:], months[:3]
        cats = {k for m in months for k in totals[m]}
        for k in cats:
            old = sum(totals[m].get(k, 0) for m in year_ago) / 3
            new = sum(totals[m].get(k, 0) for m in recent) / 3
            if old >= 10000 and new <= old * 0.8:
                out.append({"kind": "sustained_reduction", "category": k,
                            "old_monthly_cents": int(old),
                            "new_monthly_cents": int(new),
                            "annualised_cents": int((old - new) * 12)})
    out.sort(key=lambda w: w["annualised_cents"], reverse=True)
    return {"available": True, "wins": out,
            "total_annualised_cents": sum(w["annualised_cents"] for w in out)}


# ── 8. fixed vs variable: is the committed floor rising? ────────────────────

def fixed_floor(con: sqlite3.Connection, months_back: int = 24) -> dict:
    """The committed floor counts more than consumption: recurring unmatched
    outbound transfers (scheduled family contributions, standing obligations)
    belong in 'fixed' even though they are never consumption spend. Matched
    internal moves stay out entirely."""
    months = _complete_months(con, months_back)
    if not months:
        return {"available": False, "reason": "no complete months"}
    recurring = {r["merchant_key"] for r in con.execute(
        "SELECT merchant_key FROM recurring_charges")}
    qmarks = ",".join("?" * len(months))
    series = {m: {"fixed": 0, "variable": 0} for m in months}
    for r in con.execute(
            f"""SELECT substr(t.date,1,7) mo, t.merchant_key mk,
                       {ECAT} cat, ABS(SUM(t.amount_cents)) cents
                {JOIN}
                WHERE t.direction='debit' AND t.date IS NOT NULL
                  AND tl.txn_id IS NULL AND {ECAT} != 'TRANSFER_IN'
                  AND substr(t.date,1,7) IN ({qmarks})
                GROUP BY mo, t.merchant_key""", months):
        if r["mk"] in recurring:
            bucket = "fixed"
        elif r["cat"] in ("TRANSFER_OUT", "LOAN_PAYMENTS"):
            continue  # one-off unmatched transfers aren't floor or variable
        else:
            bucket = "variable"
        series[r["mo"]][bucket] += r["cents"] or 0
    return {"available": True, "months": months,
            "fixed_cents": [series[m]["fixed"] for m in months],
            "variable_cents": [series[m]["variable"] for m in months]}


def all_trends(con: sqlite3.Connection) -> dict:
    """Everything at once — the /viz page's single fetch."""
    return {"waterfall": waterfall(con), "run_rate": run_rate(con),
            "price_creep": price_creep(con), "pareto": pareto(con),
            "momentum": momentum(con), "anomalies": anomalies(con),
            "wins": wins(con), "fixed_floor": fixed_floor(con)}
