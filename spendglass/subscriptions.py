"""Subscription detector — cadence-aware facts about recurring money.

Built on the deterministic recurring_charges table; this module adds the
judgements that make it decision-ready:

- annualised cost per line (a A$120 yearly charge is A$10/mo, not a spike —
  and a A$9.99 monthly is A$120/yr, not pocket change);
- ACTIVE vs STOPPED, by whether the charge is overdue beyond its own cadence;
- upcoming renewals, so annual plans stop being billing surprises;
- duplicate detection: two or more active lines resolving to the same vendor
  (overlapping plans for one service are an easy, expensive miss).

Everything is arithmetic over the store — no model, no network.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .trends import CADENCE_PER_YEAR

RENEW_SOON_DAYS = 14
# A charge is "stopped" when we're this far past its expected next hit:
# grace of one full interval plus a week for bank posting jitter.
def _grace_days(median_interval: float) -> float:
    return max(median_interval * 2, median_interval + 7)


def _cents(amount: str | float | None) -> int:
    try:
        return abs(round(float(amount or 0) * 100))
    except (TypeError, ValueError):
        return 0


def overview(con: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    rows = con.execute(
        """SELECT r.merchant_key, r.display_name, r.cadence, r.occurrences,
                  r.typical_amount, r.first_seen, r.last_seen, r.next_expected,
                  r.median_interval_days,
                  ml.resolved_name, ml.resolved_category, ml.resolved_subcategory
           FROM recurring_charges r
           LEFT JOIN merchant_lookups ml ON ml.merchant_key = r.merchant_key
             AND ml.status IN ('auto','approved','overridden')
           ORDER BY r.last_seen DESC""").fetchall()

    active, stopped = [], []
    for r in rows:
        per_year = CADENCE_PER_YEAR.get(r["cadence"], 12)
        typical = _cents(r["typical_amount"])
        try:
            last = date.fromisoformat(r["last_seen"])
        except (TypeError, ValueError):
            continue
        is_active = (today - last).days <= _grace_days(
            r["median_interval_days"] or 30)
        item = {
            "merchant_key": r["merchant_key"],
            "name": r["resolved_name"] or r["display_name"] or r["merchant_key"],
            "subcategory": r["resolved_subcategory"],
            "cadence": r["cadence"],
            "typical_cents": typical,
            "annualised_cents": typical * per_year,
            "monthly_equiv_cents": round(typical * per_year / 12),
            "occurrences": r["occurrences"],
            "last_seen": r["last_seen"],
            "next_expected": r["next_expected"],
            "billed_infrequently": r["cadence"] in ("quarterly", "yearly"),
        }
        if is_active:
            try:
                nxt = date.fromisoformat(r["next_expected"])
                item["renewing_soon"] = (
                    timedelta(0) <= (nxt - today) <= timedelta(RENEW_SOON_DAYS))
            except (TypeError, ValueError):
                item["renewing_soon"] = False
            active.append(item)
        else:
            item["stopped_since"] = r["last_seen"]
            stopped.append(item)

    # Duplicates: two or more ACTIVE lines resolving to the same vendor name.
    by_name: dict[str, list[dict]] = {}
    for a in active:
        by_name.setdefault(a["name"].strip().lower(), []).append(a)
    duplicates = []
    for group in by_name.values():
        if len(group) > 1:
            for a in group:
                a["duplicate"] = True
            duplicates.append({
                "name": group[0]["name"],
                "lines": len(group),
                "combined_annualised_cents": sum(
                    a["annualised_cents"] for a in group),
            })

    active.sort(key=lambda a: -a["annualised_cents"])
    stopped.sort(key=lambda a: a["stopped_since"], reverse=True)
    return {
        "active": active,
        "stopped": stopped[:20],
        "duplicates": sorted(duplicates,
                             key=lambda d: -d["combined_annualised_cents"]),
        "renewing_soon": [a for a in active if a.get("renewing_soon")],
        "total_annualised_cents": sum(a["annualised_cents"] for a in active),
        "total_monthly_equiv_cents": sum(
            a["monthly_equiv_cents"] for a in active),
    }
