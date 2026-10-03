"""Local MCP server: read-only tools over the store, serving stdio.

The privilege split that defines this repo: sync.py is the only code that
ever builds a Redbark client, so it is the only place the bank key is
actually used. This process sits on the other side of that split. Its tool
surface is read-only, no tool writes, a test pins the exact tool list, and
nothing in its import graph makes a network call. So if it is ever asked
about money movement, the answer is structural: there is no such tool.

Three limits worth stating, so nobody relies on more than is here:

- Config.load() runs at import and parses the whole of `.env`, so the bank
  key does sit in this process's memory. What the split buys is narrower
  than "this process holds nothing": it is that no code path here uses the
  key. Anything that can read this process's memory has it.
- Per-query connections open with mode=ro, but the freshness envelope
  every response carries goes through queries.freshness(), which opens the
  store through Store: read-write, running migrations on connect. Being
  read-only is a property of the tools, not of the SQLite handle.
- Every response but one carries `as_of` + `stale` + `staleness_warnings`,
  so a stale store answers loudly rather than quietly. The exception is
  store_health, which returns the store's health record as-is: it has its
  own `stale` flag, the last sync run and per-connection warnings, but no
  `as_of` and no `staleness_warnings` list.

Register (per machine; PYTHONPATH makes the package importable from any
working directory, since nothing is pip-installed):
  claude mcp add -s user spendglass -e PYTHONPATH=<repo> -- \
    <repo>/.venv/bin/python -m spendglass.mcp_server
"""

from __future__ import annotations

import sqlite3

from mcp.server.mcpserver import MCPServer

from . import overrides as _overrides_mod
from . import queries
from .config import Config

mcp = MCPServer("spendglass")
_cfg = Config.load()


_schema_ensured = False  # set once, lazily — see _ensure_derived_schema


def _ensure_derived_schema() -> None:
    """queries.py, trends.py and themes.py join against `merchants`,
    `merchant_lookups`, `category_overrides` and `themes`/`theme_rules` for
    the effective-category formula (issue #90/#91) and theme matching —
    this process may be the very first one to ever open the store, with the
    UI server (which normally ensures these on its own startup, in
    create_app) never having run. Same additive, idempotent schemas as
    there: CREATE TABLE IF NOT EXISTS plus idempotent seeding, never a
    bank-sourced column. A fresh `themes` table still holds zero themes —
    nothing here invents one — but theme_spend can now say so correctly
    instead of reporting "themes not initialised".

    Backed up first, and synchronously — not racing a backup-scheduler
    thread the way the UI server's own startup does (see ui.py's
    build_app) — so "back up before migration" is an actual guarantee at
    this entry point. Skipped when nothing has changed since the newest
    snapshot, so repeated calls (or a restarted server, one per chat
    session) don't spam data/backups/.

    Lazy on purpose, run from the tools that need it rather than at import:
    importing this module (as the pinned-tool-list test does) must never
    touch a real store, with no `SPENDGLASS_DB` override in sight."""
    global _schema_ensured
    if _schema_ensured:
        return
    _schema_ensured = True
    from .enrich import ensure_schema as _enrich_schema
    from .lookup import ensure_schema as _lookup_schema
    from .store import Store
    from .themes import ensure_schema as _themes_schema
    from .transfers import ensure_schema as _transfers_schema
    try:
        from . import backup as _backup_mod
        if _backup_mod.changed_since_last_snapshot(_cfg.db_path):
            _backup_mod.backup(_cfg.db_path, _cfg.backup_keep, _cfg.backup_mirror_dir)
    except Exception:
        pass  # best-effort: no store yet, or a snapshot failed; proceed anyway
    try:
        with Store(_cfg.db_path) as _s:
            _enrich_schema(_s)
            _lookup_schema(_s)
            _transfers_schema(_s)
            _themes_schema(_s)
            _overrides_mod.ensure_schema(_s)
    except Exception:
        pass  # a store that predates sync entirely; queries fall back


def _ro_enriched_connection() -> sqlite3.Connection:
    """Same mode=ro connection as open_readonly(), for the tools whose SQL
    joins against the derived schema _ensure_derived_schema guarantees
    first. Plain list_accounts/store_health/etc. keep calling
    queries.open_readonly() directly — they don't need it."""
    _ensure_derived_schema()
    return queries.open_readonly(_cfg.db_path)


def _with_freshness(payload: dict) -> dict:
    return {**queries.freshness(_cfg.db_path), **payload}


@mcp.tool()
def list_accounts() -> dict:
    """List every bank account with its latest balance, institution, and the
    connection's consent status. Read-only, answered from the local store —
    no network call, no bank API."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness({"accounts": queries.list_accounts(con)})


@mcp.tool()
def search_transactions(
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
    """Search stored bank transactions. Filters combine with AND: free-text q
    (description/merchant), account_id (from list_accounts), merchant substring,
    category (EFFECTIVE category — the user's own overrides and corrections
    for known bank mislabelling, issue #90, already applied; e.g. 'ALCOHOL'
    finds bottle shops even though the bank filed them as Eating Out),
    direction ('debit' = money out, 'credit' = money in), status
    ('posted'/'pending'), date_from/date_to (YYYY-MM-DD), amount_min/
    amount_max (dollars, signed — debits are negative). Returns matched count,
    net_amount (SQL-computed — do not re-add amounts yourself), and up to
    `limit` rows, newest first. Each row carries the EFFECTIVE category and
    subcategory under `category`/`subcategory`, plus the bank's own,
    uncorrected CDR code under `raw_category` (and `raw_custom_category`)
    for when the raw bank value itself is what's asked about."""
    with _ro_enriched_connection() as con:
        return _with_freshness(
            queries.search_transactions(
                con, q=q, account_id=account_id, merchant=merchant,
                category=category, direction=direction, status=status,
                date_from=date_from, date_to=date_to,
                amount_min=amount_min, amount_max=amount_max, limit=limit,
            )
        )


@mcp.tool()
def spending_summary(
    group_by: str = "category",
    date_from: str | None = None,
    date_to: str | None = None,
    direction: str = "debit",
    top: int = 25,
) -> dict:
    """Aggregate spending in SQL: group_by one of 'category' (EFFECTIVE —
    the user's own overrides and corrections for known bank mislabelling,
    issue #90, already applied), 'subcategory' (effective), 'raw_category'
    (the bank's own, uncorrected CDR code, for when that's specifically
    what's asked about), 'merchant', 'month', 'account'; optional date
    range; direction 'debit' (money out, default), 'credit', or 'both'.
    Amounts are computed by the database from exact integer cents — report
    them as given, never recompute."""
    with _ro_enriched_connection() as con:
        return _with_freshness(
            queries.spending_summary(
                con, group_by=group_by, date_from=date_from,
                date_to=date_to, direction=direction, top=top,
            )
        )


@mcp.tool()
def list_recurring_charges() -> dict:
    """Detected recurring charges (subscriptions, utilities, rent): cadence,
    typical amount with observed min/max, occurrences, and next expected
    date. Derived deterministically from interval regularity — no model
    guessed at these. Run enrichment first if the list is empty."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(queries.list_recurring_charges(con))


@mcp.tool()
def spending_deltas(months: int = 6) -> dict:
    """Month-over-month spending changes: total debit per month, change vs
    the previous month, and the biggest per-category moves. All sums are
    SQL over integer cents — report figures as given, never recompute."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(queries.spending_deltas(con, months=months))


# ── trend devices: deterministic SQL; the model interprets, never computes ──
# Every device compares the user against their OWN trailing history — never a
# budget. Report figures as given; lead with the actionable number.

from . import trends as _trends


@mcp.tool()
def spend_change_waterfall() -> dict:
    """WHY spend changed: bridges the last two complete months' totals with
    the categories that drove the difference, biggest movers first plus an
    'everything else' remainder. Lead with the top mover as the actionable
    fact ("Dining drove most of the increase"), not the totals. All sums are
    SQL over integer cents — report as given."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.waterfall(con))


@mcp.tool()
def spending_run_rate() -> dict:
    """Mid-month pacing: month-to-date spend, a projection for the full month
    (MTD + the user's own median rest-of-month — deliberately NOT a linear
    extrapolation, which lies when rent and annual bills are lumpy), and the
    trailing median month as the baseline. Positive delta_cents = on track to
    overshoot their own typical month; that delta is the headline."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.run_rate(con))


@mcp.tool()
def price_creep() -> dict:
    """Recurring charges whose amount drifted between their earliest and
    latest occurrences, each annualised by cadence, plus the total. Individual
    rises look ignorable; the annualised figure is the decision — lead with
    it ("that streaming service is +$72/yr since it started")."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.price_creep(con))


@mcp.tool()
def merchant_pareto(days: int = 90) -> dict:
    """Focus list: the few merchants that make up most VARIABLE (non-
    recurring, non-interest) spend over the window, with each merchant's
    share, cumulative share, and recent monthly totals. Loan/credit
    interest is excluded — it's a committed cost, see fixed_vs_variable.
    Use it to ration attention — cutting one top-five merchant beats
    trimming twenty tail categories."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.pareto(con, days=days))


@mcp.tool()
def category_momentum() -> dict:
    """Slow drift the monthly view misses: each category's trailing 3-month
    average vs the 3 months before, split into heating (rising) and cooling
    (falling). Frame heating entries as trends to interrupt early, not
    emergencies."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.momentum(con))


@mcp.tool()
def spending_anomalies() -> dict:
    """Categories whose latest complete month exceeded the user's own
    12-month typical (median + 3×MAD), each with the culprit transactions
    attached. Always name the culprit merchant and date — an anomaly without
    an owner isn't actionable."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.anomalies(con))


@mcp.tool()
def savings_wins() -> dict:
    """Realised wins, annualised: recurring charges that stopped, and
    categories held well below their prior-year level for 3+ months. This is
    the reward loop — when the user has acted on other insights, lead with
    the cumulative locked-in figure."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.wins(con))


@mcp.tool()
def fixed_vs_variable() -> dict:
    """Monthly recurring (fixed) vs variable spend across the stored history.
    Loan/credit interest counts as fixed, alongside detected subscriptions —
    it's a committed cost of debt already taken on. A rising fixed floor is
    the earliest structural warning — it determines financial slack
    regardless of month-to-month discipline."""
    with _ro_enriched_connection() as con:
        return _with_freshness(_trends.fixed_floor(con))


@mcp.tool()
def subscriptions() -> dict:
    """Cadence-aware subscription overview: every active recurring charge
    with its honest ANNUALISED cost (a yearly plan is not a spike; never
    annualise a single charge without checking its cadence here first),
    upcoming renewals within 14 days (the window to cancel or downgrade),
    duplicate detection (two active lines at one vendor — usually an
    accidental double subscription), and recently stopped charges. Lead with
    duplicates and imminent renewals — those are actionable; the totals are
    context. All arithmetic is SQL/deterministic — report figures as given."""
    from . import subscriptions as _subs

    with _ro_enriched_connection() as con:
        try:
            return _with_freshness(_subs.overview(con))
        except Exception:
            return _with_freshness(
                {"available": False, "reason": "enrichment not run"})


@mcp.tool()
def theme_spend(months: int = 12) -> dict:
    """Cross-cutting theme lenses (e.g. Renovation, Pets, AI, Health &
    Fitness): trailing monthly spend per theme over the user's own defined
    rules. A theme collects one part of life across categories — a renovation
    is trades + hardware + architects; pets are supplies + vet + insurance.
    Use it when the question is "what is all of X costing me?" rather than a
    category question. Rules are user-editable; report figures as given."""
    from . import themes as _themes

    with _ro_enriched_connection() as con:
        try:
            out = _themes.summary(con, months_back=max(1, min(months, 24)))
            out["definitions"] = _themes.list_themes(con)
        except Exception:
            out = {"available": False,
                   "reason": "themes not initialised — open the UI once"}
        return _with_freshness(out)


@mcp.tool()
def store_health() -> dict:
    """Store status: row counts, last sync result, per-connection consent
    status with estimated expiry, and every active staleness warning. Check
    this first if any other answer looks off."""
    from .store import Store

    with Store(_cfg.db_path) as s:
        return s.health()


if __name__ == "__main__":
    mcp.run()
