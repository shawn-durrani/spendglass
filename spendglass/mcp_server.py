"""Local MCP server — read-only tools over the store, never the network.

The privilege split that defines this repo: sync.py holds the API key and
talks to Redbark; THIS process holds nothing and talks to nobody. It opens
the SQLite file read-only (mode=ro — writes raise at the database layer)
and serves its tools over stdio. If it's ever asked about money movement,
the honest answer is structural: there is no such tool.

Register (per machine; PYTHONPATH makes the package importable from any
working directory, since nothing is pip-installed):
  claude mcp add -s user spendglass -e PYTHONPATH=<repo> -- \
    <repo>/.venv/bin/python -m spendglass.mcp_server

Every response carries `as_of` + `stale` + `staleness_warnings` — a stale
store answers loudly, never quietly.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import queries
from .config import Config

mcp = MCPServer("spendglass")
_cfg = Config.load()


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
    category (CDR code), direction ('debit' = money out, 'credit' = money in),
    status ('posted'/'pending'), date_from/date_to (YYYY-MM-DD), amount_min/
    amount_max (dollars, signed — debits are negative). Returns matched count,
    net_amount (SQL-computed — do not re-add amounts yourself), and up to
    `limit` rows, newest first."""
    with queries.open_readonly(_cfg.db_path) as con:
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
    """Aggregate spending in SQL: group_by one of 'category', 'merchant',
    'month', 'account'; optional date range; direction 'debit' (money out,
    default), 'credit', or 'both'. Amounts are computed by the database from
    exact integer cents — report them as given, never recompute."""
    with queries.open_readonly(_cfg.db_path) as con:
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
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(_trends.waterfall(con))


@mcp.tool()
def spending_run_rate() -> dict:
    """Mid-month pacing: month-to-date spend, a projection for the full month
    (MTD + the user's own median rest-of-month — deliberately NOT a linear
    extrapolation, which lies when rent and annual bills are lumpy), and the
    trailing median month as the baseline. Positive delta_cents = on track to
    overshoot their own typical month; that delta is the headline."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(_trends.run_rate(con))


@mcp.tool()
def price_creep() -> dict:
    """Recurring charges whose amount drifted between their earliest and
    latest occurrences, each annualised by cadence, plus the total. Individual
    rises look ignorable; the annualised figure is the decision — lead with
    it ("that streaming service is +$72/yr since it started")."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(_trends.price_creep(con))


@mcp.tool()
def merchant_pareto(days: int = 90) -> dict:
    """Focus list: the few merchants that make up most VARIABLE (non-
    recurring) spend over the window, with each merchant's share, cumulative
    share, and recent monthly totals. Use it to ration attention — cutting
    one top-five merchant beats trimming twenty tail categories."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(_trends.pareto(con, days=days))


@mcp.tool()
def category_momentum() -> dict:
    """Slow drift the monthly view misses: each category's trailing 3-month
    average vs the 3 months before, split into heating (rising) and cooling
    (falling). Frame heating entries as trends to interrupt early, not
    emergencies."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(_trends.momentum(con))


@mcp.tool()
def spending_anomalies() -> dict:
    """Categories whose latest complete month exceeded the user's own
    12-month typical (median + 3×MAD), each with the culprit transactions
    attached. Always name the culprit merchant and date — an anomaly without
    an owner isn't actionable."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(_trends.anomalies(con))


@mcp.tool()
def savings_wins() -> dict:
    """Realised wins, annualised: recurring charges that stopped, and
    categories held well below their prior-year level for 3+ months. This is
    the reward loop — when the user has acted on other insights, lead with
    the cumulative locked-in figure."""
    with queries.open_readonly(_cfg.db_path) as con:
        return _with_freshness(_trends.wins(con))


@mcp.tool()
def fixed_vs_variable() -> dict:
    """Monthly recurring (fixed) vs variable spend across the stored history.
    A rising fixed floor is the earliest structural warning — it determines
    financial slack regardless of month-to-month discipline."""
    with queries.open_readonly(_cfg.db_path) as con:
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

    with queries.open_readonly(_cfg.db_path) as con:
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

    with queries.open_readonly(_cfg.db_path) as con:
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
