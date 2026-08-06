"""Local admin UI: loopback-only, password-protected, mostly reading.

One page served at http://127.0.0.1:8903 — login/setup and the transaction
table are the same document; JS switches views off /api/session. Every
query (filter, sort, totals) runs server-side in SQLite, so 10k+ rows stay
fast and money math never leaves SQL.

Security posture:
- Binds 127.0.0.1 only (start.sh / __main__ hardcode it) + a Host-header
  allowlist as DNS-rebinding defense.
- POSTs reject cross-site callers via Sec-Fetch-Site when the browser
  sends it; the session cookie is HttpOnly + SameSite=Strict.
- This surface writes. Do not describe it as read-only. The write paths,
  none of which edit what the bank sent:
  * labels: merchant-identity decisions (/api/lookups/decide) into the
    derived merchant_lookups table, and theme names and rules
    (/api/themes and friends);
  * operator settings (/api/admin/config) and the miner status rows a run
    records (/api/admin/run), both in `meta`;
  * provider keys (/api/admin/key), written into `.env` at 0600 after the
    key is sent to that provider once to validate it;
  * the password record (/api/setup) in data/ui_auth.json and session
    digests (/api/login, /api/logout) in data/ui_sessions.json.
  The one column the app writes on a bank row is the derived
  transactions.merchant_key, set by the enrichment miner. Balances,
  amounts, dates, descriptions and every other field the bank sent are
  written only by sync.py, which a run-now sync launches as a subprocess.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from . import capabilities
from . import themes as themes_mod
from .auth import Auth
from .config import Config
from .enrich import merchant_key, strip_noise
from .lookup import (SETTING_DEFAULTS, decide, ensure_schema, get_settings,
                     identify_clear_merchants, lookup_merchants,
                     propagate_decisions, set_settings)
from .store import Store

# Overridable so a second instance (e.g. against a test store) can run
# beside the real one; the Host allowlist tracks whatever port is chosen.
PORT = int(os.environ.get("SPENDGLASS_UI_PORT", "8903"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}
COOKIE = "spendglass_session"

# Sortable columns: an allowlist mapped to real SQL, so user input is never
# interpolated — unknown sort keys are a 400, not an injection surface.
SORT_COLUMNS = {
    "date": "t.date",
    "description": "t.description",
    "merchant": "t.merchant_name",
    "amount": "t.amount_cents",
    "direction": "t.direction",
    "category": "t.category",
    "custom_category": "t.custom_category",
    "account": "a.name",
    "status": "t.status",
    # Resolved via the materialised transactions.merchant_key; NULLs (not yet
    # attributed) sort together at the end.
    "subcategory": "ml.resolved_subcategory",
}


def create_app(db_path: Path, auth: Auth, propagator=None,
               backup_interval_hours: float | None = None) -> FastAPI:
    """`propagator(confirmed_decisions)` runs after approvals on a background
    thread — production wires the model-backed one; tests get none.
    `backup_interval_hours` is display-only: it lets the banner call a
    stalled backup stalled; the scheduler itself starts in build_app."""
    app = FastAPI(title="spendglass", docs_url=None, redoc_url=None)

    # Derived-table schemas the queries join against; safe and idempotent.
    try:
        from .enrich import ensure_schema as _enrich_schema
        from .transfers import ensure_schema as _transfers_schema
        with Store(db_path) as _s:
            _enrich_schema(_s)
            ensure_schema(_s)
            _transfers_schema(_s)
            themes_mod.ensure_schema(_s)
    except Exception:
        pass  # a store that predates sync entirely; queries fall back

    def db() -> sqlite3.Connection:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return con

    # ── middleware: loopback host + cross-site POST rejection ───────────────

    @app.middleware("http")
    async def guards(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "host not allowed"}, status_code=400)
        if request.method == "POST":
            sfs = request.headers.get("sec-fetch-site")
            if sfs not in (None, "same-origin", "none"):
                return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        return await call_next(request)

    # ── auth plumbing ───────────────────────────────────────────────────────

    def require_session(request: Request) -> None:
        if not auth.check_session(request.cookies.get(COOKIE)):
            raise HTTPException(status_code=401, detail="not authenticated")

    @app.get("/api/session")
    def session_state(request: Request) -> dict:
        return {
            "authenticated": auth.check_session(request.cookies.get(COOKIE)),
            "first_run": auth.first_run,
        }

    @app.post("/api/setup")
    async def setup(request: Request, response: Response) -> dict:
        body = await request.json()
        try:
            ok = auth.set_password(body.get("recovery_secret", ""), body.get("password", ""))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=403, detail="recovery secret does not match "
                                "the one printed in the server terminal")
        response.set_cookie(COOKIE, auth.create_session(), httponly=True,
                            samesite="strict", max_age=24 * 3600)
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request, response: Response) -> dict:
        body = await request.json()
        if not auth.check_password(body.get("password", "")):
            raise HTTPException(status_code=403, detail="wrong password")
        response.set_cookie(COOKIE, auth.create_session(), httponly=True,
                            samesite="strict", max_age=24 * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict:
        auth.revoke_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ── read-only data endpoints ────────────────────────────────────────────

    @app.get("/api/health", dependencies=[Depends(require_session)])
    def health() -> dict:
        from . import backup as backup_mod
        with Store(db_path) as s:
            h = s.health()
        snap = backup_mod.last_snapshot(db_path)
        # Epoch ms so the client needs no date parsing; None = never.
        h["last_backup"] = int(snap.stat().st_mtime * 1000) if snap else None
        h["backup_interval_hours"] = backup_interval_hours
        return h

    @app.get("/api/accounts", dependencies=[Depends(require_session)])
    def accounts() -> list[dict]:
        con = db()
        try:
            return [dict(r) for r in con.execute(
                "SELECT id, name, institution_name, type, currency FROM accounts ORDER BY name"
            )]
        finally:
            con.close()

    @app.get("/api/categories", dependencies=[Depends(require_session)])
    def categories() -> list[dict]:
        con = db()
        try:
            return [dict(r) for r in con.execute(
                "SELECT key, label FROM categories ORDER BY label")]
        finally:
            con.close()

    @app.get("/api/transactions", dependencies=[Depends(require_session)])
    def transactions(
        sort: str = "date", dir: str = "desc",
        limit: int = 100, offset: int = 0,
        q: str | None = None, account: str | None = None,
        category: str | None = None, direction: str | None = None,
        status: str | None = None, date_from: str | None = None,
        date_to: str | None = None, amount_min: float | None = None,
        amount_max: float | None = None, theme: str | None = None,
    ) -> dict:
        if sort not in SORT_COLUMNS:
            raise HTTPException(status_code=400, detail=f"unknown sort column '{sort}'")
        order = "ASC" if dir == "asc" else "DESC"
        limit = max(1, min(limit, 500))

        where, params = ["1=1"], []
        if q:
            where.append("(t.description LIKE ? OR t.merchant_name LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if account:
            where.append("t.account_id = ?"); params.append(account)
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
        if theme:
            where.append(themes_mod.MATCH); params.append(theme)
        cond = " AND ".join(where)

        con = db()
        try:
            totals = con.execute(
                f"""SELECT COUNT(*) n, COALESCE(SUM(t.amount_cents),0) sum_cents
                    FROM transactions t WHERE {cond}""", params).fetchone()
            base_query = """SELECT t.id, t.date, t.description, t.merchant_name,
                           t.amount, t.amount_cents, t.direction, t.category,
                           t.custom_category, t.status, {internal_col},
                           a.name AS account_name, a.currency
                    FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id
                    {ml_join}
                    WHERE {cond}
                    ORDER BY {sort_sql} {order}, t.id
                    LIMIT ? OFFSET ?"""
            ml_join = ("""LEFT JOIN merchant_lookups ml
                          ON ml.merchant_key = t.merchant_key
                          AND ml.status IN ('auto','approved','overridden')
                          LEFT JOIN transfer_links tl
                          ON tl.txn_id = t.id AND tl.kind='internal'""")
            try:
                rows = con.execute(
                    base_query.format(
                        ml_join=ml_join, cond=cond,
                        internal_col="(tl.txn_id IS NOT NULL) AS internal",
                        sort_sql=SORT_COLUMNS[sort], order=order),
                    [*params, limit, offset]).fetchall()
            except sqlite3.OperationalError:
                # store predates the lookup tables / materialised key
                rows = con.execute(
                    base_query.format(
                        ml_join="", cond=cond, internal_col="0 AS internal",
                        order=order,
                        sort_sql=SORT_COLUMNS[sort] if sort != "subcategory"
                        else "t.date"),
                    [*params, limit, offset]).fetchall()
            # Best-known identity per merchant key: a confirmed/auto lookup
            # beats the normalised label beats the raw bank field. The raw
            # value stays in the payload untouched.
            try:
                ident = {}
                for r in con.execute(
                    """SELECT m.key, m.display_name, l.status, l.resolved_name,
                              l.resolved_subcategory, l.resolved_category
                       FROM merchants m
                       LEFT JOIN merchant_lookups l ON l.merchant_key = m.key"""):
                    confirmed = r["status"] in ("auto", "approved", "overridden")
                    ident[r["key"]] = (
                        (r["resolved_name"] if confirmed and r["resolved_name"]
                         else r["display_name"]),
                        r["resolved_subcategory"] if confirmed else None,
                        r["resolved_category"] if confirmed else None,
                        r["status"],
                    )
            except sqlite3.OperationalError:  # enrichment never run on this store
                ident = {}
            data = []
            for r in rows:
                d = dict(r)
                key = merchant_key(r["merchant_name"], r["description"])
                name, sub, cat, prov = ident.get(key, (None, None, None, None))
                d["merchant_key"] = key
                d["merchant_display"] = (
                    name or strip_noise(r["merchant_name"] or "") or None)
                d["subcategory"] = sub
                d["category_display"] = cat or r["category"]  # override wins
                d["merchant_prov"] = prov  # auto|approved|overridden|pending|None
                data.append(d)
            return {"total": totals["n"], "sum_cents": totals["sum_cents"],
                    "limit": limit, "offset": offset, "data": data}
        finally:
            con.close()

    # ── merchant-identity review (the one write path — labels only) ─────────

    @app.get("/api/lookups", dependencies=[Depends(require_session)])
    def lookups(status: str = "pending", q: str | None = None,
                plumbing: int = 0) -> dict:
        if status not in ("pending", "auto", "approved", "overridden",
                          "rejected", "all"):
            raise HTTPException(status_code=400, detail="unknown status filter")
        con = db()
        try:
            cond, params = "1=1", []
            if status != "all":
                cond, params = "l.status = ?", [status]
            # Transfers, fees, loan lines and income aren't merchants — they
            # belong to the transfer matcher, not this queue.
            plumb = ("" if plumbing else
                     " AND (COALESCE(m.llm_category, m.cdr_category) IS NULL"
                     " OR COALESCE(m.llm_category, m.cdr_category) NOT IN"
                     " ('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS',"
                     "'BANK_FEES','INCOME'))")
            try:
                rows = con.execute(
                    f"""SELECT l.*, m.display_name, m.txn_count,
                               m.total_debit_cents,
                               COALESCE(m.llm_category, m.cdr_category)
                                 AS category
                        FROM merchant_lookups l
                        LEFT JOIN merchants m ON m.key = l.merchant_key
                        WHERE {cond}{plumb}
                        ORDER BY ABS(COALESCE(m.total_debit_cents,0)) DESC""",
                    params).fetchall()
                counts = {r["status"]: r["n"] for r in con.execute(
                    f"""SELECT l.status, COUNT(*) n FROM merchant_lookups l
                        LEFT JOIN merchants m ON m.key = l.merchant_key
                        WHERE 1=1{plumb} GROUP BY l.status""")}
            except sqlite3.OperationalError:
                # Store predates enrichment aggregates — serve unjoined,
                # without the plumbing filter (it needs merchant categories).
                try:
                    rows = con.execute(
                        f"""SELECT l.*, NULL AS display_name, NULL AS txn_count,
                                   NULL AS total_debit_cents, NULL AS category
                            FROM merchant_lookups l WHERE {cond}
                            ORDER BY l.merchant_key""", params).fetchall()
                    counts = {r["status"]: r["n"] for r in con.execute(
                        "SELECT status, COUNT(*) n FROM merchant_lookups "
                        "GROUP BY status")}
                except sqlite3.OperationalError:  # lookup never run at all
                    return {"data": [], "counts": {}}
            data = [dict(r) for r in rows]
            if q:
                ql = q.lower()
                data = [r for r in data if ql in " ".join(
                    str(r.get(f) or "") for f in
                    ("merchant_key", "display_name", "proposed_name",
                     "proposed_subcategory", "proposed_summary")).lower()]
            # The strict tree: each subcategory with its parent category.
            # Fallback to the flat distinct list if the tree isn't built yet.
            try:
                subcats = [{"name": r["name"], "category": r["category"]}
                           for r in con.execute(
                               "SELECT name, category FROM subcategories "
                               "ORDER BY name")]
            except sqlite3.OperationalError:
                subcats = []
            if not subcats:
                subcats = [{"name": r["s"], "category": None} for r in con.execute(
                    """SELECT DISTINCT
                         COALESCE(resolved_subcategory, proposed_subcategory) s
                       FROM merchant_lookups
                       WHERE COALESCE(resolved_subcategory, proposed_subcategory)
                             != '' ORDER BY s""")]
            return {"data": data, "counts": counts, "subcategories": subcats}
        finally:
            con.close()

    @app.post("/api/lookups/decide", dependencies=[Depends(require_session)])
    async def lookups_decide(request: Request) -> dict:
        body = await request.json()
        decisions = body.get("decisions") or []
        if not isinstance(decisions, list) or not decisions:
            raise HTTPException(status_code=400,
                                detail="decisions: non-empty list required")
        con = db()
        try:
            valid_cats = {r["key"] for r in con.execute("SELECT key FROM categories")}
        finally:
            con.close()
        for d in decisions:
            if not (d.get("merchant_key") or "").strip() \
                    or d.get("action") not in ("approve", "reject"):
                raise HTTPException(
                    status_code=400,
                    detail="each decision needs merchant_key and action approve|reject")
            cat = (d.get("category") or "").strip()
            if cat and cat not in valid_cats:
                raise HTTPException(status_code=400,
                                    detail=f"unknown category '{cat}'")
        with Store(db_path) as s:
            ensure_schema(s)
            for d in decisions:
                decide(s, d["merchant_key"].strip(),
                       (d.get("name") or "").strip() or None,
                       (d.get("subcategory") or "").strip() or None,
                       (d.get("category") or "").strip() or None,
                       approve=(d["action"] == "approve"))
            # What the user actually confirmed, for the propagation pass.
            approved_keys = [d["merchant_key"].strip() for d in decisions
                             if d["action"] == "approve"]
            confirmed = [dict(r) for r in s.con.execute(
                f"""SELECT merchant_key, resolved_name AS name,
                           resolved_subcategory AS subcategory, proposed_summary
                    FROM merchant_lookups
                    WHERE merchant_key IN ({','.join('?' * len(approved_keys))})""",
                approved_keys)] if approved_keys else []

        with Store(db_path) as s:
            prop_on = bool(get_settings(s)["propagation"])
        if confirmed and propagator and prop_on:
            threading.Thread(target=propagator, args=(confirmed,),
                             daemon=True).start()
        return {"ok": True, "decided": len(decisions)}

    # ── admin: miner settings + run-now (writes meta only) ──────────────────

    def _set_status(name: str, payload: dict) -> None:
        with Store(db_path) as s:
            s.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                          (f"miner.status.{name}", json.dumps(payload)))
            s.con.commit()

    def _run_miner(name: str, limit: int) -> None:
        started = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        _set_status(name, {"state": "running", "started_at": started})
        try:
            if name == "enrich":
                from .enrich import detect_recurring, rebuild_merchants
                from .transfers import match_transfers
                with Store(db_path) as s:
                    result = {"merchants": rebuild_merchants(s),
                              "recurring": detect_recurring(s),
                              "internal_pairs": match_transfers(s)["pairs"]}
            else:
                import anthropic

                from .config import REPO_ROOT, _parse_env_file
                key = os.environ.get("ANTHROPIC_API_KEY") or _parse_env_file(
                    REPO_ROOT / ".env").get("ANTHROPIC_API_KEY")
                if not key:
                    raise RuntimeError("ANTHROPIC_API_KEY is not set")
                client = anthropic.Anthropic(api_key=key)
                with Store(db_path) as s:
                    if name == "classify":
                        from .enrich import classify_merchants
                        result = classify_merchants(s, client)
                    elif name == "sweep":
                        result = identify_clear_merchants(s, client)
                    else:
                        result = lookup_merchants(s, client, limit=limit)
                        result.pop("results", None)
            _set_status(name, {
                "state": "done", "started_at": started, "result": result,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            })
        except Exception as e:
            _set_status(name, {"state": "error", "started_at": started,
                               "error": str(e)[:300]})

    @app.get("/api/admin", dependencies=[Depends(require_session)])
    def admin_state() -> dict:
        with Store(db_path) as s:
            ensure_schema(s)
            settings = get_settings(s)
            miners = {}
            for r in s.con.execute(
                    "SELECT key, value FROM meta WHERE key LIKE 'miner.status.%'"):
                try:
                    miners[r["key"][len("miner.status."):]] = json.loads(r["value"])
                except ValueError:
                    pass
            lo, hi, n = s.con.execute(
                "SELECT MIN(date), MAX(date), COUNT(*) FROM transactions").fetchone()
        return {"settings": settings, "miners": miners,
                "capabilities": capabilities.status(),
                "data": {"lo": lo, "hi": hi, "n": n}}

    @app.post("/api/admin/config", dependencies=[Depends(require_session)])
    async def admin_config(request: Request) -> dict:
        body = await request.json()
        updates = {}
        checks = {
            "auto_threshold": lambda v: 0.0 <= float(v) <= 1.0 and float(v),
            "max_searches": lambda v: 1 <= int(v) <= 5 and int(v),
            "workers": lambda v: 1 <= int(v) <= 8 and int(v),
            "propagation": lambda v: int(bool(int(v))) in (0, 1) and int(bool(int(v))),
            "sync_interval_hours": lambda v: 0 <= float(v) <= 168 and float(v),
        }
        for k in SETTING_DEFAULTS:
            if k not in body:
                continue
            try:
                v = checks[k](body[k])
                if v is False:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"invalid {k}")
            updates[k] = v
        with Store(db_path) as s:
            set_settings(s, updates)
        return {"ok": True, "updated": sorted(updates)}

    @app.post("/api/admin/key", dependencies=[Depends(require_session)])
    async def admin_key(request: Request) -> dict:
        """Validate-then-persist a provider key. Saving sends the key to
        that provider once to check it works, and writes it to .env at 0600
        only if the check passes. It is never logged or shown back."""
        body = await request.json()
        return capabilities.save_key(str(body.get("service") or ""),
                                     str(body.get("key") or ""))

    @app.post("/api/admin/run", dependencies=[Depends(require_session)])
    async def admin_run(request: Request) -> dict:
        body = await request.json()
        miner = body.get("miner")
        if miner not in ("lookup", "classify", "enrich", "sweep", "sync"):
            raise HTTPException(status_code=400, detail="unknown miner")
        limit = max(1, min(int(body.get("limit") or 50), 1000))
        with Store(db_path) as s:
            row = s.con.execute("SELECT value FROM meta WHERE key=?",
                                (f"miner.status.{miner}",)).fetchone()
        if row:
            try:
                st = json.loads(row["value"])
                if st.get("state") == "running":
                    raise HTTPException(status_code=409,
                                        detail=f"{miner} is already running")
            except ValueError:
                pass
        if miner == "sync":
            # Runs as a subprocess that reads .env itself, so the bank is
            # never called from this process. (Config.load() at startup
            # does put the key in this process's memory; nothing here
            # uses it.)
            def _sync_bg() -> None:
                from . import autosync
                try:
                    autosync.run_once(db_path)
                except Exception:
                    pass  # run_once already recorded the reason
            threading.Thread(target=_sync_bg, daemon=True).start()
        else:
            threading.Thread(target=_run_miner, args=(miner, limit),
                             daemon=True).start()
        return {"ok": True, "started": miner}

    # ── visualisation data (read-only) ──────────────────────────────────────
    # Effective category: user override > identity-informed model > bank.
    _ECAT = ("COALESCE(ml.resolved_category, m.llm_category, m.cdr_category, "
             "t.category)")
    _VIZ_JOIN = """FROM transactions t
        LEFT JOIN merchants m ON m.key = t.merchant_key
        LEFT JOIN merchant_lookups ml ON ml.merchant_key = t.merchant_key
          AND ml.status IN ('auto','approved','overridden')
        LEFT JOIN transfer_links tl ON tl.txn_id = t.id AND tl.kind='internal'"""
    # Transfers between own accounts and loan principal aren't consumption —
    # excluded from spend views by default (toggle in the UI). Loan INTEREST
    # is money gone forever, so those subcategories count as spend even under
    # the Loan Payments category.
    _NON_SPEND = "('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS')"
    _LOAN_SPEND_SUBS = "('Mortgage Interest','Card Interest','Loan Fees')"

    def _viz_where(date_from, date_to, transfers: bool) -> tuple[str, list]:
        where, params = ["t.date IS NOT NULL"], []
        if date_from:
            where.append("t.date >= ?"); params.append(date_from)
        if date_to:
            where.append("t.date <= ?"); params.append(date_to)
        if not transfers:
            # matched internal moves always out; unmatched transfer-category
            # rows out of consumption until recategorised
            where.append(
                f"tl.txn_id IS NULL AND ({_ECAT} NOT IN {_NON_SPEND}"
                f" OR ({_ECAT} = 'LOAN_PAYMENTS'"
                f"     AND ml.resolved_subcategory IN {_LOAN_SPEND_SUBS}))")
        return " AND ".join(where), params

    @app.get("/api/viz/series", dependencies=[Depends(require_session)])
    def viz_series(date_from: str | None = None, date_to: str | None = None,
                   bucket: str = "month", transfers: int = 0,
                   category: str | None = None, subcategory: str | None = None,
                   theme: str | None = None,
                   exclude_cats: str | None = None,
                   exclude_subs: str | None = None,
                   split: int = 0) -> dict:
        fmt = {"day": "t.date", "week": "strftime('%Y-%m-%d', t.date, 'weekday 0', '-6 days')",
               "month": "substr(t.date,1,7)"}.get(bucket)
        if fmt is None:
            raise HTTPException(status_code=400, detail="bucket must be day|week|month")
        cond, params = _viz_where(date_from, date_to, bool(transfers))
        # Drill focus: the timeline narrows to whatever the user drilled into.
        if theme:
            cond += f" AND {themes_mod.MATCH}"; params.append(theme)
        if category:
            cond += f" AND {_ECAT} = ?"; params.append(category)
        if subcategory:
            if subcategory == "(no subcategory)":
                cond += " AND ml.resolved_subcategory IS NULL"
            else:
                cond += " AND ml.resolved_subcategory = ?"
                params.append(subcategory)
        # User-toggled exclusions (chips on the spending page).
        ex_c = [x for x in (exclude_cats or "").split(",") if x]
        if ex_c:
            cond += f" AND {_ECAT} NOT IN ({','.join('?' * len(ex_c))})"
            params += ex_c
        ex_s = [x for x in (exclude_subs or "").split(",") if x]
        if ex_s:
            cond += (" AND (ml.resolved_subcategory IS NULL OR "
                     f"ml.resolved_subcategory NOT IN ({','.join('?' * len(ex_s))}))")
            params += ex_s
        con = db()
        try:
            try:
                rows = con.execute(
                    f"""SELECT {fmt} b,
                          ABS(SUM(CASE WHEN t.direction='debit' THEN t.amount_cents ELSE 0 END)) debit_cents,
                          ABS(SUM(CASE WHEN t.direction='credit' THEN t.amount_cents ELSE 0 END)) credit_cents,
                          COUNT(*) n
                        {_VIZ_JOIN} WHERE {cond} GROUP BY b ORDER BY b""",
                    params).fetchall()
            except sqlite3.OperationalError:  # store predates enrichment
                cond2 = cond.replace(_ECAT, "t.category")
                rows = con.execute(
                    f"""SELECT {fmt} b,
                          ABS(SUM(CASE WHEN t.direction='debit' THEN t.amount_cents ELSE 0 END)) debit_cents,
                          ABS(SUM(CASE WHEN t.direction='credit' THEN t.amount_cents ELSE 0 END)) credit_cents,
                          COUNT(*) n
                        FROM transactions t WHERE {cond2} GROUP BY b ORDER BY b""",
                    params).fetchall()
            data = [dict(r) for r in rows]
            out = {"buckets": data,
                   "total_debit_cents": sum(r["debit_cents"] or 0 for r in data),
                   "txn_count": sum(r["n"] for r in data)}
            if split:
                # Per-bucket contributors, so a spike explains itself at a
                # glance: inside a category drill the dimension is its
                # subcategories, otherwise the category.
                dim = ("COALESCE(ml.resolved_subcategory,'(no subcategory)')"
                       if category else _ECAT)
                try:
                    srows = con.execute(
                        f"""SELECT {fmt} b, {dim} k,
                              ABS(SUM(CASE WHEN t.direction='debit'
                                  THEN t.amount_cents ELSE 0 END)) cents
                            {_VIZ_JOIN} WHERE {cond}
                            GROUP BY b, k""", params).fetchall()
                    totals: dict[str, int] = {}
                    for r in srows:
                        totals[r["k"]] = totals.get(r["k"], 0) + (r["cents"] or 0)
                    top = [k for k, _ in sorted(totals.items(),
                                                key=lambda i: -i[1])[:6]]
                    keys = [b["b"] for b in data]
                    series = {k: {b: 0 for b in keys} for k in [*top, "(other)"]}
                    for r in srows:
                        k = r["k"] if r["k"] in top else "(other)"
                        if r["b"] in series[k]:
                            series[k][r["b"]] += r["cents"] or 0
                    out["split"] = [
                        {"key": k, "cents": [series[k][b] for b in keys]}
                        for k in [*top, "(other)"]
                        if any(series[k].values())]
                except sqlite3.OperationalError:
                    pass  # pre-enrichment store: totals still work
            return out
        finally:
            con.close()

    @app.get("/api/viz/categories", dependencies=[Depends(require_session)])
    def viz_categories(date_from: str | None = None, date_to: str | None = None,
                       transfers: int = 0) -> dict:
        cond, params = _viz_where(date_from, date_to, bool(transfers))
        con = db()
        try:
            try:
                cats = con.execute(
                    f"""SELECT {_ECAT} k, ABS(SUM(t.amount_cents)) cents, COUNT(*) n
                        {_VIZ_JOIN}
                        WHERE {cond} AND t.direction='debit'
                        GROUP BY k ORDER BY cents DESC""", params).fetchall()
                subs = con.execute(
                    f"""SELECT {_ECAT} k, ml.resolved_subcategory s,
                               ABS(SUM(t.amount_cents)) cents, COUNT(*) n
                        {_VIZ_JOIN}
                        WHERE {cond} AND t.direction='debit'
                        GROUP BY k, s ORDER BY cents DESC""", params).fetchall()
            except sqlite3.OperationalError:
                cond2 = cond.replace(_ECAT, "t.category")
                cats = con.execute(
                    f"""SELECT t.category k, ABS(SUM(t.amount_cents)) cents, COUNT(*) n
                        FROM transactions t WHERE {cond2} AND t.direction='debit'
                        GROUP BY k ORDER BY cents DESC""", params).fetchall()
                subs = []
            out = {r["k"] or "(uncategorised)": {
                       "key": r["k"] or "(uncategorised)", "cents": r["cents"] or 0,
                       "n": r["n"], "subs": []} for r in cats}
            for r in subs:
                k = r["k"] or "(uncategorised)"
                if k in out:
                    out[k]["subs"].append({
                        "name": r["s"] or "(no subcategory)",
                        "cents": r["cents"] or 0, "n": r["n"]})
            return {"categories": list(out.values())}
        finally:
            con.close()

    @app.get("/api/taxonomy", dependencies=[Depends(require_session)])
    def taxonomy() -> dict:
        """Categories (bank CDR keys + labels) and the subcategory tree —
        everything an editor needs for suggestions."""
        con = db()
        try:
            cats = [dict(r) for r in con.execute(
                "SELECT key, label FROM categories ORDER BY label")]
            try:
                subs = [dict(r) for r in con.execute(
                    "SELECT name, category FROM subcategories ORDER BY name")]
            except sqlite3.OperationalError:
                subs = []
            return {"categories": cats, "subcategories": subs}
        finally:
            con.close()

    @app.get("/api/trends", dependencies=[Depends(require_session)])
    def trends_all() -> dict:
        from . import trends
        con = db()
        try:
            try:
                return trends.all_trends(con)
            except sqlite3.OperationalError:
                return {k: {"available": False, "reason": "enrichment not run"}
                        for k in ("waterfall", "run_rate", "price_creep",
                                  "pareto", "momentum", "anomalies", "wins",
                                  "fixed_floor")}
        finally:
            con.close()

    @app.get("/api/subscriptions", dependencies=[Depends(require_session)])
    def subscriptions_overview() -> dict:
        from . import subscriptions
        con = db()
        try:
            try:
                return subscriptions.overview(con)
            except sqlite3.OperationalError:
                return {"active": [], "stopped": [], "duplicates": [],
                        "renewing_soon": [], "total_annualised_cents": 0,
                        "total_monthly_equiv_cents": 0}
        finally:
            con.close()

    @app.get("/api/themes", dependencies=[Depends(require_session)])
    def themes_summary(months: int = 12) -> dict:
        templates = [{"name": n, "rules": [{"kind": k, "value": v}
                                           for k, v in rules]}
                     for n, rules in themes_mod.TEMPLATES.items()]
        con = db()
        try:
            try:
                out = themes_mod.summary(con, max(1, min(months, 24)))
                out["definitions"] = themes_mod.list_themes(con)
            except sqlite3.OperationalError:
                out = {"months": [], "themes": [], "definitions": []}
            out["templates"] = templates
            return out
        finally:
            con.close()

    # Theme writes are the same class as merchant decisions: user judgments
    # over lens data, never bank rows. POST-only keeps them behind the
    # same-site guard.
    @app.post("/api/themes", dependencies=[Depends(require_session)])
    async def themes_create(request: Request) -> dict:
        body = await request.json()
        name = (body.get("name") or "").strip()
        template = body.get("template")
        with Store(db_path) as s:
            themes_mod.ensure_schema(s)
            try:
                themes_mod.create_theme(s, name, template)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except sqlite3.IntegrityError:
                raise HTTPException(status_code=409,
                                    detail=f"theme '{name}' already exists")
            return {"ok": True, "definitions": themes_mod.list_themes(s.con)}

    @app.post("/api/themes/delete", dependencies=[Depends(require_session)])
    async def themes_delete(request: Request) -> dict:
        body = await request.json()
        name = (body.get("name") or "").strip()
        with Store(db_path) as s:
            themes_mod.ensure_schema(s)
            if not themes_mod.delete_theme(s, name):
                raise HTTPException(status_code=404,
                                    detail=f"no theme named '{name}'")
            return {"ok": True, "definitions": themes_mod.list_themes(s.con)}

    @app.post("/api/themes/rules", dependencies=[Depends(require_session)])
    async def themes_rules(request: Request) -> dict:
        body = await request.json()
        action = body.get("action")
        if action not in ("add", "remove"):
            raise HTTPException(status_code=400,
                                detail="action must be add|remove")
        theme = (body.get("theme") or "").strip()
        kind = body.get("kind")
        value = (body.get("value") or "").strip()
        with Store(db_path) as s:
            themes_mod.ensure_schema(s)
            try:
                if action == "add":
                    themes_mod.add_rule(s, theme, kind, value)
                elif not themes_mod.remove_rule(s, theme, kind, value):
                    raise HTTPException(status_code=404,
                                        detail="no such rule")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            return {"ok": True, "definitions": themes_mod.list_themes(s.con)}

    @app.get("/api/viz/transactions", dependencies=[Depends(require_session)])
    def viz_transactions(date_from: str | None = None, date_to: str | None = None,
                         category: str | None = None,
                         subcategory: str | None = None,
                         theme: str | None = None,
                         transfers: int = 0, limit: int = 500) -> dict:
        cond, params = _viz_where(date_from, date_to, bool(transfers))
        if theme:
            cond += f" AND {themes_mod.MATCH}"; params.append(theme)
        if category:
            cond += f" AND {_ECAT} = ?"; params.append(category)
        if subcategory:
            if subcategory == "(no subcategory)":
                cond += " AND ml.resolved_subcategory IS NULL"
            else:
                cond += " AND ml.resolved_subcategory = ?"
                params.append(subcategory)
        con = db()
        try:
            rows = con.execute(
                f"""SELECT t.date, t.amount_cents, t.description,
                       t.merchant_key, {_ECAT} AS category,
                       ml.resolved_subcategory AS subcategory,
                       COALESCE(ml.resolved_name, m.display_name,
                                t.merchant_name, t.description) AS merchant
                    {_VIZ_JOIN}
                    WHERE {cond} AND t.direction='debit'
                    ORDER BY ABS(t.amount_cents) DESC, t.date DESC
                    LIMIT ?""", [*params, max(1, min(limit, 1000))]).fetchall()
            data = [dict(r) for r in rows]
            return {"data": data,
                    "total_cents": sum(abs(r["amount_cents"] or 0) for r in data)}
        finally:
            con.close()

    @app.get("/static/echarts.min.js")
    def echarts_js() -> Response:
        path = Path(__file__).parent / "static" / "echarts.min.js"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="echarts not vendored")
        return Response(content=path.read_bytes(),
                        media_type="application/javascript",
                        headers={"Cache-Control": "max-age=86400"})

    @app.get("/viz", response_class=HTMLResponse)
    def viz_page() -> str:
        return PAGE_VIZ

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE

    return app


def build_app() -> FastAPI:
    """App factory — used directly by main() and by uvicorn's reloader."""
    cfg = Config.load()
    from .config import REPO_ROOT, _parse_env_file

    # Environment wins, .env is the durable home — same precedence as every
    # other setting. (Reading os.environ alone was a bug: start.sh doesn't
    # export .env, so a secret set there was silently ignored and a random
    # one minted instead.)
    secret = os.environ.get("SPENDGLASS_RECOVERY_SECRET") or _parse_env_file(
        REPO_ROOT / ".env").get("SPENDGLASS_RECOVERY_SECRET")
    auth = Auth(
        auth_file=cfg.db_path.parent / "ui_auth.json",
        recovery_secret=secret,
    )
    print("─" * 64, flush=True)
    if auth.first_run:
        print(f"First run: open http://127.0.0.1:{PORT} and set a password.")
    else:
        print(f"Open http://127.0.0.1:{PORT} and log in with your password.")
    print(f"RECOVERY SECRET (first-run setup / password reset): {auth.recovery_secret}", flush=True)
    print("─" * 64, flush=True)

    def live_propagator(confirmed: list[dict]) -> None:
        # Best-effort: a confirmed identity often informs other pendings
        # (same brand/platform/pattern). Failures are silent — this only
        # ever improves proposals, never blocks a decision.
        try:
            import anthropic

            key = os.environ.get("ANTHROPIC_API_KEY") or _parse_env_file(
                REPO_ROOT / ".env").get("ANTHROPIC_API_KEY")
            if not key:
                return
            with Store(cfg.db_path) as s:
                propagate_decisions(s, anthropic.Anthropic(api_key=key), confirmed)
        except Exception:
            pass

    # Scheduled freshness lives with the server, not the OS — portable for
    # anyone who clones this. Tests use create_app directly, so stay inert.
    from . import autosync
    autosync.start(cfg.db_path)

    # Same principle for restore points: snapshot at startup and on a timer.
    from . import backup as backup_mod
    backup_mod.start(cfg.db_path, cfg.backup_interval_hours,
                     cfg.backup_keep, cfg.backup_mirror_dir)

    return create_app(cfg.db_path, auth, propagator=live_propagator,
                      backup_interval_hours=cfg.backup_interval_hours)


def main() -> None:
    import uvicorn

    if os.environ.get("SPENDGLASS_DEV"):
        # Dev mode: watch the source tree and restart on save. Sessions
        # persist on disk, so a reload is invisible beyond a page refresh.
        uvicorn.run("spendglass.ui:build_app", factory=True,
                    host="127.0.0.1", port=PORT, reload=True,
                    log_level="warning")
    else:
        uvicorn.run(build_app(), host="127.0.0.1", port=PORT, log_level="warning")


# ── the page ────────────────────────────────────────────────────────────────
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>spendglass</title>
<style>
/* Colour themes — five named palettes, one source of truth.
   :root = Zinc (dark-first, the default); [data-theme] flips the rest.
   Money semantics (--credit/--debit) and the warn pair are re-stated per
   theme: an amount or a staleness banner that goes low-contrast on one
   palette is a bug, not a taste question. */
:root{--bg:#14161a;--card:#1c1f26;--card2:#23272f;--line:#2c313c;
      --fg:#d8dde6;--muted:#8b93a2;--faint:#6b7280;
      --accent:#6ea8fe;--accent-ink:#0b1220;--accent-soft:#1e2a3d;--ok:#4cc38a;
      --warn-bg:#3a2a12;--warn-fg:#e5c07b;--err:#e06c75;
      --credit:#4cc38a;--debit:#d8dde6}
[data-theme="midnight"]{--bg:#0b1120;--card:#121a2b;--card2:#1c2740;
      --line:#2a3a55;--fg:#e8ecf6;--muted:#9aa7c2;--faint:#6e7c9c;
      --accent:#6ea8fe;--accent-ink:#0b1120;--accent-soft:#17233c;--warn-bg:#33280f;
      --credit:#4cc38a;--debit:#e8ecf6}
[data-theme="espresso"]{--bg:#141110;--card:#1b1815;--card2:#282218;
      --line:#3a3226;--fg:#f1ece4;--muted:#b3a895;--faint:#837868;
      --accent:#d19a66;--accent-ink:#141110;--accent-soft:#2e2318;--warn-bg:#3a2a12;
      --credit:#7cc99e;--debit:#f1ece4}
[data-theme="oled"]{--bg:#000;--card:#0d0d0f;--card2:#1a1a1d;--line:#26262b;
      --fg:#f5f5f7;--muted:#a0a0a8;--faint:#707078;
      --accent:#6ea8fe;--accent-ink:#000;--accent-soft:#14202f;--warn-bg:#2e2208;
      --credit:#4cc38a;--debit:#f5f5f7}
[data-theme="light"]{--bg:#fff;--card:#f6f6f8;--card2:#ececf0;--line:#d5d5dc;
      --fg:#101012;--muted:#4a4a52;--faint:#7c7c87;
      --accent:#2563eb;--accent-ink:#fff;--accent-soft:#e6edfd;--ok:#15803d;
      --warn-bg:#fdecea;--warn-fg:#8c1d10;--err:#b91c1c;
      --credit:#15803d;--debit:#101012}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:var(--bg);color:var(--fg)}
.wrap{margin:0 auto;padding:20px}
h1{font-size:20px;margin:0}
h1 .bark{color:var(--accent)}
.sub{color:var(--muted);font-size:13px;margin:2px 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px;margin-bottom:14px}
.center{max-width:420px;margin:12vh auto}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:7px;
      background:var(--bg);color:var(--fg);font:inherit}
button{margin-top:14px;padding:9px 18px;border:0;border-radius:7px;background:var(--accent);
       color:var(--accent-ink);font:inherit;font-weight:600;cursor:pointer}
button.link{background:none;color:var(--muted);font-weight:400;padding:4px;font-size:13px}
.err{color:var(--err);font-size:13px;margin-top:8px;min-height:18px}
.banner{display:flex;gap:18px;flex-wrap:wrap;align-items:center;font-size:13px}
.banner .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px}
.banner .warnrow{width:100%;background:var(--warn-bg);color:var(--warn-fg);
       padding:9px 12px;border-radius:7px;font-weight:600}
.filters{display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr 1fr 1fr;gap:8px}
.filters input,.filters select{font-size:13px}
@media(max-width:900px){.filters{grid-template-columns:1fr 1fr}}
table{border-collapse:collapse;font-size:13.5px;table-layout:fixed;min-width:100%}
th,td{padding:7px 9px;text-align:left;border-bottom:1px solid var(--line);
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
table.wraptext td{white-space:normal;overflow-wrap:anywhere}
th{cursor:pointer;user-select:none;color:var(--muted);font-size:12px;
   text-transform:uppercase;letter-spacing:.04em;position:relative}
th .rz{position:absolute;top:0;right:0;width:9px;height:100%;cursor:col-resize}
th .rz::after{content:"";position:absolute;right:4px;top:18%;height:64%;
   border-right:1px solid var(--line)}
th .rz:hover,th .rz.on{background:var(--accent-soft)}
th .rz:hover::after,th .rz.on::after{border-color:var(--accent)}
.colsbar{display:flex;justify-content:flex-end;gap:16px;align-items:center;
   font-size:12.5px;color:var(--muted);margin-bottom:8px;position:relative}
.colsbar label{display:inline-flex;gap:5px;align-items:center;font-size:12.5px;
   margin:0;cursor:pointer}
.colsbar input[type=checkbox]{width:auto}
.colpop{position:absolute;right:0;top:24px;background:var(--card2);
   border:1px solid var(--line);border-radius:8px;padding:10px 14px;z-index:5;
   box-shadow:0 6px 24px rgba(0,0,0,.35)}
.colpop label{display:flex;gap:7px;align-items:center;margin:4px 0;
   color:var(--fg);font-size:13px;cursor:pointer}
.colpop input[type=checkbox]{width:auto}
td.copytd{padding:7px 2px 7px 6px}
.prov{margin-left:6px;font-size:14px;font-weight:700;user-select:none;
   display:inline-block;transform:translateY(1px)}
.prov-auto{color:var(--accent)}
.prov-human{color:var(--ok)}
.prov-pending{color:var(--warn-fg);cursor:pointer;background:var(--warn-bg);
   border-radius:99px;padding:0 7px}
.copy{background:none;border:0;color:var(--muted);cursor:pointer;padding:0;
   margin:0;font:inherit;font-size:13px;opacity:0;transition:opacity .12s}
tr:hover .copy{opacity:1}
.copy:hover{color:var(--accent)}
.rvbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.rvbar input,.rvbar select{width:auto;font-size:13px}
.rv-actions{margin-left:auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
#rvtbl{table-layout:fixed;width:100%}
#rvtbl th,#rvtbl td{padding:7px 5px}
#rv-head th.active::after{content:" ▾"}
#rv-head th.active.asc::after{content:" ▴"}
.rv-sum{color:var(--faint);font-size:12px;display:-webkit-box;-webkit-line-clamp:2;
   -webkit-box-orient:vertical;overflow:hidden}
#rvtbl input[type=text]{font-size:13px;padding:4px 7px;width:100%}
.rv-approve,.rv-reject{padding:2px 4px}
#rvtbl input[type=checkbox]{width:auto}
#rvtbl td{white-space:normal}
.adgrid{display:grid;grid-template-columns:minmax(260px,420px) 1fr;gap:28px}
.adgrid h3{font-size:13px;text-transform:uppercase;letter-spacing:.04em;
   color:var(--muted);margin:0 0 6px}
.adchk{display:flex;gap:7px;align-items:center;cursor:pointer}
.adchk input{width:auto}
.adnew{display:flex;gap:8px;align-items:center;margin:10px 0;flex-wrap:wrap}
.adnew select,.adnew input{width:auto;flex:0 1 220px}
.thadd{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.thadd select{width:auto}
.thadd input{flex:1 1 260px}
.thchips{margin-top:4px;display:flex;gap:6px;flex-wrap:wrap}
.miner-row{display:flex;justify-content:space-between;align-items:center;gap:12px;
   padding:9px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.miner-row .st{color:var(--faint);font-size:12px;overflow-wrap:anywhere}
.capform{display:flex;gap:8px;align-items:center;margin:7px 0 2px;flex-wrap:wrap}
.capform input{flex:1;min-width:220px;font-size:13px;padding:6px 9px}
@media(max-width:900px){.adgrid{grid-template-columns:1fr}}
td.copytd{white-space:nowrap}
.toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,14px);
   background:var(--card2);border:1px solid var(--ok);color:var(--fg);
   padding:10px 16px;border-radius:9px;font-size:13.5px;z-index:60;
   opacity:0;pointer-events:none;transition:all .18s;max-width:560px}
.toast.bad{border-color:var(--err)}
.toast.on{opacity:1;transform:translate(-50%,0)}
td.editable{cursor:text}
td.editable:hover{box-shadow:inset 0 0 0 1px var(--line)}
input.celledit{width:100%;padding:3px 6px;font-size:13px;border:1px solid var(--accent);
   border-radius:5px;background:var(--bg);color:var(--fg)}
.sglist{position:fixed;z-index:50;background:var(--card2);border:1px solid var(--line);
   border-radius:8px;overflow-y:auto;overscroll-behavior:contain;
   scrollbar-width:thin;scrollbar-color:var(--line) transparent;
   box-shadow:0 8px 28px rgba(0,0,0,.4)}
/* the scrollbar IS the "there's more" affordance — keep it visible */
.sglist::-webkit-scrollbar{width:10px}
.sglist::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}
.sglist::-webkit-scrollbar-track{background:transparent}
.sglist div{padding:6px 11px;cursor:pointer;font-size:13px;color:var(--fg);
   white-space:nowrap}
.sglist div:hover,.sglist div.on{background:var(--accent-soft);color:var(--accent)}
.rv-approve{color:var(--ok);background:none;border:0;cursor:pointer;font:inherit;
   font-weight:600;padding:2px 6px}
.rv-reject{color:var(--err);background:none;border:0;cursor:pointer;font:inherit;
   padding:2px 6px}
th.active{color:var(--accent)}
td.amt{text-align:right;font-variant-numeric:tabular-nums}
td.amt.credit{color:var(--credit);font-weight:600}
tr:hover td{background:var(--accent-soft)}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--accent-soft);
      color:var(--accent);font-size:12px}
.foot{display:flex;justify-content:space-between;align-items:center;margin-top:10px;
      font-size:13px;color:var(--muted)}
.foot b{color:var(--fg)}
.tablewrap{overflow-x:auto}
.theme-pick{width:auto;padding:3px 8px;font-size:12px;background:var(--card2);
      color:var(--muted);border:1px solid var(--line);border-radius:6px}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);
      white-space:nowrap}
.topright{display:flex;align-items:center;gap:8px}
.hidden{display:none}
</style></head><body>
<script>
  /* theme before first paint (no flash); "zinc" = :root default, no attribute */
  (function(){var t=localStorage.getItem("sg-theme");
   if(t&&t!=="zinc")document.documentElement.dataset.theme=t;})();
</script>
<div class="wrap">
  <div id="gate" class="center card hidden">
    <div class="topright" style="float:right"><label class="visually-hidden" for="theme-gate">Colour theme</label><select id="theme-gate" class="theme-pick" aria-label="Colour theme"><option value="zinc">Zinc</option><option value="midnight">Midnight</option><option value="espresso">Espresso</option><option value="oled">OLED</option><option value="light">Light</option></select></div>
    <h1>redbark-<span class="bark">local</span>-store</h1>
    <p class="sub" id="gate-sub"></p>
    <div id="setup-form" class="hidden">
      <p style="font-size:13px">This sets the password you'll use from now on. Prove it's
      you by pasting the <b>recovery secret</b> printed in the server terminal.</p>
      <label>Recovery secret (from the terminal)</label>
      <input id="secret" autocomplete="off">
      <label>Choose a password (min 8 characters)</label>
      <input id="new-password" type="password" autocomplete="new-password">
      <button onclick="doSetup()">Set password</button>
    </div>
    <div id="login-form" class="hidden">
      <label>Password</label>
      <input id="password" type="password" autocomplete="current-password"
             onkeydown="if(event.key==='Enter')doLogin()">
      <button onclick="doLogin()">Unlock</button>
      <button class="link" onclick="showReset()">Forgot? Reset with the recovery secret</button>
    </div>
    <div class="err" id="gate-err"></div>
  </div>

  <div id="main" class="hidden">
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <div><h1>redbark-<span class="bark">local</span>-store</h1>
      <p class="sub">Your bank transactions, stored on this machine. This page cannot move
      money, and it never edits what your bank sent. What it does change: merchant labels
      and themes, your settings, and the provider keys you save.</p></div>
      <div class="topright"><a class="link" href="/viz" style="text-decoration:none">spending</a><button class="link" onclick="toggleAdmin()">admin</button><button class="link" id="review-btn" onclick="toggleReview()">review</button><label class="visually-hidden" for="theme">Colour theme</label><select id="theme" class="theme-pick" aria-label="Colour theme"><option value="zinc">Zinc</option><option value="midnight">Midnight</option><option value="espresso">Espresso</option><option value="oled">OLED</option><option value="light">Light</option></select><button class="link" onclick="doLogout()">Lock</button></div>
    </div>
    <div class="card banner" id="banner">loading…</div>
    <div class="card hidden" id="admin">
      <div class="adgrid">
        <div>
          <h3>Miner settings</h3>
          <label>Auto-approve threshold (0–1)</label>
          <input id="ad-thr" type="number" step="0.05" min="0" max="1">
          <label>Max web searches per merchant</label>
          <input id="ad-srch" type="number" min="1" max="5">
          <label>Parallel workers</label>
          <input id="ad-wrk" type="number" min="1" max="8">
          <label class="adchk"><input type="checkbox" id="ad-prop"> learn from my decisions (refresh related pendings)</label>
          <label>Scheduled sync every N hours (0 = off)</label>
          <input id="ad-sync" type="number" step="0.5" min="0" max="168">
          <button onclick="saveAdmin()">Save settings</button>
          <span class="err" id="ad-msg"></span>
        </div>
        <div>
          <h3>Miners</h3>
          <div id="ad-miners"></div>
          <p class="sub" id="ad-data"></p>
          <h3 style="margin-top:18px">Connections</h3>
          <div id="ad-caps"></div>
          <p class="sub">Saving a key sends it to that provider once to check it
          works, then writes it to <code>.env</code> on this machine (owner-only,
          0600) if the check passes. It is never shown back here. The miners send
          it to the provider each time they run.</p>
        </div>
      </div>
      <h3 style="margin-top:18px">Spending themes</h3>
      <p class="sub">A theme is one number for something that spans categories —
      a renovation is trades + hardware + architects; pets are supplies + vet +
      insurance. Rules match a subcategory exactly, or a merchant key with a
      SQL LIKE pattern. Themes never change a transaction's category, and new
      stores start with none.</p>
      <div class="adnew">
        <select id="th-tpl" aria-label="Theme template"
          onchange="if(!$('th-name').value)$('th-name').value=this.value"></select>
        <input id="th-name" placeholder="Theme name"
          onkeydown="if(event.key==='Enter')createTheme()">
        <button class="link" onclick="createTheme()">＋ create theme</button>
        <span class="err" id="th-msg"></span>
      </div>
      <div id="ad-themes"></div>
    </div>
    <div class="card hidden" id="review">
      <div class="rvbar">
        <input id="rv-q" placeholder="Filter proposals…">
        <select id="rv-status">
          <option value="pending">Pending</option><option value="auto">Auto</option>
          <option value="approved">Approved</option><option value="overridden">Overridden</option>
          <option value="rejected">Rejected</option><option value="all">All</option>
        </select>
        <label class="tog" style="font-size:12.5px;color:var(--muted);display:inline-flex;gap:5px;align-items:center;cursor:pointer">
          <input type="checkbox" id="rv-plumbing" style="width:auto"> show transfers &amp; bank lines</label>
        <span class="rv-actions">
          <input id="rv-bulk-sub" data-suggest="subcats" autocomplete="off" placeholder="Subcategory for selected (optional)…">
          <button class="link" onclick="bulkDecide('approve')">✓ approve selected</button>
          <button class="link" onclick="bulkDecide('reject')">✕ reject selected</button>
        </span>
      </div>
      <div class="tablewrap"><table id="rvtbl">
        <thead><tr id="rv-head">
          <th style="width:30px;cursor:default"><input type="checkbox" id="rv-all" onchange="rvAll(this.checked)"></th>
          <th onclick="rvSortBy('descriptor')" data-rvs="descriptor">Descriptor</th>
          <th style="width:170px" onclick="rvSortBy('name')" data-rvs="name">Name</th>
          <th style="width:68px" onclick="rvSortBy('spend')" data-rvs="spend">Spend</th>
          <th style="width:172px" onclick="rvSortBy('category')" data-rvs="category">Category</th>
          <th style="width:150px" onclick="rvSortBy('subcategory')" data-rvs="subcategory">Subcategory</th>
          <th style="width:56px" onclick="rvSortBy('conf')" data-rvs="conf">Conf</th>
          <th style="width:68px;cursor:default">Source</th><th style="width:70px;cursor:default"></th>
        </tr></thead>
        <tbody id="rv-rows"></tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="filters">
        <input id="f-q" placeholder="Search description / merchant…">
        <select id="f-account"><option value="">All accounts</option></select>
        <select id="f-category"><option value="">All categories</option></select>
        <select id="f-theme"><option value="">All themes</option></select>
        <select id="f-direction"><option value="">In + out</option>
          <option value="debit">Money out</option><option value="credit">Money in</option></select>
        <input id="f-from" type="date" title="From date">
        <input id="f-to" type="date" title="To date">
        <select id="f-status"><option value="">Any status</option>
          <option value="posted">Posted</option><option value="pending">Pending</option></select>
      </div>
    </div>
    <div class="card">
      <div class="colsbar">
        <label><input type="checkbox" id="wrap-toggle"> wrap text</label>
        <button class="link" style="margin:0" onclick="toggleColPop(event)">columns ▾</button>
        <div id="colpop" class="colpop hidden"></div>
      </div>
      <div class="tablewrap"><table id="tbl">
        <colgroup id="cols"></colgroup>
        <thead><tr id="head"></tr></thead>
        <tbody id="rows"></tbody>
      </table></div>
      <div class="foot">
        <span id="totals"></span>
        <span>
          <button class="link" onclick="page(-1)">‹ prev</button>
          <span id="pageinfo"></span>
          <button class="link" onclick="page(1)">next ›</button>
        </span>
      </div>
    </div>
  </div>
</div>
<script>
/* [sort key, label, default px width, cell renderer] — every column can be
   hidden, resized (drag the right edge of a header), and sorted. */
const COLS=[
  ["date","Date",100,x=>`<td>${x.date??""}</td>`],
  ["description","Description",380,x=>`<td>${esc(x.description)}</td>`],
  ["merchant","Merchant",180,(x,i)=>`<td class="editable" title="${esc(x.merchant_name)} — click to correct (applies to all matching)" onclick="cellEdit(event,${i},'merchant')">${x.internal?`<span class="prov" style="color:var(--faint)" title="Matched internal transfer — both legs found in your own accounts; excluded from spend analytics">⇄ </span>`:""}${esc(x.merchant_display)}${prov(x)}</td>`],
  ["amount","Amount",110,x=>`<td class="amt ${x.direction==="credit"?"credit":""}">${money(x.amount_cents??0,x.currency)}</td>`],
  ["direction","Direction",90,x=>`<td>${x.direction==="credit"?"in":"out"}</td>`],
  ["category","Category",150,(x,i)=>`<td class="editable" title="Click to correct (applies to all matching)" onclick="cellEdit(event,${i},'category')">${x.category_display?`<span class="pill">${esc(CATLABEL[x.category_display]||x.category_display)}</span>`:""}</td>`],
  ["subcategory","Subcategory",130,(x,i)=>`<td class="editable" title="Click to correct (applies to all matching)" onclick="cellEdit(event,${i},'subcategory')">${esc(x.subcategory)}</td>`],
  ["custom_category","Custom",110,x=>`<td>${esc(x.custom_category)}</td>`],
  ["account","Account",150,x=>`<td>${esc(x.account_name)}</td>`],
  ["status","Status",90,x=>`<td>${x.status==="pending"?"⏳ pending":"posted"}</td>`],
];
const DEFAULT_HIDDEN=["direction","custom_category"];
let vis=new Set(JSON.parse(localStorage.getItem("sg-cols")||"null")
  ||COLS.map(c=>c[0]).filter(k=>!DEFAULT_HIDDEN.includes(k)));
let widths=Object.assign(Object.fromEntries(COLS.map(c=>[c[0],c[2]])),
  JSON.parse(localStorage.getItem("sg-widths")||"{}"));
const visCols=()=>COLS.filter(c=>vis.has(c[0]));
let lastData=[];
let state={sort:"date",dir:"desc",offset:0,limit:100};
const $=id=>document.getElementById(id);
const api=(p,o)=>fetch(p,o).then(r=>{if(r.status===401){showGate();throw 0}return r.json()});

async function boot(){
  const s=await fetch("/api/session").then(r=>r.json());
  if(s.authenticated){showMain()}else{showGate(s.first_run)}
}
function showGate(firstRun){
  $("main").classList.add("hidden");$("gate").classList.remove("hidden");
  const setup=firstRun===true;
  $("setup-form").classList.toggle("hidden",!setup);
  $("login-form").classList.toggle("hidden",setup);
  $("gate-sub").textContent=setup?
    "First run — create the password that protects this page.":
    "Locked. This page shows your real bank transactions.";
}
function showReset(){
  $("setup-form").classList.remove("hidden");$("login-form").classList.add("hidden");
  $("gate-sub").textContent="Reset — paste the recovery secret from the server terminal.";
}
async function doSetup(){
  const r=await fetch("/api/setup",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({recovery_secret:$("secret").value.trim(),password:$("new-password").value})});
  if(r.ok){showMain()}else{$("gate-err").textContent=(await r.json()).detail}
}
async function doLogin(){
  const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({password:$("password").value})});
  if(r.ok){showMain()}else{$("gate-err").textContent="Wrong password."}
}
async function doLogout(){await fetch("/api/logout",{method:"POST"});location.reload()}

async function showMain(){
  $("gate").classList.add("hidden");$("main").classList.remove("hidden");
  const [health,accounts,categories]=await Promise.all(
    [api("/api/health"),api("/api/accounts"),api("/api/categories")]);
  renderBanner(health);
  accounts.sort((a,b)=>(a.name??"").localeCompare(b.name??""));
  for(const a of accounts){const o=new Option(`${a.name} (${a.institution_name??""})`,a.id);
    $("f-account").add(o)}
  categories.sort((a,b)=>(a.label??"").localeCompare(b.label??""));
  for(const c of categories){$("f-category").add(new Option(c.label,c.key));
    CATLABEL[c.key]=c.label;CATKEY[c.label]=c.key}
  // Bank CDR enum keys stay internal; humans see the labels.
  SUGGEST.cats=categories.map(c=>c.label);
  renderHead();load();
  refreshReviewBtn();  // also seeds SUBTREE for the inline editor
  api("/api/themes?months=1").then(t=>{
    for(const th of t.definitions??[])$("f-theme").add(new Option(th.name,th.name));
  }).catch(()=>{});
}
function renderBanner(h){
  const b=$("banner");b.innerHTML="";
  const last=h.last_sync?new Date(h.last_sync.finished_at):null;
  const ageH=last?(Date.now()-last)/36e5:Infinity;
  const stale=h.stale||ageH>26;
  b.insertAdjacentHTML("beforeend",
    `<span><span class="dot" style="background:${stale?"var(--warn-fg)":"var(--ok)"}"></span>
     <b>${stale?"STALE":"Fresh"}</b> — last sync ${last?last.toLocaleString():"never"}</span>
     <span>${h.counts.transactions.toLocaleString()} transactions ·
     ${h.counts.accounts} accounts</span>`);
  const bk=h.last_backup?new Date(h.last_backup):null;
  b.insertAdjacentHTML("beforeend",
    `<span>last backup ${bk?bk.toLocaleString():"never"}</span>`);
  if(h.backup_interval_hours>0&&(bk?(Date.now()-bk)/36e5:Infinity)>2*h.backup_interval_hours)
    b.insertAdjacentHTML("beforeend",
      `<span class="warnrow">⚠ Backups have stalled — newest snapshot
       ${bk?bk.toLocaleString():"none yet"}; check <code>data/backups/</code></span>`);
  for(const c of h.connections){
    b.insertAdjacentHTML("beforeend",
      `<span>${c.institution??"connection"}: <b>${c.status}</b>
       (consent est. until ${c.estimated_consent_deadline??"?"})</span>`);
    for(const w of c.warnings){
      b.insertAdjacentHTML("beforeend",`<span class="warnrow">⚠ ${c.institution}: ${w}</span>`)}
  }
  if(stale)b.insertAdjacentHTML("beforeend",
    `<span class="warnrow">⚠ Data may be out of date — run
     <code>.venv/bin/python -m spendglass.sync</code></span>`);
}
function renderHead(){
  $("head").innerHTML=`<th style="cursor:default;width:48px"></th>`+
    visCols().map(([k,label,,,sortable])=>
    `<th class="${state.sort===k?"active":""}"
      ${sortable===false?"":`onclick="sortBy('${k}')"`}>${label}
     ${state.sort===k?(state.dir==="desc"?"▾":"▴"):""}
     <span class="rz" onmousedown="startRz(event,'${k}')" onclick="event.stopPropagation()"></span></th>`).join("");
  applyWidths();
}
function applyWidths(){
  const cs=visCols();
  $("cols").innerHTML=`<col style="width:48px">`+
    cs.map(c=>`<col style="width:${widths[c[0]]}px">`).join("");
  $("tbl").style.width=(48+cs.reduce((t,c)=>t+widths[c[0]],0))+"px";
}
/* copy a whole row (visible columns, tab-separated); field selection still
   works normally — the button never interferes with text selection */
function plainVal(x,k){
  return {date:x.date??"",description:x.description??"",merchant:x.merchant_display??"",
    amount:money(x.amount_cents??0,x.currency),direction:x.direction??"",
    category:x.category_display??"",subcategory:x.subcategory??"",
    custom_category:x.custom_category??"",account:x.account_name??"",
    status:x.status??""}[k];
}
async function copyRow(i,btn){
  const text=visCols().map(c=>plainVal(lastData[i],c[0])).join("\t");
  try{await navigator.clipboard.writeText(text)}catch(e){return}
  btn.textContent="✓";setTimeout(()=>{btn.textContent="⧉"},900);
}
/* drag the right edge of a header to resize; widths persist */
let rz=null;
function startRz(e,k){
  e.preventDefault();e.stopPropagation();
  rz={k,x:e.clientX,w:widths[k],el:e.target};rz.el.classList.add("on");
  document.addEventListener("mousemove",moveRz);
  document.addEventListener("mouseup",endRz);
}
function moveRz(e){if(rz){widths[rz.k]=Math.max(56,rz.w+e.clientX-rz.x);applyWidths()}}
function endRz(){
  if(!rz)return;
  rz.el.classList.remove("on");
  localStorage.setItem("sg-widths",JSON.stringify(widths));rz=null;
  document.removeEventListener("mousemove",moveRz);
  document.removeEventListener("mouseup",endRz);
}
/* column visibility popover; at least one column stays on */
function toggleColPop(e){e.stopPropagation();$("colpop").classList.toggle("hidden");renderColPop()}
function renderColPop(){
  $("colpop").innerHTML=COLS.map(([k,label])=>
    `<label><input type="checkbox" ${vis.has(k)?"checked":""}
      onchange="toggleCol('${k}',this.checked)"> ${label}</label>`).join("");
}
function toggleCol(k,on){
  if(on)vis.add(k);else if(vis.size>1)vis.delete(k);else{renderColPop();return}
  localStorage.setItem("sg-cols",JSON.stringify([...vis]));
  renderHead();renderRows();
}
document.addEventListener("click",e=>{
  if(!$("colpop").classList.contains("hidden")&&!e.target.closest(".colsbar"))
    $("colpop").classList.add("hidden");
});
function sortBy(k){
  if(state.sort===k){state.dir=state.dir==="desc"?"asc":"desc"}
  else{state.sort=k;state.dir="desc"}
  state.offset=0;renderHead();load();
}
function page(d){state.offset=Math.max(0,state.offset+d*state.limit);load()}
function filters(){
  return{q:$("f-q").value,account:$("f-account").value,category:$("f-category").value,
    direction:$("f-direction").value,status:$("f-status").value,
    date_from:$("f-from").value,date_to:$("f-to").value,theme:$("f-theme").value};
}
let t;for(const id of["f-q","f-account","f-category","f-direction","f-status","f-from","f-to","f-theme"]){
  document.addEventListener("input",e=>{if(e.target.id===id){clearTimeout(t);
    t=setTimeout(()=>{state.offset=0;load()},250)}})}
/* provenance decorator: how the merchant label was established */
function prov(x){
  const p=x.merchant_prov;
  if(p==="auto")return `<span class="prov prov-auto" title="Attributed by the lookup agent">✦</span>`;
  if(p==="approved"||p==="overridden")return `<span class="prov prov-human" title="Confirmed by you">✓</span>`;
  if(p==="pending")return `<span class="prov prov-pending" title="Awaiting your review — click to open"
    onclick="event.stopPropagation();if($('review').classList.contains('hidden'))toggleReview()">?</span>`;
  return "";
}
const money=(cents,cur)=>((cents<0?"-":"")+"$"+(Math.abs(cents)/100)
  .toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})
  +(cur&&cur!=="AUD"?" "+cur:""));
async function load(){
  const p=new URLSearchParams({sort:state.sort,dir:state.dir,limit:state.limit,
    offset:state.offset});
  for(const[k,v]of Object.entries(filters()))if(v)p.set(k,v);
  const r=await api("/api/transactions?"+p);
  lastData=r.data;renderRows();
  $("totals").innerHTML=`<b>${r.total.toLocaleString()}</b> transactions ·
    filtered net <b>${money(r.sum_cents)}</b>`;
  $("pageinfo").textContent=`${r.offset+1}–${Math.min(r.offset+r.limit,r.total)} of ${r.total}`;
}
function renderRows(){
  $("rows").innerHTML=lastData.map((x,i)=>
    `<tr><td class="copytd"><button class="copy" title="Copy row"
       onclick="copyRow(${i},this)">⧉</button></td>${
       visCols().map(c=>c[3](x,i)).join("")}</tr>`).join("");
}
/* ── per-cell inline correction: keyed to the merchant, marks all matching.
     Enter or clicking away saves a changed value; Esc cancels. ── */
function cellEdit(e,i,field){
  if(e.target.closest(".prov")||e.target.closest("input"))return;
  const td=e.currentTarget,x=lastData[i];
  if(td.querySelector("input"))return;
  sgHide();
  const cur=field==="merchant"?(x.merchant_display||"")
    :field==="category"?(CATLABEL[x.category_display]||x.category_display||"")
    :(x.subcategory||"");
  const sug=field==="category"?` data-suggest="cats" autocomplete="off"`
    :field==="subcategory"?` data-suggest="subcats" autocomplete="off" data-cat="${esc(x.category_display||"")}"`:"";
  td.innerHTML=`<input class="celledit" value="${esc(cur)}"${sug}>`;
  const inp=td.querySelector("input");
  inp.focus();inp.select();
  let done=false;
  const finish=save=>{
    if(done)return;done=true;sgHide();
    if(save)saveCell(i,field,inp.value.trim());else renderRows();
  };
  inp.addEventListener("keydown",ev=>{
    if(ev.key==="Enter"){ev.preventDefault();finish(true)}
    else if(ev.key==="Escape")finish(false);
  });
  // Clicking away commits a changed value (spreadsheet convention);
  // an unchanged or emptied field just closes.
  inp.addEventListener("blur",()=>setTimeout(()=>{
    const v=inp.value.trim();
    finish(v!==""&&v!==cur.trim());
  },160));
}
async function saveCell(i,field,v){
  const x=lastData[i];
  const d={merchant_key:x.merchant_key,action:"approve",
    name:null,category:null,subcategory:null};
  if(field==="merchant"){
    if(!v||v===x.merchant_display){renderRows();return}
    d.name=v;
  }else if(field==="category"){
    const key=CATKEY[v]||(CATLABEL[v]!==undefined?v:null);
    if(!key||key===x.category_display){renderRows();return}
    d.category=key;
  }else{
    if(!v||v===(x.subcategory||"")){renderRows();return}
    d.subcategory=v;
    if(SUBTREE[v]&&SUBTREE[v]!==x.category_display)d.category=SUBTREE[v];
  }
  const resp=await fetch("/api/lookups/decide",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({decisions:[d]})});
  const j=await resp.json().catch(()=>({}));
  if(!resp.ok){decideError(j.detail);renderRows();return}
  toast("Saved — applies to every matching transaction",true);
  // In-place: patch every row of this merchant, no reload, no scroll jump.
  for(const r of lastData)if(r.merchant_key===x.merchant_key){
    if(d.name)r.merchant_display=d.name;
    if(d.category)r.category_display=d.category;
    if(d.subcategory)r.subcategory=d.subcategory;
    r.merchant_prov="overridden";
  }
  renderRows();refreshReviewBtn();
}
function refreshReviewBtn(){
  api("/api/lookups?status=pending").then(r=>{
    $("review-btn").textContent=`review (${r.counts.pending??0})`;
    SUGGEST.subcats=(r.subcategories||[]).slice()
      .sort((a,b)=>a.name.localeCompare(b.name,undefined,{sensitivity:"base"}));
    SUBTREE=Object.fromEntries(SUGGEST.subcats.map(s=>[s.name,s.category]));
  }).catch(()=>{});
}
/* ── merchant-identity review queue ── */
let rvData=[];
function toggleReview(){
  $("review").classList.toggle("hidden");
  if(!$("review").classList.contains("hidden"))loadReview();
}
async function loadReview(){
  const p=new URLSearchParams({status:$("rv-status").value,
    plumbing:$("rv-plumbing").checked?1:0});
  const q=$("rv-q").value.trim();if(q)p.set("q",q);
  const r=await api("/api/lookups?"+p);
  rvData=r.data;
  $("review-btn").textContent=`review (${r.counts.pending??0})`;
  SUGGEST.subcats=(r.subcategories||[])
    .slice().sort((a,b)=>a.name.localeCompare(b.name,undefined,{sensitivity:"base"}));
  SUBTREE=Object.fromEntries(SUGGEST.subcats.map(s=>[s.name,s.category]));
  renderRvRows();
}
/* client-side sorting — the whole filtered queue is already in memory */
let rvSort={key:"spend",dir:"desc"};
const RVVAL={
  descriptor:x=>(x.display_name||x.merchant_key||"").toLowerCase(),
  name:x=>(x.resolved_name||x.proposed_name||"").toLowerCase(),
  spend:x=>Math.abs(x.total_debit_cents??0),
  category:x=>(x.resolved_category||x.category||"").toLowerCase(),
  subcategory:x=>(x.resolved_subcategory||x.proposed_subcategory||"").toLowerCase(),
  conf:x=>x.confidence??-1,
};
function rvSortBy(k){
  if(rvSort.key===k){rvSort.dir=rvSort.dir==="desc"?"asc":"desc"}
  else{rvSort={key:k,dir:k==="spend"||k==="conf"?"desc":"asc"}}
  renderRvRows();
}
function renderRvRows(){
  const f=RVVAL[rvSort.key],s=rvSort.dir==="asc"?1:-1;
  rvData.sort((a,b)=>{const va=f(a),vb=f(b);
    return (va<vb?-1:va>vb?1:0)*s});
  for(const th of document.querySelectorAll("#rv-head th[data-rvs]")){
    th.classList.toggle("active",th.dataset.rvs===rvSort.key);
    th.classList.toggle("asc",th.dataset.rvs===rvSort.key&&rvSort.dir==="asc");
  }
  $("rv-all").checked=false;
  $("rv-rows").innerHTML=rvData.map((x,i)=>{
    const spend=Math.abs(x.total_debit_cents??0)/100;
    return `<tr>
    <td><input type="checkbox" class="rv-pick" data-i="${i}"></td>
    <td title="${esc(x.merchant_key)}">${esc(x.display_name||x.merchant_key)}
      ${x.status!=="pending"?`<span class="pill">${esc(x.status)}</span>`:""}
      <div class="rv-sum" title="${esc(x.proposed_summary)}">${esc(x.proposed_summary)}</div></td>
    <td><input type="text" id="rv-name-${i}" value="${esc(x.resolved_name||x.proposed_name)}"></td>
    <td class="amt">${"$"+spend.toLocaleString(undefined,{maximumFractionDigits:0})}</td>
    <td><input type="text" data-suggest="cats" autocomplete="off" id="rv-cat-${i}" value="${esc(CATLABEL[x.resolved_category||x.category]||x.resolved_category||x.category)}"></td>
    <td><input type="text" data-suggest="subcats" autocomplete="off" id="rv-sub-${i}" value="${esc(x.resolved_subcategory||x.proposed_subcategory)}"></td>
    <td>${x.confidence==null?"":x.confidence.toFixed(2)}</td>
    <td>${x.evidence_url?`<a href="${esc(x.evidence_url)}" target="_blank" rel="noreferrer noopener" style="color:var(--accent)">source</a>`:""}</td>
    <td style="white-space:nowrap">
      <button class="rv-approve" title="Approve (with any edits)" onclick="decideOne(${i},'approve')">✓</button>
      <button class="rv-reject" title="Reject" onclick="decideOne(${i},'reject')">✕</button></td>
  </tr>`}).join("");
}
function rvDecision(i,action,bulkSub){
  const x=rvData[i];
  const name=$(`rv-name-${i}`).value.trim();
  const sub=(bulkSub||$(`rv-sub-${i}`).value).trim();
  let cat=$(`rv-cat-${i}`).value.trim();
  cat=CATKEY[cat]||cat;  // inputs display labels; the API speaks keys
  /* strict tree: a known subcategory implies its parent category */
  if(sub&&SUBTREE[sub])cat=SUBTREE[sub];
  return {merchant_key:x.merchant_key,action,
    name:name&&name!==x.proposed_name?name:null,
    subcategory:sub&&sub!==x.proposed_subcategory?sub:null,
    category:cat&&cat!==x.category?cat:null};
}
function toast(msg,ok){
  let t=$("toast");
  if(!t){t=document.createElement("div");t.id="toast";t.className="toast";
    document.body.appendChild(t)}
  t.textContent=msg;t.classList.toggle("bad",!ok);t.classList.add("on");
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("on"),4200);
}
function decideError(detail){
  let msg=detail||"couldn't save the decision";
  if(/unknown category/.test(msg))
    msg+=" — the bank's 16 categories are fixed; put it in a subcategory "
        +"instead (e.g. Services → Website Hosting).";
  toast(msg,false);
}
async function sendDecisions(list){
  if(!list.length)return;
  const resp=await fetch("/api/lookups/decide",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({decisions:list})});
  const j=await resp.json().catch(()=>({}));
  if(resp.status===401){showGate();return}
  if(!resp.ok){decideError(j.detail);return}
  toast("Saved — applies to every matching transaction",true);
  // In-place: decided rows leave the current status view (or update, on
  // "all"); no full reload, no scroll jump. The table refreshes quietly.
  const keys=new Set(list.map(d=>d.merchant_key));
  if($("rv-status").value==="all"){
    for(const x of rvData)if(keys.has(x.merchant_key)){
      const d=list.find(d=>d.merchant_key===x.merchant_key);
      x.status=d.action==="reject"?"rejected":"overridden";
      if(d.name)x.resolved_name=d.name;
      if(d.category)x.resolved_category=d.category;
      if(d.subcategory)x.resolved_subcategory=d.subcategory;
    }
  }else rvData=rvData.filter(x=>!keys.has(x.merchant_key));
  renderRvRows();refreshReviewBtn();load();
}
function decideOne(i,action){sendDecisions([rvDecision(i,action)])}
function bulkDecide(action){
  const bulkSub=$("rv-bulk-sub").value.trim();
  const picks=[...document.querySelectorAll(".rv-pick:checked")]
    .map(cb=>rvDecision(+cb.dataset.i,action,bulkSub||null));
  sendDecisions(picks);
}
function rvAll(on){for(const cb of document.querySelectorAll(".rv-pick"))cb.checked=on}
/* consistency rule: leaving a row with changed values commits them.
   Fires only when focus leaves the ROW (tabbing between its fields is
   fine); unchanged rows are untouched. */
let rvSaving=false;
document.addEventListener("focusout",e=>{
  const m=e.target.id&&e.target.id.match(/^rv-(name|cat|sub)-(\d+)$/);
  if(!m)return;
  setTimeout(()=>{
    if(rvSaving)return;
    const a=document.activeElement;
    const am=a&&a.id&&a.id.match(/^rv-(name|cat|sub)-(\d+)$/);
    if(am&&am[2]===m[2])return;         // still editing the same row
    const i=+m[2];
    if(!rvData[i]||!$(`rv-name-${i}`))return;  // row re-rendered meanwhile
    const d=rvDecision(i,"approve");
    if(d.name||d.category||d.subcategory){
      rvSaving=true;
      sendDecisions([d]).finally(()=>{rvSaving=false});
    }
  },180);
});
/* custom type-ahead — native <datalist> is unreliable in Safari */
const SUGGEST={subcats:[],cats:[]};
let SUBTREE={};   // subcategory name -> parent category KEY (the strict tree)
const CATLABEL={},CATKEY={};  // CDR key <-> friendly label
const sg={box:null,input:null,idx:-1,items:[]};
function sgHide(){if(sg.box){sg.box.remove();sg.box=null;sg.input=null;sg.idx=-1;sg.items=[]}}
function sgSource(input){
  if(input.dataset.suggest!=="subcats")return SUGGEST[input.dataset.suggest]||[];
  /* strict tree: a row's subcategory suggestions are scoped to its category
     (key from data-cat, or the sibling category input which shows a label) */
  let cat=input.dataset.cat||"";
  if(!cat){
    const m=input.id.match(/^rv-sub-(\w+)$/);
    const sib=m&&$(`rv-cat-${m[1]}`);
    const raw=sib?(sib.value||"").trim():"";
    cat=CATKEY[raw]||raw;
  }
  if(cat&&!CATLABEL[cat])cat="";  // unresolvable text -> show everything
  const pool=cat?SUGGEST.subcats.filter(s=>s.category===cat||s.category==null)
                :SUGGEST.subcats;
  return pool.map(s=>s.name);
}
function sgShow(input){
  const q=input.value.trim().toLowerCase();
  // Every match, prefix hits first — the list scrolls, so no cap: a value
  // you can't see in a picker may as well not exist.
  sg.items=sgSource(input).filter(s=>s.toLowerCase().includes(q))
    .sort((a,b)=>b.toLowerCase().startsWith(q)-a.toLowerCase().startsWith(q));
  if(!sg.items.length){sgHide();return}
  if(!sg.box){sg.box=document.createElement("div");sg.box.className="sglist";
    document.body.appendChild(sg.box)}
  sg.input=input;sg.idx=-1;
  const r=input.getBoundingClientRect();
  // Fill the space the screen actually has; flip above the input when the
  // bottom is tight. A clipped list with an invisible scrollbar reads as
  // "that's all of them" — never allow that.
  const below=window.innerHeight-r.bottom-12,above=r.top-12;
  const flip=below<220&&above>below;
  sg.box.style.maxHeight=Math.max(160,Math.min(440,flip?above:below))+"px";
  sg.box.style.left=r.left+"px";
  if(flip){sg.box.style.top="";sg.box.style.bottom=(window.innerHeight-r.top+2)+"px"}
  else{sg.box.style.bottom="";sg.box.style.top=(r.bottom+2)+"px"}
  sg.box.style.minWidth=r.width+"px";
  sg.box.innerHTML=sg.items.map((s,i)=>`<div data-i="${i}">${esc(s)}</div>`).join("");
}
function sgPick(i){
  if(sg.input&&sg.items[i]!=null){
    sg.input.value=sg.items[i];
    /* picking a subcategory fills in its parent category (shown as label) */
    const m=sg.input.id&&sg.input.id.match(/^rv-sub-(\w+)$/);
    const parent=SUBTREE[sg.items[i]];
    const sib=m&&$(`rv-cat-${m[1]}`);
    if(sib&&parent)sib.value=CATLABEL[parent]||parent;
  }
  sgHide();
}
document.addEventListener("input",e=>{if(e.target.dataset&&e.target.dataset.suggest)sgShow(e.target)});
document.addEventListener("focusin",e=>{if(e.target.dataset&&e.target.dataset.suggest)sgShow(e.target)});
document.addEventListener("mousedown",e=>{
  const it=e.target.closest&&e.target.closest(".sglist div");
  if(it){e.preventDefault();sgPick(+it.dataset.i)}
  else if(!(e.target.dataset&&e.target.dataset.suggest))sgHide();
});
document.addEventListener("keydown",e=>{
  if(!sg.box||e.target!==sg.input)return;
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){
    e.preventDefault();
    sg.idx=(sg.idx+(e.key==="ArrowDown"?1:-1)+sg.items.length)%sg.items.length;
    [...sg.box.children].forEach((el,i)=>el.classList.toggle("on",i===sg.idx));
    sg.box.children[sg.idx].scrollIntoView({block:"nearest"});
  }else if(e.key==="Enter"&&sg.idx>=0){e.preventDefault();sgPick(sg.idx)}
  else if(e.key==="Escape"){sgHide()}
});
// Page scroll un-anchors the fixed box, so close it — but the box's OWN
// scroll must not (capture phase sees both; that bug ate the scroll).
document.addEventListener("scroll",e=>{
  if(!sg.box||!sg.box.contains(e.target))sgHide();
},true);
/* ── admin panel ── */
const MINERS=[
 ["sync","Bank sync + enrich","Pulls fresh data from the bank, then re-derives merchants, recurring, transfer links. Also runs on the schedule set above.",false],
 ["lookup","Merchant lookup agent","Web-searches unclear merchants; auto-applies above the threshold",true],
 ["sweep","Identity sweep","Haiku batch over every merchant not yet covered — name + subcategory, no web search (cheap)",false],
 ["classify","Category classifier","Haiku batch over unclassified merchants (cheap)",false],
 ["enrich","Deterministic enrich","Rebuild merchant aggregates + recurring detection (free)",false],
];
function toggleAdmin(){$("admin").classList.toggle("hidden");
  if(!$("admin").classList.contains("hidden")){loadAdmin();loadThemes()}}
async function loadAdmin(){
  const r=await api("/api/admin");
  $("ad-thr").value=r.settings.auto_threshold;
  $("ad-srch").value=r.settings.max_searches;
  $("ad-wrk").value=r.settings.workers;
  $("ad-prop").checked=!!r.settings.propagation;
  $("ad-sync").value=r.settings.sync_interval_hours;
  $("ad-data").textContent=`Data: ${r.data.lo} → ${r.data.hi} · ${(r.data.n??0).toLocaleString()} transactions`;
  $("ad-miners").innerHTML=MINERS.map(([k,label,desc,hasLimit])=>{
    const st=r.miners[k];
    let s="never run";
    if(st){s=st.state==="running"?`⏳ running since ${st.started_at}`:
      st.state==="error"?`✕ ${esc(st.error)}`:
      `✓ ${st.finished_at??""} — ${esc(JSON.stringify(st.result??{}))}`}
    return `<div class="miner-row"><div><b>${label}</b>
      <div class="st">${desc}</div><div class="st">${s}</div></div>
      <span style="white-space:nowrap">${hasLimit?`<input id="ad-limit-${k}" type="number" value="50" min="1" max="1000" style="width:74px">`:""}
      <button class="link" onclick="runMiner('${k}')">▶ run</button></span></div>`}).join("");
  $("ad-caps").innerHTML=(r.capabilities||[]).map(c=>`
    <div class="miner-row" id="cap-${c.id}"><div style="flex:1"><b>${esc(c.name)}</b>
      <span class="pill" style="margin-left:6px">${c.configured?"configured":"not set up"}</span>
      <div class="st">${esc(c.detail)}</div>
      <div class="capform ${c.configured?"hidden":""}" id="capform-${c.id}">
        <input type="password" id="capkey-${c.id}" autocomplete="off" spellcheck="false"
          placeholder="${esc(c.placeholder)}"
          onkeydown="if(event.key==='Enter')saveKey('${c.id}')">
        <button class="link" style="margin:0" onclick="saveKey('${c.id}')">Validate &amp; save</button>
        <a class="link" style="margin:0" target="_blank" rel="noreferrer noopener"
          href="${esc(c.get_key_url)}">Get a key ↗</a>
      </div>
      <div class="st" id="capmsg-${c.id}"></div></div>
      ${c.configured?`<button class="link" onclick="$('capform-${c.id}').classList.toggle('hidden')">Replace key</button>`:""}
    </div>`).join("");
  if(!$("admin").classList.contains("hidden")
     &&Object.values(r.miners).some(m=>m&&m.state==="running"))setTimeout(loadAdmin,4000);
}
async function saveKey(id){
  const inp=$(`capkey-${id}`),msg=$(`capmsg-${id}`);
  const key=inp.value.trim();
  if(!key)return;
  msg.textContent="Checking…";
  const r=await api("/api/admin/key",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({service:id,key})});
  if(r.valid){inp.value="";msg.innerHTML=`<span style="color:var(--ok)">✓ ${esc(r.unlocked)}</span>`;
    setTimeout(loadAdmin,1200)}
  else msg.innerHTML=`<span style="color:var(--err)">${esc(r.error)}</span>`;
}
async function saveAdmin(){
  await api("/api/admin/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({auto_threshold:+$("ad-thr").value,max_searches:+$("ad-srch").value,
      workers:+$("ad-wrk").value,propagation:$("ad-prop").checked?1:0,
      sync_interval_hours:+$("ad-sync").value})});
  $("ad-msg").textContent="saved ✓";setTimeout(()=>{$("ad-msg").textContent=""},1500);
}
// settings commit on change too — the button is just reassurance
document.addEventListener("change",e=>{
  if(["ad-thr","ad-srch","ad-wrk","ad-prop","ad-sync"].includes(e.target.id))saveAdmin();
});
async function runMiner(k){
  const lim=$(`ad-limit-${k}`);
  await api("/api/admin/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({miner:k,limit:lim?+lim.value:undefined})});
  loadAdmin();
}

/* ── spending themes ── */
let THDEFS=[];
async function loadThemes(){
  const r=await api("/api/themes");
  THDEFS=r.definitions||[];
  $("th-tpl").innerHTML=`<option value="">(blank theme)</option>`+
    (r.templates||[]).map(t=>`<option value="${esc(t.name)}">${esc(t.name)} — template</option>`).join("");
  $("ad-themes").innerHTML=THDEFS.map((d,i)=>`
    <div class="miner-row" style="align-items:flex-start"><div style="flex:1"><b>${esc(d.name)}</b>
      <div class="thchips">${d.rules.map((rl,j)=>`<span class="pill">${esc(rl.kind)}: ${esc(rl.value)}
        <button class="link" style="margin:0;padding:0 3px" title="remove this rule"
          onclick="thRule(${i},'remove',${j})">✕</button></span>`).join("")
        ||`<span class="st">no rules yet — this theme matches nothing</span>`}</div>
      <div class="thadd">
        <select id="th-kind-${i}" aria-label="Rule kind">
          <option value="subcategory">subcategory equals</option>
          <option value="merchant">merchant LIKE</option></select>
        <input id="th-val-${i}" data-suggest="subcats" autocomplete="off"
          placeholder="Subcategory — or a LIKE pattern, e.g. example builders%"
          onkeydown="if(event.key==='Enter')thRule(${i},'add')">
        <button class="link" onclick="thRule(${i},'add')">＋ add rule</button>
      </div></div>
      <button class="link" onclick="thDelete(${i})">delete theme</button>
    </div>`).join("")
    ||`<div class="st" style="margin-top:6px">No themes yet — create one above, or start from a template.</div>`;
}
function thMsg(t){$("th-msg").textContent=t;setTimeout(()=>{$("th-msg").textContent=""},3000)}
async function thPost(path,body){
  const resp=await fetch(path,{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(resp.ok)return true;
  const j=await resp.json().catch(()=>({}));
  thMsg(j.detail||"failed");return false;
}
async function createTheme(){
  const tpl=$("th-tpl").value||null;
  const name=$("th-name").value.trim()||tpl||"";
  if(!name){thMsg("give the theme a name");return}
  if(await thPost("/api/themes",{name,template:tpl})){
    $("th-name").value="";$("th-tpl").value="";loadThemes()}
}
async function thRule(i,action,j){
  const d=THDEFS[i];if(!d)return;
  const body=action==="add"
    ?{theme:d.name,action,kind:$(`th-kind-${i}`).value,value:$(`th-val-${i}`).value}
    :{theme:d.name,action,kind:d.rules[j].kind,value:d.rules[j].value};
  if(await thPost("/api/themes/rules",body))loadThemes();
}
async function thDelete(i){
  const d=THDEFS[i];if(!d)return;
  if(!confirm(`Delete theme "${d.name}" and its ${d.rules.length} rule(s)? `+
              `Transactions are untouched — a theme is only a lens.`))return;
  if(await thPost("/api/themes/delete",{name:d.name}))loadThemes();
}
let rvT;for(const id of["rv-q","rv-status","rv-plumbing"]){
  document.addEventListener("input",e=>{if(e.target.id===id){clearTimeout(rvT);
    rvT=setTimeout(loadReview,250)}})}
// wrap-text toggle, persisted
function initWrap(){
  const w=localStorage.getItem("sg-wrap")==="1";
  $("wrap-toggle").checked=w;
  $("tbl").classList.toggle("wraptext",w);
  $("wrap-toggle").addEventListener("change",()=>{
    localStorage.setItem("sg-wrap",$("wrap-toggle").checked?"1":"0");
    $("tbl").classList.toggle("wraptext",$("wrap-toggle").checked);
  });
}
// theme picker — both copies stay in step; initial value applied pre-paint
function initTheme(){
  const cur=localStorage.getItem("sg-theme")||"zinc";
  for(const id of["theme","theme-gate"]){
    const sel=$(id); if(!sel)continue;
    sel.value=cur;
    sel.addEventListener("change",()=>{
      const t=sel.value;
      localStorage.setItem("sg-theme",t);
      if(t==="zinc")delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme=t;
      for(const other of["theme","theme-gate"]){if($(other))$(other).value=t}
    });
  }
}
const esc=s=>(s??"").toString().replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",
  '"':"&quot;","'":"&#39;"}[c]));
initTheme();
initWrap();
boot();
</script></body></html>
"""


# ── the spending page ───────────────────────────────────────────────────────
# Theme variables are a copy of PAGE's block — keep the two in sync (port to
# a shared constant when the third page appears).
PAGE_VIZ = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>redbark — spending</title>
<style>
:root{--bg:#14161a;--card:#1c1f26;--card2:#23272f;--line:#2c313c;
      --fg:#d8dde6;--muted:#8b93a2;--faint:#6b7280;
      --accent:#6ea8fe;--accent-ink:#0b1220;--accent-soft:#1e2a3d;--ok:#4cc38a;
      --warn-bg:#3a2a12;--warn-fg:#e5c07b;--err:#e06c75;
      --credit:#4cc38a;--debit:#d8dde6}
[data-theme="midnight"]{--bg:#0b1120;--card:#121a2b;--card2:#1c2740;
      --line:#2a3a55;--fg:#e8ecf6;--muted:#9aa7c2;--faint:#6e7c9c;
      --accent:#6ea8fe;--accent-ink:#0b1120;--accent-soft:#17233c;--warn-bg:#33280f;
      --credit:#4cc38a;--debit:#e8ecf6}
[data-theme="espresso"]{--bg:#141110;--card:#1b1815;--card2:#282218;
      --line:#3a3226;--fg:#f1ece4;--muted:#b3a895;--faint:#837868;
      --accent:#d19a66;--accent-ink:#141110;--accent-soft:#2e2318;--warn-bg:#3a2a12;
      --credit:#7cc99e;--debit:#f1ece4}
[data-theme="oled"]{--bg:#000;--card:#0d0d0f;--card2:#1a1a1d;--line:#26262b;
      --fg:#f5f5f7;--muted:#a0a0a8;--faint:#707078;
      --accent:#6ea8fe;--accent-ink:#000;--accent-soft:#14202f;--warn-bg:#2e2208;
      --credit:#4cc38a;--debit:#f5f5f7}
[data-theme="light"]{--bg:#fff;--card:#f6f6f8;--card2:#ececf0;--line:#d5d5dc;
      --fg:#101012;--muted:#4a4a52;--faint:#7c7c87;
      --accent:#2563eb;--accent-ink:#fff;--accent-soft:#e6edfd;--ok:#15803d;
      --warn-bg:#fdecea;--warn-fg:#8c1d10;--err:#b91c1c;
      --credit:#15803d;--debit:#101012}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:var(--bg);color:var(--fg)}
.wrap{margin:0 auto;padding:20px}
h1{font-size:20px;margin:0}
h1 .bark{color:var(--accent)}
h1 .sub2{color:var(--muted);font-weight:400;font-size:15px}
h2{font-size:15px;margin:0;color:var(--muted);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:18px;margin-bottom:14px}
.topbar{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:16px}
.topright{display:flex;align-items:center;gap:12px}
a.link,button.link{background:none;border:0;color:var(--muted);cursor:pointer;
      font:inherit;font-size:13px;text-decoration:none;padding:4px}
a.link:hover,button.link:hover{color:var(--accent)}
.theme-pick{width:auto;padding:3px 8px;font-size:12px;background:var(--card2);
      color:var(--muted);border:1px solid var(--line);border-radius:6px}
/* Headline the insight, not the axis */
.insight{font-size:22px;font-weight:650;margin-bottom:2px;letter-spacing:-.01em}
.insight .delta-down{color:var(--ok)}
.insight .delta-up{color:var(--warn-fg)}
.insight-sub{color:var(--muted);font-size:13px;margin-bottom:14px}
.exbar{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 10px}
.exbar:empty{display:none}
.exchip{font-size:12px;padding:2px 10px;border-radius:99px;cursor:pointer;
  border:1px solid var(--line);background:var(--card2);color:var(--fg);
  user-select:none}
.exchip:hover{border-color:var(--accent)}
.exchip.off{opacity:.45;text-decoration:line-through}
.controls{display:flex;justify-content:space-between;align-items:center;
      flex-wrap:wrap;gap:10px;margin-bottom:6px}
.seg{display:inline-flex;background:var(--card2);border:1px solid var(--line);
      border-radius:8px;overflow:hidden}
.seg button{background:none;border:0;color:var(--muted);font:inherit;font-size:13px;
      padding:6px 14px;cursor:pointer}
.seg button.on{background:var(--accent);color:var(--accent-ink);font-weight:600}
.tog{display:inline-flex;gap:6px;align-items:center;font-size:13px;color:var(--muted);
      cursor:pointer}
.cathead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.hidden{display:none}
#txlist table{width:100%;border-collapse:collapse;font-size:13.5px}
#txlist th,#txlist td{padding:7px 9px;text-align:left;border-bottom:1px solid var(--line)}
#txlist th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
#txlist td.amt,#txlist th.amt{text-align:right;font-variant-numeric:tabular-nums}
#txlist .txtotal{color:var(--muted);font-size:13px;margin:4px 0 10px}
#txlist input.txin{width:100%;padding:5px 8px;font-size:13px;
   border:1px solid var(--line);border-radius:6px;background:var(--bg);
   color:var(--fg)}
#txlist input.txin:focus{border-color:var(--accent);outline:none}
.tx-approve{color:var(--ok);background:none;border:0;cursor:pointer;
   font:inherit;font-weight:600;padding:2px 6px}
.sglist{position:fixed;z-index:50;background:var(--card2);border:1px solid var(--line);
   border-radius:8px;overflow-y:auto;overscroll-behavior:contain;
   scrollbar-width:thin;scrollbar-color:var(--line) transparent;
   box-shadow:0 8px 28px rgba(0,0,0,.4)}
/* the scrollbar IS the "there's more" affordance — keep it visible */
.sglist::-webkit-scrollbar{width:10px}
.sglist::-webkit-scrollbar-thumb{background:var(--line);border-radius:5px}
.sglist::-webkit-scrollbar-track{background:transparent}
.sglist div{padding:6px 11px;cursor:pointer;font-size:13px;color:var(--fg);
   white-space:nowrap}
.sglist div:hover,.sglist div.on{background:var(--accent-soft);color:var(--accent)}
.toast{position:fixed;bottom:22px;left:50%;transform:translate(-50%,14px);
   background:var(--card2);border:1px solid var(--ok);color:var(--fg);
   padding:10px 16px;border-radius:9px;font-size:13.5px;z-index:60;
   opacity:0;pointer-events:none;transition:all .18s;max-width:560px}
.toast.bad{border-color:var(--err)}
.toast.on{opacity:1;transform:translate(-50%,0)}
.pacing{margin:2px 0 12px;padding:9px 13px;background:var(--card2);
   border:1px solid var(--line);border-radius:8px;font-size:13.5px}
.pacing b{font-variant-numeric:tabular-nums}
.pacing .over{color:var(--warn-fg)}.pacing .under{color:var(--ok)}
.trendgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.trendgrid .card{margin-bottom:0}
.trsub{color:var(--faint);font-size:12.5px;margin:2px 0 10px}
.trrow{display:flex;justify-content:space-between;gap:10px;padding:6px 0;
   border-bottom:1px solid var(--line);font-size:13.5px;align-items:baseline}
.trrow:last-child{border-bottom:0}
.trrow .amt{font-variant-numeric:tabular-nums;white-space:nowrap}
.trrow .who{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trrow .spark{font-size:11px;letter-spacing:1px;color:var(--accent);opacity:.8}
.up{color:var(--warn-fg)}.dn{color:var(--ok)}
.trempty{color:var(--faint);font-size:13px;padding:8px 0}
details.explain{margin:2px 0 12px;font-size:12.5px;color:var(--muted);
   width:100%;border-bottom:1px solid var(--line);padding-bottom:8px}
details.explain summary{cursor:pointer;color:var(--faint);user-select:none;
   list-style:none;display:block;width:100%}
details.explain summary::before{content:"ⓘ ";}
details.explain summary:hover{color:var(--accent)}
details.explain[open] summary{color:var(--muted)}
details.explain p{margin:7px 0 0;line-height:1.5}
details.explain b{color:var(--fg)}
@media(max-width:900px){.trendgrid{grid-template-columns:1fr}}
@media(max-width:700px){.insight{font-size:18px}#spendchart{height:260px!important}}
</style></head><body>
<script>
  (function(){var t=localStorage.getItem("sg-theme");
   if(t&&t!=="zinc")document.documentElement.dataset.theme=t;})();
</script>
<div class="wrap">
  <div class="topbar">
    <h1>redbark-<span class="bark">local</span>-store <span class="sub2">/ spending</span></h1>
    <div class="topright">
      <a class="link" href="/">← transactions</a>
      <select id="theme" class="theme-pick"><option value="zinc">Zinc</option>
        <option value="midnight">Midnight</option><option value="espresso">Espresso</option>
        <option value="oled">OLED</option><option value="light">Light</option></select>
    </div>
  </div>
  <div class="card">
    <div class="insight" id="insight">loading…</div>
    <div class="insight-sub" id="insight-sub"></div>
    <div class="pacing hidden" id="pacing"></div>
    <div class="controls">
      <div class="seg" id="range">
        <button data-d="30">1M</button><button data-d="90" class="on">3M</button>
        <button data-d="180">6M</button><button data-d="365">1Y</button>
        <button data-d="730">2Y</button>
      </div>
      <div class="seg" id="bucketsel" title="Bar scale">
        <button data-b="day">Day</button><button data-b="week">Week</button>
        <button data-b="month" class="on">Month</button>
      </div>
      <label class="tog"><input type="checkbox" id="inc-transfers"> include transfers &amp; loan payments</label>
    </div>
    <details class="explain"><summary>what am I looking at?</summary>
      <p>Each bar is what you spent in that period (transfers between your own
      accounts and loan principal are excluded — they're not consumption). The
      <b>solid line</b> is your smoothed trend; the <b>straight dash-dot
      line</b> is the least-squares fit across the window — green sloping
      down, amber sloping up, with the per-period rate in the header (the
      current incomplete period is drawn but never used in the fit); the
      <b>dashed line</b> is the same-length window one period earlier.
      <b>Good:</b> bars sitting at or under the dashed line, fit sloping
      down. <b>Bad:</b> the trend climbing clear of the dashed line for
      several periods — that's a new, higher normal forming. The pacing
      strip above projects this month from
      your own typical rest-of-month, not a straight-line guess: green means
      you're pacing under your median month.</p></details>
    <div id="spendchart" style="height:340px"></div>
  </div>
  <div class="card">
    <div class="cathead"><h2 id="cat-title">Where it went</h2>
      <button class="link hidden" id="cat-back" onclick="drillUp()">← back</button></div>
    <details class="explain"><summary>what am I looking at?</summary>
      <p>Your biggest spending buckets in the selected window, largest at the
      top, with the change vs the previous same-length window as a chip
      (<b>▲ amber = up, ▼ green = down</b>). Click a bar to see its
      subcategories, click again to see the actual transactions — every
      number here bottoms out in real rows you can inspect. <b>Good:</b> the
      big bars are things you'd endorse on purpose, and chips on
      discretionary categories point down. <b>Bad:</b> a discretionary
      category showing ▲ several windows in a row — that's drift, not a
      decision.</p></details>
    <div id="exbar" class="exbar"></div>
    <div id="catchart" style="height:430px"></div>
    <div id="txlist" class="hidden"></div>
  </div>
  <div class="trendgrid">
    <div class="card" id="tr-waterfall"><h2>What changed, and why</h2>
      <div class="trsub" id="wf-sub"></div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>The bridge from last month's total to this month's: the grey bar is
        where you started, each coloured step is a category that pushed the
        total up (<b>amber</b>) or down (<b>green</b>), and the blue bar is
        where you landed. <b>Good:</b> the big steps are things you chose — a
        planned trip, an annual bill landing. <b>Bad:</b> the big steps are
        drift you didn't decide on. A small "everything else" bar means the
        story is concentrated in a few categories — which makes it fixable.</p>
      </details>
      <div id="wfchart" style="height:300px"></div></div>
    <div class="card" id="tr-momentum"><h2>Momentum</h2>
      <div class="trsub">trailing 3 months vs the 3 before — heating vs cooling</div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>Each category's average over the last three complete months against
        the three before that. Month-to-month numbers are noisy; this catches
        the <b>slow drift</b> a single month hides. <b>Good:</b> discretionary
        categories cooling (▼). <b>Bad:</b> a category heating (▲) with no
        life-change that explains it — that's the earliest, cheapest moment to
        interrupt a trend, well before it shows up as a shocking month.</p>
      </details>
      <div id="momentum"></div></div>
    <div class="card" id="tr-creep"><h2>Recurring price creep</h2>
      <div class="trsub" id="creep-sub"></div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>Recurring charges whose price has moved since they started — earliest
        typical price → latest, with the difference <b>annualised</b>. An A$3/month
        rise reads as ignorable; A$36/yr is the honest number, and several
        together are real money. <b>Good:</b> an empty list, or green
        (prices fell). <b>Bad:</b> a positive total — every line here is a
        renegotiate-or-cancel candidate, and providers count on you not
        noticing.</p></details>
      <div id="creep"></div></div>
    <div class="card" id="tr-anomalies"><h2>Unusual last month</h2>
      <div class="trsub">vs your own 12-month typical (median + 3×MAD)</div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>Categories where last month was far outside <b>your own</b> normal
        range — measured robustly (median plus three median-absolute-deviations),
        so one odd week doesn't trigger it. Each flag names the transactions
        that drove it. <b>Good:</b> empty, or flags you can explain in one
        sentence ("that was the car service"). <b>Bad:</b> a flag you can't
        explain — check the culprit rows before it repeats.</p></details>
      <div id="anomalies"></div></div>
    <div class="card" id="tr-pareto"><h2>Focus list</h2>
      <div class="trsub" id="pareto-sub"></div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>The handful of merchants that make up most of your <b>variable</b>
        (non-subscription) spend, with each one's share and a six-month shape.
        Attention is the scarce resource: trimming one merchant at the top of
        this list beats optimising twenty small ones. <b>Good:</b> no
        surprises — you could have named this list. <b>Bad:</b> a merchant you
        wouldn't have guessed sitting in the top five.</p></details>
      <div id="pareto"></div></div>
    <div class="card" id="tr-wins"><h2>Locked-in wins</h2>
      <div class="trsub" id="wins-sub"></div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>Money you've already saved, annualised: recurring charges that
        stopped, and categories you've held well below their old level for
        three-plus months. This is the scoreboard for everything else on the
        page — the other cards prompt action, this one proves it worked.
        <b>Good:</b> this number growing. There is no bad here; an empty list
        just means the wins haven't started yet.</p></details>
      <div id="wins"></div></div>
    <div class="card" id="tr-floor" style="grid-column:1/-1"><h2>Your committed floor</h2>
      <div class="trsub">recurring (fixed) vs everything else — a rising floor is the earliest structural warning</div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>Every month split into <b>recurring commitments</b> (subscriptions,
        utilities, rent-like charges — the blue base) and everything else. The
        blue base is money spent before the month even starts. <b>Good:</b> a
        flat or falling floor — discipline in any given month actually moves
        your total. <b>Bad:</b> a floor that ratchets upward — each new
        commitment quietly shrinks the part of your income you still have
        choices about.</p></details>
      <div id="floorchart" style="height:280px"></div></div>
    <div class="card" id="tr-subs" style="grid-column:1/-1"><h2>Subscriptions</h2>
      <div class="trsub" id="subs-sub"></div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>Every recurring charge, priced honestly: the <b>annualised</b>
        column is the real cost (a yearly plan isn't a spike, a small monthly
        isn't pocket change). Flags: <b>×2</b> = two or more active lines at
        the same vendor — usually an accidental double subscription;
        <b>renewing</b> = expected to bill within a fortnight — the moment to
        cancel or downgrade is before that, not after; <b>bills
        yearly/quarterly</b> = don't read its charge as a monthly amount.
        <b>Good:</b> you could name every line here. <b>Bad:</b> a line you
        don't recognise or a ×2 — each one is found money.</p></details>
      <div id="subs"></div></div>
    <div class="card" id="tr-themes" style="grid-column:1/-1"><h2>Themes</h2>
      <div class="trsub">cross-cutting lenses — one number for things that span categories</div>
      <details class="explain"><summary>what am I looking at?</summary>
        <p>A theme collects everything about one part of your life no matter
        which category it landed in — a renovation is trades + hardware +
        architects + skip hire; pets are supplies + vet + insurance. Each row
        is the trailing-12-month total with the monthly shape; click it to see
        (and correct) the actual transactions. Themes never move a
        transaction — they're a lens over the same data. <b>Good:</b> the
        number matches what you think you're spending on that part of your
        life. <b>Bad:</b> it doesn't — and now you know by exactly how
        much.</p></details>
      <div id="themes"></div></div>
  </div>
</div>
<script src="/static/echarts.min.js"></script>
<script>
const $=id=>document.getElementById(id);
const state={days:+(localStorage.getItem("sg-days")||90),
  bucket:localStorage.getItem("sg-bucket")||"month",
  transfers:false,cat:null,sub:null,themeSel:null,
  exCats:new Set(JSON.parse(localStorage.getItem("sg-excat")||"[]")),
  exSubs:new Set(JSON.parse(localStorage.getItem("sg-exsub")||"[]"))};
function toggleEx(kind,key){
  const set=kind==="cat"?state.exCats:state.exSubs;
  set.has(key)?set.delete(key):set.add(key);
  localStorage.setItem(kind==="cat"?"sg-excat":"sg-exsub",
    JSON.stringify([...set]));
  load();
}
let catData=[],prevCats={},spendChart=null,catChart=null,curRange={};
const cssVar=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const aud=c=>"A$"+(Math.abs(c)/100).toLocaleString(undefined,{maximumFractionDigits:0});
const iso=d=>d.toISOString().slice(0,10);
const MONTHS=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function bucketOf(days){return days<=45?"day":days<=200?"week":"month"}
function fmtBucket(b,bucket){
  if(bucket==="month"){const[y,m]=b.split("-");return MONTHS[+m-1]+" "+y.slice(2)}
  const d=new Date(b);return d.getDate()+" "+MONTHS[d.getMonth()];
}
async function fetchJSON(u){const r=await fetch(u);if(r.status===401){location.href="/";throw 0}return r.json()}
async function load(){
  const bucket=state.bucket||bucketOf(state.days);
  const to=new Date(),from=new Date();from.setDate(from.getDate()-state.days);
  if(bucket==="month")from.setDate(1);  // whole first month, no phantom dip
  const from2=new Date(from);from2.setDate(from2.getDate()-state.days);
  const to2=new Date(from);to2.setDate(to2.getDate()-1);
  const t=state.transfers?1:0;
  const q=(f,tt)=>`date_from=${iso(f)}&date_to=${iso(tt)}&transfers=${t}`;
  curRange={from:iso(from),to:iso(to)};
  // The timeline focuses on whatever you've drilled into; the category
  // chart stays whole — it's the navigation.
  const dq=(state.themeSel?`&theme=${encodeURIComponent(state.themeSel)}`
    :state.cat?`&category=${encodeURIComponent(state.cat)}`
      +(state.sub!=null?`&subcategory=${encodeURIComponent(state.sub)}`:"")
    :"")
    +(state.exCats.size?`&exclude_cats=${encodeURIComponent([...state.exCats].join(","))}`:"")
    +(state.exSubs.size?`&exclude_subs=${encodeURIComponent([...state.exSubs].join(","))}`:"");
  const[cur,prev,cats,pcats]=await Promise.all([
    fetchJSON(`/api/viz/series?${q(from,to)}&bucket=${bucket}${dq}&split=1`),
    fetchJSON(`/api/viz/series?${q(from2,to2)}&bucket=${bucket}${dq}`),
    fetchJSON(`/api/viz/categories?${q(from,to)}`),
    fetchJSON(`/api/viz/categories?${q(from2,to2)}`),
  ]);
  catData=cats.categories;
  prevCats={};
  for(const c of pcats.categories){prevCats[c.key]={cents:c.cents,subs:{}};
    for(const s of c.subs)prevCats[c.key].subs[s.name]=s.cents}
  renderInsight(cur,prev);
  renderSpend(cur,prev,bucket);
  if(state.sub!=null||state.themeSel)renderTxList(); // stay on the drill
  else renderCats();
}
function focusLabel(){
  if(state.themeSel)return state.themeSel;
  if(state.cat)return (CATLABEL[state.cat]||state.cat)
    +(state.sub!=null&&state.sub!=="(no subcategory)"?" · "+state.sub:"");
  return null;
}
function renderInsight(cur,prev){
  const c=cur.total_debit_cents,p=prev.total_debit_cents;
  const label={30:"month",90:"3 months",180:"6 months",365:"year",730:"2 years"}[state.days];
  let deltaTxt="";
  if(p>0){
    const pct=Math.round((c-p)/p*100);
    const cls=pct<=0?"delta-down":"delta-up";
    deltaTxt=` — <span class="${cls}">${pct<=0?"down":"up"} ${Math.abs(pct)}%</span> on the previous ${label}`;
  }
  const focus=focusLabel();
  $("insight").innerHTML=`${focus?`<span style="color:var(--accent)">${esc(focus)}</span>: `:""}${aud(c)} spent in the last ${label}${deltaTxt}`;
  const nex=state.exCats.size+state.exSubs.size;
  $("insight-sub").textContent=`${cur.txn_count.toLocaleString()} transactions`
    +(state.transfers?" · including transfers & loan payments":" · transfers & loan payments excluded")
    +(nex?` · ${nex} categor${nex===1?"y":"ies"}/subs toggled off`:"");
}
function baseOpts(th){return{
  animationDuration:250,
  tooltip:{backgroundColor:th.card2,borderColor:th.line,
    textStyle:{color:th.fg,fontSize:13},confine:true},
}}
function movingAvg(vals,w){
  return vals.map((_,i)=>{
    const s=vals.slice(Math.max(0,i-w+1),i+1);
    return Math.round(s.reduce((a,b)=>a+b,0)/s.length);
  });
}
/* Least-squares line over the window. The current (incomplete) bucket is
   excluded from the FIT — a half-finished month reads as a collapse and
   would drag every slope down — but the line still extends across it. */
function linTrend(vals,bucket){
  const fitN=bucket==="day"?vals.length:Math.max(2,vals.length-1);
  if(fitN<3)return null;
  let sx=0,sy=0,sxy=0,sxx=0;
  for(let x=0;x<fitN;x++){const y=vals[x];sx+=x;sy+=y;sxy+=x*y;sxx+=x*x}
  const d=fitN*sxx-sx*sx;
  if(!d)return null;
  const b=(fitN*sxy-sx*sy)/d,a=(sy-b*sx)/fitN;
  return {slope:b,line:vals.map((_,x)=>Math.max(0,Math.round(a+b*x)))};
}
/* Contributor palette: fixed slot order (identity is the colour's job),
   legible on all five house themes; "(other)" is always the muted grey. */
const SLOTC=["#3987e5","#eb6834","#1baf7a","#eda100","#e87ba4","#9085e9"];
let CATCOLOR={};  // key -> colour, shared with "Where it went" for identity
function renderSpend(cur,prev,bucket){
  const th=theme();
  if(spendChart)spendChart.dispose();
  spendChart=echarts.init($("spendchart"));
  const vals=cur.buckets.map(b=>Math.round((b.debit_cents||0)/100));
  const prevVals=prev.buckets.map(b=>Math.round((b.debit_cents||0)/100));
  CATCOLOR={};
  const split=(cur.split||[]).map((s,i)=>{
    const color=s.key==="(other)"?th.faint:SLOTC[i%SLOTC.length];
    CATCOLOR[s.key]=color;
    return{name:s.key==="(other)"?"other":(CATLABEL[s.key]||s.key),
      color,vals:s.cents.map(c=>Math.round(c/100))}});
  // Comparison series aligned by position: same-length window, one period back.
  const prevAligned=vals.map((_,i)=>prevVals[i]??null);
  const trendW=bucket==="day"?7:3;
  const lt=linTrend(vals,bucket);
  const ltUp=lt&&lt.slope>0;
  const unit={day:"day",week:"wk",month:"mo"}[bucket];
  const sub=$("insight-sub");
  sub.querySelector(".lt-note")?.remove();
  if(lt&&Math.abs(lt.slope)>=1)sub.insertAdjacentHTML("beforeend",
    `<span class="lt-note"> · linear trend <span style="color:${ltUp?"var(--warn-fg)":"var(--ok)"}">`+
    `${ltUp?"▲":"▼"} ${aud(Math.abs(lt.slope)*100)}/${unit}</span> across this window</span>`);
  const stacked=split.length>1;
  const barSeries=stacked
    ?split.map(s=>({name:s.name,type:"bar",stack:"spend",data:s.vals,
       itemStyle:{color:s.color},barMaxWidth:38}))
    :[{name:"spend",type:"bar",data:vals,
       itemStyle:{color:th.accent,borderRadius:[3,3,0,0]},barMaxWidth:38}];
  spendChart.setOption({...baseOpts(th),
    grid:{left:8,right:8,top:stacked?34:14,bottom:26,containLabel:true},
    legend:stacked?{top:0,left:0,icon:"roundRect",itemWidth:10,itemHeight:10,
      textStyle:{color:th.muted,fontSize:11},
      data:split.map(s=>s.name)}:undefined,
    xAxis:{type:"category",data:cur.buckets.map(b=>fmtBucket(b.b,bucket)),
      axisLine:{lineStyle:{color:th.line}},axisTick:{show:false},
      axisLabel:{color:th.muted,fontSize:11}},
    yAxis:{type:"value",splitLine:{lineStyle:{color:th.line}},
      axisLabel:{color:th.muted,fontSize:11,formatter:v=>aud(v*100)}},
    tooltip:{...baseOpts(th).tooltip,trigger:"axis",
      axisPointer:{type:"shadow"},
      formatter:ps=>{
        const i=ps[0].dataIndex;
        const tr=ps.find(p=>p.seriesName==="trend");
        const lin=ps.find(p=>p.seriesName==="linear trend");
        const pv=ps.find(p=>p.seriesName==="previous period");
        let s=`<b>${ps[0].name}</b><br>${aud((vals[i]||0)*100)} spent`;
        if(stacked){
          // Contributors largest-first — the spike explains itself.
          const parts=split.map(x=>({n:x.name,v:x.vals[i]||0,c:x.color}))
            .filter(x=>x.v>0).sort((a,b)=>b.v-a.v);
          for(const x of parts.slice(0,7))
            s+=`<br><span style="color:${x.c}">●</span> ${x.n} ${aud(x.v*100)}`;
        }else{
          const bar=ps.find(p=>p.seriesName==="spend");
          if(!bar)s=`<b>${ps[0].name}</b>`;
        }
        if(tr&&tr.value!=null)s+=`<br><span style="opacity:.75">trend ${aud(tr.value*100)}</span>`;
        if(lin&&lin.value!=null)s+=`<br><span style="opacity:.75">linear ${aud(lin.value*100)}</span>`;
        if(pv&&pv.value!=null)s+=`<br><span style="opacity:.6">prev period ${aud(pv.value*100)}</span>`;
        return s}},
    dataZoom:[{type:"inside"}],
    series:[
      ...barSeries,
      {name:"trend",type:"line",data:movingAvg(vals,trendW),smooth:true,
       symbol:"none",lineStyle:{color:th.fg,width:2,opacity:.85},z:3},
      ...(lt?[{name:"linear trend",type:"line",data:lt.line,
       symbol:"none",lineStyle:{color:ltUp?th.warn:th.ok,width:1.8,
         type:[6,5],opacity:.9},z:4}]:[]),
      {name:"previous period",type:"line",data:prevAligned,smooth:true,
       symbol:"none",lineStyle:{color:th.faint,width:1.5,type:"dashed",opacity:.7},z:2},
    ],
  });
}
const esc=s=>(s??"").toString().replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",
  ">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function pctOf(cur,prev){
  if(!prev||prev<1)return null;
  return Math.round((cur-prev)/prev*100);
}
function renderCats(){
  const th=theme();
  $("txlist").classList.add("hidden");
  $("catchart").classList.remove("hidden");
  let src,title;
  if(state.cat==null){
    // A category's shown total drops any subs toggled off; a fully toggled-off
    // category drops out of the bars (its chip is the way back).
    src=catData.filter(c=>!state.exCats.has(c.key)).map(c=>{
      const exc=(c.subs||[]).filter(s=>state.exSubs.has(s.name))
        .reduce((a,s)=>a+s.cents,0);
      return{key:c.key,label:CATLABEL[c.key]||c.key,cents:c.cents-exc,
        prev:prevCats[c.key]?prevCats[c.key].cents:null}});
    $("exbar").innerHTML=catData.map(c=>{
      const off=state.exCats.has(c.key);
      return `<span class="exchip${off?" off":""}" title="${off?"Click to include":"Click to exclude from all charts"}"
        onclick="toggleEx('cat',${JSON.stringify(c.key).replace(/"/g,"&quot;")})">${esc(CATLABEL[c.key]||c.key)}</span>`}).join("");
    title="Where it went";
  }else{
    const c=catData.find(c=>c.key===state.cat);
    src=((c&&c.subs)||[]).filter(s=>!state.exSubs.has(s.name))
      .map(s=>({key:s.name,label:s.name,cents:s.cents,
      prev:prevCats[state.cat]&&prevCats[state.cat].subs[s.name]}));
    $("exbar").innerHTML=((c&&c.subs)||[]).map(s=>{
      const off=state.exSubs.has(s.name);
      return `<span class="exchip${off?" off":""}" title="${off?"Click to include":"Click to exclude from all charts"}"
        onclick="toggleEx('sub',${JSON.stringify(s.name).replace(/"/g,"&quot;")})">${esc(s.name)}</span>`}).join("");
    title=CATLABEL[state.cat]||state.cat;
  }
  $("cat-title").textContent=title;
  $("cat-back").classList.toggle("hidden",state.cat==null);
  src.sort((a,b)=>a.cents-b.cents);
  const shown=src.slice(-14);
  if(catChart)catChart.dispose();
  catChart=echarts.init($("catchart"));
  catChart.setOption({...baseOpts(th),
    grid:{left:8,right:96,top:6,bottom:6,containLabel:true},
    xAxis:{type:"value",splitLine:{show:false},axisLabel:{show:false}},
    yAxis:{type:"category",data:shown.map(s=>s.label),
      axisLine:{show:false},axisTick:{show:false},
      axisLabel:{color:th.fg,fontSize:12.5}},
    tooltip:{...baseOpts(th).tooltip,
      formatter:p=>{const s=shown[p.dataIndex];
        const d=pctOf(s.cents,s.prev);
        return `<b>${p.name}</b><br>${aud(p.value*100)}`
          +(d==null?"":`<br><span style="opacity:.7">${d>=0?"+":""}${d}% vs previous period</span>`)
          +`<br><span style="opacity:.55">click to drill in</span>`}},
    series:[{type:"bar",data:shown.map(s=>({value:Math.round(s.cents/100),
      // same entity, same colour as the timeline stack; beyond the top
      // six it's the stack's grey "other"
      itemStyle:{color:CATCOLOR[s.key]||CATCOLOR["(other)"]||th.accent,
        borderRadius:[0,3,3,0]}})),barMaxWidth:20,
      label:{show:true,position:"right",fontSize:11,
        rich:{v:{color:th.muted},up:{color:th.warn,fontSize:10.5},
              dn:{color:th.ok,fontSize:10.5}},
        formatter:p=>{const s=shown[p.dataIndex];
          const d=pctOf(s.cents,s.prev);
          const chip=d==null?"":d>=0?` {up|▲${d}%}`:` {dn|▼${Math.abs(d)}%}`;
          return `{v|${aud(p.value*100)}}${chip}`}}}],
  });
  catChart.on("click",p=>{
    const s=shown[p.dataIndex];
    if(state.cat==null)state.cat=s.key;
    else state.sub=s.key;
    load();  // the timeline refocuses on the drill
  });
}
let txData=[],statsDirty=false;
function paintTxList(){
  const el=$("txlist");
  const total=txData.reduce((a,t)=>a+Math.abs(t.amount_cents||0),0);
  el.innerHTML=`<div class="txtotal">${txData.length} transactions · ${aud(total)} · largest first — edits apply to every transaction from that merchant</div>
   <table><thead><tr><th style="width:88px">Date</th>
     <th style="width:200px">Merchant</th><th>Description</th>
     <th style="width:150px">Category</th><th style="width:150px">Subcategory</th>
     <th class="amt" style="width:90px">Amount</th><th style="width:34px"></th></tr></thead>
   <tbody>${txData.map((t,i)=>`<tr>
     <td>${t.date??""}</td>
     <td><input class="txin" id="tx-name-${i}" autocomplete="off" value="${esc(t.merchant)}"></td>
     <td style="color:var(--muted)">${esc(t.description)}</td>
     <td><input class="txin" id="tx-cat-${i}" data-suggest="cats" autocomplete="off" value="${esc(CATLABEL[t.category]||t.category||CATLABEL[state.cat]||state.cat||"")}"></td>
     <td><input class="txin" id="tx-sub-${i}" data-suggest="subcats" autocomplete="off" value="${esc(t.subcategory||(!state.sub||state.sub==="(no subcategory)"?"":state.sub))}"></td>
     <td class="amt">${aud(Math.abs(t.amount_cents||0))}</td>
     <td><button class="tx-approve" title="Save (applies to all matching)" onclick="txSave(${i})">✓</button></td></tr>`).join("")}</tbody></table>`;
}
async function renderTxList(){
  $("catchart").classList.add("hidden");
  const el=$("txlist");el.classList.remove("hidden");
  if(state.themeSel)$("exbar").innerHTML="";
  $("cat-title").textContent=state.themeSel
    ?`Theme · ${state.themeSel}`
    :`${CATLABEL[state.cat]||state.cat} · ${state.sub}`;
  $("cat-back").classList.remove("hidden");
  el.innerHTML=`<div class="txtotal">loading…</div>`;
  const p=new URLSearchParams({date_from:curRange.from,date_to:curRange.to,
    transfers:state.transfers?1:0});
  if(state.themeSel)p.set("theme",state.themeSel);
  else{p.set("category",state.cat);p.set("subcategory",state.sub)}
  const r=await fetchJSON("/api/viz/transactions?"+p);
  txData=r.data;
  paintTxList();
}
async function renderSubs(){
  const s=await fetchJSON("/api/subscriptions");
  $("subs-sub").innerHTML=`${s.active.length} active · <b>${aud(s.total_monthly_equiv_cents)}/mo</b> (${aud(s.total_annualised_cents)}/yr)`
    +(s.duplicates.length?` · <b class="up">${s.duplicates.length} duplicate vendor${s.duplicates.length>1?"s":""}</b>`:"")
    +(s.renewing_soon.length?` · ${s.renewing_soon.length} renewing within 14 days`:"");
  const flag=a=>(a.duplicate?`<b class="up">×2</b> `:"")
    +(a.renewing_soon?`<span style="color:var(--warn-fg)">renewing ${esc(a.next_expected)}</span> `:"")
    +(a.billed_infrequently?`<span style="color:var(--faint)">bills ${esc(a.cadence)}</span>`:"");
  $("subs").innerHTML=s.active.map(a=>`<div class="trrow">
    <span class="who">${esc(a.name)} <span style="color:var(--faint)">${esc(a.subcategory||"")}</span> ${flag(a)}</span>
    <span class="amt">${aud(a.typical_cents)}/${esc(a.cadence)}
      <b>${aud(a.annualised_cents)}/yr</b></span></div>`).join("")
   ||`<div class="trempty">no recurring charges detected yet — run the enrich miner</div>`;
  if(s.stopped.length)$("subs").insertAdjacentHTML("beforeend",
    `<div class="trsub" style="margin-top:10px">recently stopped: ${
      s.stopped.slice(0,6).map(x=>`${esc(x.name)} (${esc(x.stopped_since)})`).join(" · ")}</div>`);
}
async function renderThemes(){
  const t=await fetchJSON("/api/themes");
  TPLS=t.templates||[];
  $("themes").innerHTML=(t.themes||[]).map(x=>`<div class="trrow" style="cursor:pointer"
     onclick="state.themeSel=${JSON.stringify(x.theme).replace(/"/g,"&quot;")};state.cat=null;state.sub=null;load()">
     <span class="who">${esc(x.theme)} <span class="spark">${spark(x.months.map(m=>m.cents))}</span>
       <span style="color:var(--faint)">${x.txn_count} txns</span></span>
     <span class="amt">${aud(x.total_cents)} <span style="color:var(--faint)">/ ${x.months.length} mo</span></span></div>`).join("")
   ||`<div class="trempty">No themes yet. A theme turns one part of life that
      spans categories into a single number you can watch. Start from a
      template: ${TPLS.map((x,i)=>`<button class="link"
        onclick="quickTheme(${i})">${esc(x.name)}</button>`).join(" ")}
      — or build your own under <a class="link" href="/">admin</a>. Themes
      never change a transaction; they're only a lens.</div>`;
}
let TPLS=[];
async function quickTheme(i){
  const x=TPLS[i];if(!x)return;
  const resp=await fetch("/api/themes",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:x.name,template:x.name})});
  if(resp.ok){toast(`Theme "${x.name}" created — tune its rules under admin`,true);renderThemes()}
  else{const j=await resp.json().catch(()=>({}));toast(j.detail||"couldn't create theme",false)}
}
async function txSave(i){
  const t=txData[i];
  const name=$(`tx-name-${i}`).value.trim();
  let cat=$(`tx-cat-${i}`).value.trim();cat=CATKEY[cat]||cat;
  const sub=$(`tx-sub-${i}`).value.trim();
  if(sub&&SUBTREE[sub])cat=SUBTREE[sub];
  const baseCat=t.category||state.cat;
  const d={merchant_key:t.merchant_key,action:"approve",
    name:name&&name!==t.merchant?name:null,
    category:cat&&cat!==baseCat?cat:null,
    subcategory:sub&&sub!==(t.subcategory||"")?sub:null};
  if(!d.name&&!d.category&&!d.subcategory)return;
  const resp=await fetch("/api/lookups/decide",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({decisions:[d]})});
  const j=await resp.json().catch(()=>({}));
  if(!resp.ok){
    let msg=j.detail||"couldn't save";
    if(/unknown category/.test(msg))msg+=" — the bank's 16 categories are fixed; use a subcategory instead";
    toast(msg,false);return}
  toast("Saved — applies to every matching transaction",true);
  // In-place: patch or drop the merchant's rows right here; charts and
  // taxonomy refresh quietly the next time you leave this list.
  // Theme drill: a recategorised row may still belong to the theme, so
  // patch in place rather than dropping it.
  const moved=!state.themeSel&&(d.category!=null||d.subcategory!=null);
  if(moved)txData=txData.filter(r=>r.merchant_key!==t.merchant_key);
  else for(const r of txData)if(r.merchant_key===t.merchant_key){
    if(d.name)r.merchant=d.name;
    if(d.category)r.category=d.category;
    if(d.subcategory)r.subcategory=d.subcategory;
  }
  paintTxList();
  statsDirty=true;
  loadTaxonomy();
}
function toast(msg,ok){
  let el=$("toast");
  if(!el){el=document.createElement("div");el.id="toast";el.className="toast";
    document.body.appendChild(el)}
  el.textContent=msg;el.classList.toggle("bad",!ok);el.classList.add("on");
  clearTimeout(el._h);el._h=setTimeout(()=>el.classList.remove("on"),4200);
}
/* type-ahead over the taxonomy — same behaviour as the main page */
const SUGGEST={cats:[],subcats:[]};
let SUBTREE={},CATKEY={};
const sg={box:null,input:null,idx:-1,items:[]};
function sgHide(){if(sg.box){sg.box.remove();sg.box=null;sg.input=null;sg.idx=-1;sg.items=[]}}
function sgSource(input){
  if(input.dataset.suggest==="cats")return SUGGEST.cats;
  const m=input.id.match(/^tx-sub-(\d+)$/);
  const sib=m&&$(`tx-cat-${m[1]}`);
  const catRaw=sib?sib.value.trim():"";
  let cat=CATKEY[catRaw]||catRaw;
  if(cat&&!CATLABEL[cat])cat="";  // unresolvable text -> show everything
  const pool=cat?SUGGEST.subcats.filter(s=>s.category===cat||s.category==null)
                :SUGGEST.subcats;
  return pool.map(s=>s.name);
}
function sgShow(input){
  const q=input.value.trim().toLowerCase();
  // Every match, prefix hits first — the list scrolls, so no cap: a value
  // you can't see in a picker may as well not exist.
  sg.items=sgSource(input).filter(s=>s.toLowerCase().includes(q))
    .sort((a,b)=>b.toLowerCase().startsWith(q)-a.toLowerCase().startsWith(q));
  if(!sg.items.length){sgHide();return}
  if(!sg.box){sg.box=document.createElement("div");sg.box.className="sglist";
    document.body.appendChild(sg.box)}
  sg.input=input;sg.idx=-1;
  const r=input.getBoundingClientRect();
  // Fill the space the screen actually has; flip above the input when the
  // bottom is tight. A clipped list with an invisible scrollbar reads as
  // "that's all of them" — never allow that.
  const below=window.innerHeight-r.bottom-12,above=r.top-12;
  const flip=below<220&&above>below;
  sg.box.style.maxHeight=Math.max(160,Math.min(440,flip?above:below))+"px";
  sg.box.style.left=r.left+"px";
  if(flip){sg.box.style.top="";sg.box.style.bottom=(window.innerHeight-r.top+2)+"px"}
  else{sg.box.style.bottom="";sg.box.style.top=(r.bottom+2)+"px"}
  sg.box.style.minWidth=r.width+"px";
  sg.box.innerHTML=sg.items.map((s,i)=>`<div data-i="${i}">${esc(s)}</div>`).join("");
}
function sgPick(i){
  if(sg.input&&sg.items[i]!=null){
    sg.input.value=sg.items[i];
    const parent=SUBTREE[sg.items[i]];
    const m=sg.input.id&&sg.input.id.match(/^tx-sub-(\d+)$/);
    const sib=m&&$(`tx-cat-${m[1]}`);
    if(sib&&parent)sib.value=CATLABEL[parent]||parent;
  }
  sgHide();
}
document.addEventListener("input",e=>{if(e.target.dataset&&e.target.dataset.suggest)sgShow(e.target)});
document.addEventListener("focusin",e=>{if(e.target.dataset&&e.target.dataset.suggest)sgShow(e.target)});
document.addEventListener("mousedown",e=>{
  const it=e.target.closest&&e.target.closest(".sglist div");
  if(it){e.preventDefault();sgPick(+it.dataset.i)}
  else if(!(e.target.dataset&&e.target.dataset.suggest))sgHide();
});
// Page scroll un-anchors the fixed box, so close it — but the box's OWN
// scroll must not (capture phase sees both; that bug ate the scroll).
document.addEventListener("scroll",e=>{
  if(!sg.box||!sg.box.contains(e.target))sgHide();
},true);
/* consistency rule: leaving a drill row with changed values commits them */
let txSaving=false;
document.addEventListener("focusout",e=>{
  const m=e.target.id&&e.target.id.match(/^tx-(name|cat|sub)-(\d+)$/);
  if(!m)return;
  setTimeout(()=>{
    if(txSaving)return;
    const a=document.activeElement;
    const am=a&&a.id&&a.id.match(/^tx-(name|cat|sub)-(\d+)$/);
    if(am&&am[2]===m[2])return;         // still editing the same row
    const i=+m[2];
    if(!txData[i]||!$(`tx-name-${i}`))return;
    txSaving=true;
    txSave(i).finally(()=>{txSaving=false});
  },180);
});
async function loadTaxonomy(){
  const t=await fetchJSON("/api/taxonomy");
  CATLABEL={};CATKEY={};
  for(const c of t.categories){CATLABEL[c.key]=c.label;CATKEY[c.label]=c.key}
  SUGGEST.cats=t.categories.map(c=>c.label);
  SUGGEST.subcats=t.subcategories;
  SUBTREE=Object.fromEntries(t.subcategories.map(s=>[s.name,s.category]));
}
function drillUp(){
  if(state.themeSel)state.themeSel=null;
  else if(state.sub!=null)state.sub=null;
  else state.cat=null;
  statsDirty=false;
  load();  // the timeline widens back out (and picks up any edits)
}
/* ── the eight trend devices (cull freely — each card stands alone) ── */
const catName=k=>CATLABEL[k]||k;
const pm=c=>(c>=0?"+":"−")+aud(Math.abs(c));
function spark(vals){
  const mx=Math.max(...vals,1),B="▁▂▃▄▅▆▇";
  return vals.map(v=>B[Math.min(6,Math.round(v/mx*6))]).join("");
}
let wfChart=null,floorChart=null;
async function renderTrends(){
  const t=await fetchJSON("/api/trends");
  const th=theme();
  // 2. run-rate pacing — inside the hero card
  if(t.run_rate.available){
    const r=t.run_rate,over=r.delta_cents>0;
    $("pacing").classList.remove("hidden");
    $("pacing").innerHTML=`This month: <b>${aud(r.mtd_cents)}</b> so far ·
      on track for <b class="${over?"over":"under"}">${aud(r.projected_cents)}</b>
      — <span class="${over?"over":"under"}">${pm(r.delta_cents)}</span> vs your
      ${r.history_months}-month median (${aud(r.median_month_cents)}),
      paced against your own typical rest-of-month`;
  }
  // 1. waterfall
  if(t.waterfall.available){
    const w=t.waterfall;
    $("wf-sub").textContent=
      `${w.prev_month} ${aud(w.prev_total_cents)} → ${w.cur_month} ${aud(w.cur_total_cents)} (${pm(w.cur_total_cents-w.prev_total_cents)})`;
    const steps=[{name:"prev",label:w.prev_month,v:w.prev_total_cents}];
    let running=w.prev_total_cents;
    const bars=[{base:0,val:w.prev_total_cents,color:th.faint,label:w.prev_month}];
    for(const m of w.movers){
      const base=m.delta_cents>=0?running:running+m.delta_cents;
      bars.push({base,val:Math.abs(m.delta_cents),
        color:m.delta_cents>=0?th.warn:th.ok,label:catName(m.category)});
      running+=m.delta_cents;
    }
    if(w.other_delta_cents){
      const base=w.other_delta_cents>=0?running:running+w.other_delta_cents;
      bars.push({base,val:Math.abs(w.other_delta_cents),
        color:th.muted,label:"everything else"});
      running+=w.other_delta_cents;
    }
    bars.push({base:0,val:w.cur_total_cents,color:th.accent,label:w.cur_month});
    if(wfChart)wfChart.dispose();
    wfChart=echarts.init($("wfchart"));
    wfChart.setOption({...baseOpts(th),
      grid:{left:8,right:8,top:10,bottom:44,containLabel:true},
      xAxis:{type:"category",data:bars.map(b=>b.label),
        axisLine:{lineStyle:{color:th.line}},axisTick:{show:false},
        axisLabel:{color:th.muted,fontSize:10.5,interval:0,rotate:28}},
      yAxis:{type:"value",splitLine:{lineStyle:{color:th.line}},
        axisLabel:{color:th.muted,fontSize:11,formatter:v=>aud(v)}},
      tooltip:{...baseOpts(th).tooltip,
        formatter:p=>`<b>${p.name}</b><br>${aud(bars[p.dataIndex].val)}`},
      series:[
        {type:"bar",stack:"wf",data:bars.map(b=>b.base),
         itemStyle:{color:"transparent"},tooltip:{show:false},barMaxWidth:34},
        {type:"bar",stack:"wf",data:bars.map((b,i)=>({value:b.val,
          itemStyle:{color:b.color,borderRadius:3}})),barMaxWidth:34}],
    });
  } else $("tr-waterfall").innerHTML+=`<div class="trempty">${esc(t.waterfall.reason)}</div>`;
  // 5. momentum
  if(t.momentum.available){
    const mk=x=>`<div class="trrow"><span class="who">${esc(catName(x.category))}</span>
      <span class="amt ${x.delta_cents>0?"up":"dn"}">${x.delta_cents>0?"▲":"▼"} ${pm(x.delta_cents)}/mo${x.pct!=null?` (${x.pct>0?"+":""}${x.pct}%)`:""}</span></div>`;
    $("momentum").innerHTML=
      (t.momentum.heating.length?t.momentum.heating.map(mk).join(""):"")+
      (t.momentum.cooling.length?t.momentum.cooling.map(mk).join(""):"")||
      `<div class="trempty">no meaningful movement</div>`;
  }
  // 3. price creep
  if(t.price_creep.available){
    const c=t.price_creep;
    $("creep-sub").innerHTML=c.items.length
      ?`your recurring base moved <b class="${c.total_annualised_cents>0?"up":"dn"}">${pm(c.total_annualised_cents)}/yr</b>`
      :"";
    $("creep").innerHTML=c.items.length?c.items.map(i=>`<div class="trrow">
      <span class="who">${esc(i.merchant)} <span style="color:var(--faint)">(${i.cadence})</span></span>
      <span class="amt">${aud(i.early_cents)} → ${aud(i.late_cents)}
        <b class="${i.delta_cents>0?"up":"dn"}">${pm(i.annualised_cents)}/yr</b></span></div>`).join("")
      :`<div class="trempty">no recurring price changes detected</div>`;
  }
  // 6. anomalies
  if(t.anomalies.available){
    $("anomalies").innerHTML=t.anomalies.flags.length?t.anomalies.flags.map(f=>
      `<div class="trrow"><span class="who">${esc(catName(f.category))}
        <span style="color:var(--faint)">— ${f.ratio}× typical${f.culprits[0]?`, driven by ${esc(f.culprits[0].merchant)} ${aud(f.culprits[0].cents)} on ${f.culprits[0].date}`:""}</span></span>
       <span class="amt up">${aud(f.cents)} <span style="color:var(--faint)">vs ${aud(f.typical_cents)}</span></span></div>`).join("")
      :`<div class="trempty">nothing unusual — last month looked like you</div>`;
  }
  // 4. pareto
  if(t.pareto.available){
    const p=t.pareto;
    $("pareto-sub").textContent=`last ${p.days} days of variable (non-recurring) spend — ${aud(p.variable_total_cents)} total`;
    $("pareto").innerHTML=p.merchants.map(m=>`<div class="trrow">
      <span class="who">${esc(m.merchant)} <span class="spark">${spark(m.monthly_cents)}</span></span>
      <span class="amt">${aud(m.cents)} <span style="color:var(--faint)">(${Math.round(m.share*100)}%, cum ${Math.round(m.cum_share*100)}%)</span></span></div>`).join("");
  }
  // 7. wins
  if(t.wins.available){
    const w=t.wins;
    $("wins-sub").innerHTML=w.wins.length
      ?`locked in <b class="dn">${aud(w.total_annualised_cents)}/yr</b> so far`:"";
    $("wins").innerHTML=w.wins.length?w.wins.map(x=>`<div class="trrow">
      <span class="who">${x.kind==="cancelled_recurring"
        ?`${esc(x.merchant)} <span style="color:var(--faint)">— stopped (last seen ${x.last_seen})</span>`
        :`${esc(catName(x.category))} <span style="color:var(--faint)">— held ${aud(x.new_monthly_cents)}/mo, was ${aud(x.old_monthly_cents)}</span>`}</span>
      <span class="amt dn">${aud(x.annualised_cents)}/yr</span></div>`).join("")
      :`<div class="trempty">wins land here when a recurring charge stops or a category stays down 3+ months</div>`;
  }
  // 8. fixed vs variable floor
  if(t.fixed_floor.available){
    const f=t.fixed_floor;
    if(floorChart)floorChart.dispose();
    floorChart=echarts.init($("floorchart"));
    floorChart.setOption({...baseOpts(th),
      grid:{left:8,right:8,top:26,bottom:24,containLabel:true},
      legend:{top:0,textStyle:{color:th.muted,fontSize:11},
        itemWidth:14,itemHeight:9},
      xAxis:{type:"category",data:f.months.map(m=>fmtBucket(m,"month")),
        axisLine:{lineStyle:{color:th.line}},axisTick:{show:false},
        axisLabel:{color:th.muted,fontSize:10.5}},
      yAxis:{type:"value",splitLine:{lineStyle:{color:th.line}},
        axisLabel:{color:th.muted,fontSize:11,formatter:v=>aud(v*100)}},
      tooltip:{...baseOpts(th).tooltip,trigger:"axis"},
      series:[
        {name:"recurring (fixed)",type:"line",stack:"f",areaStyle:{opacity:.55},
         symbol:"none",smooth:true,lineStyle:{width:1},
         itemStyle:{color:th.accent},
         data:f.fixed_cents.map(c=>Math.round(c/100))},
        {name:"variable",type:"line",stack:"f",areaStyle:{opacity:.25},
         symbol:"none",smooth:true,lineStyle:{width:1},
         itemStyle:{color:th.muted},
         data:f.variable_cents.map(c=>Math.round(c/100))}],
    });
  }
}
function theme(){return{fg:cssVar("--fg"),muted:cssVar("--muted"),faint:cssVar("--faint"),
  line:cssVar("--line"),accent:cssVar("--accent"),card2:cssVar("--card2"),
  ok:cssVar("--ok")||"#4cc38a",warn:cssVar("--warn-fg")||"#e5c07b"}}
$("range").addEventListener("click",e=>{
  const b=e.target.closest("button");if(!b)return;
  for(const x of $("range").children)x.classList.toggle("on",x===b);
  state.days=+b.dataset.d;localStorage.setItem("sg-days",state.days);
  state.cat=null;state.sub=null;load();
});
$("bucketsel").addEventListener("click",e=>{
  const b=e.target.closest("button");if(!b)return;
  for(const x of $("bucketsel").children)x.classList.toggle("on",x===b);
  state.bucket=b.dataset.b;localStorage.setItem("sg-bucket",state.bucket);
  load();
});
(function initSegs(){
  for(const x of $("range").children)
    x.classList.toggle("on",+x.dataset.d===state.days);
  for(const x of $("bucketsel").children)
    x.classList.toggle("on",x.dataset.b===state.bucket);
})();
$("inc-transfers").addEventListener("change",()=>{
  state.transfers=$("inc-transfers").checked;state.cat=null;state.sub=null;load()});
window.addEventListener("resize",()=>{for(const c of[spendChart,catChart,wfChart,floorChart])c&&c.resize()});
(function initTheme(){
  const sel=$("theme");sel.value=localStorage.getItem("sg-theme")||"zinc";
  sel.addEventListener("change",()=>{
    const t=sel.value;localStorage.setItem("sg-theme",t);
    if(t==="zinc")delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme=t;
    load();  // charts re-read the theme vars
  });
})();
let CATLABEL={};
fetchJSON("/api/session").then(async s=>{
  if(!s.authenticated){location.href="/";return}
  await loadTaxonomy();
  load();
  renderTrends();   // range-independent: computed from full history
  renderThemes();
  renderSubs();
});
$("theme").addEventListener("change",()=>setTimeout(renderTrends,50));
</script></body></html>
"""


if __name__ == "__main__":
    main()
