"""Merchant lookup — attribution with a web-searching agent, low-friction.

The enrichment pass (enrich.py) classifies merchants into categories; this
module answers a different question: WHO is the merchant behind an opaque
bank descriptor ("EFTPOS DEBIT 1234567 EXMPL PH MAIN ST0")? An agent
running against the read-only view of the store calls the Claude API with
Anthropic's server-side web search tool and proposes an identity.

Friction budget (the design constraint): the user never confirms the
obvious. Proposals at or above AUTO_THRESHOLD are applied automatically
with status 'auto' — visible and overridable later, never a queue item.
Only genuinely unclear ones land as 'pending'. Every decision caches in
`merchant_lookups` keyed by the stable merchant key, so a descriptor is
looked up at most once, ever.

Raw transactions stay immutable — identities live in a side table and the
whole table is re-derivable (delete it and re-run).

Run:  .venv/bin/python -m spendglass.lookup --dry-run   # list candidates
      .venv/bin/python -m spendglass.lookup --limit 10  # look up 10
"""

from __future__ import annotations

import json
import re
import sys
import time

from .config import Config
from .store import Store

MODEL = "claude-opus-5"
AUTO_THRESHOLD = 0.8
MAX_SEARCHES_PER_MERCHANT = 3

# Runtime-tunable miner settings, persisted in meta (admin screen edits
# them). Defaults apply when a key is absent.
SETTING_DEFAULTS = {
    "auto_threshold": AUTO_THRESHOLD,  # >= this: auto-apply, else pending
    "max_searches": MAX_SEARCHES_PER_MERCHANT,
    "workers": 4,                      # parallel API calls per run
    "propagation": 1,                  # learn-from-decision pass on/off
    "sync_interval_hours": 6,          # scheduled bank sync + enrich; 0 = off
}


def get_settings(store: Store) -> dict:
    out = dict(SETTING_DEFAULTS)
    for r in store.con.execute("SELECT key, value FROM meta WHERE key LIKE 'miner.%'"):
        k = r["key"][len("miner."):]
        if k in out:
            try:
                out[k] = (float(r["value"])
                          if k in ("auto_threshold", "sync_interval_hours")
                          else int(r["value"]))
            except ValueError:
                pass
    return out


def set_settings(store: Store, updates: dict) -> None:
    for k, v in updates.items():
        if k in SETTING_DEFAULTS:
            store.con.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (f"miner.{k}", str(v)))
    store.con.commit()

# A descriptor is "unclear" when the bank gave us plumbing instead of a name.
# A bare "*" anywhere is a processor/truncation marker ("UBER *TRIP",
# "AMZNPRIMEA*", "DESIGNCO* I00001") — worth resolving to the real business.
_UNCLEAR = re.compile(
    r"^(eftpos debit|debit card purchase|direct debit|osko payment|bpay)\b"
    r"|\(via paypal\)|\b(sq|zeller|ezi)\s*\*|\*",
    re.IGNORECASE,
)

# Not merchants at all — transfers and bank plumbing belong to the transfer
# matcher, not the lookup agent.
_NOT_MERCHANT = re.compile(
    r"^(transfer (to|from)|salary|interest|payment received|credit card payment)",
    re.IGNORECASE,
)

_LOOKUPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS merchant_lookups (
  merchant_key TEXT PRIMARY KEY,
  proposed_name TEXT,            -- agent's best canonical business name
  proposed_summary TEXT,         -- one line: what this business is
  proposed_subcategory TEXT,     -- specific business type ("Hair salon")
  confidence REAL,               -- agent self-report, 0-1
  evidence_url TEXT,
  status TEXT NOT NULL CHECK (status IN
    ('auto','pending','approved','overridden','rejected')),
  resolved_name TEXT,            -- name in effect (user's, when overridden)
  resolved_subcategory TEXT,     -- sub-category in effect
  resolved_category TEXT,        -- user's category override (NULL = derived)
  model TEXT, looked_up_at TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS subcategories (
  name TEXT PRIMARY KEY,
  category TEXT NOT NULL         -- strict tree: each subcategory has exactly
);                               -- one parent among the 16 CDR keys
"""


def ensure_schema(store: Store) -> None:
    store.con.executescript(_LOOKUPS_SCHEMA)
    # Columns added after the table first shipped; keyed stores self-upgrade.
    cols = {r["name"] for r in store.con.execute("PRAGMA table_info(merchant_lookups)")}
    for col in ("proposed_subcategory", "resolved_subcategory", "resolved_category"):
        if col not in cols:
            store.con.execute(f"ALTER TABLE merchant_lookups ADD COLUMN {col} TEXT")
    store.con.commit()


# ── candidate selection ─────────────────────────────────────────────────────

def candidates(store: Store) -> list[dict]:
    """Merchants whose identity is unclear and not yet looked up, biggest
    spend first. Unclear = noisy descriptor, or the classifier itself was
    unsure (its uncertainty is a cheap proxy for 'who even is this')."""
    ensure_schema(store)
    rows = store.con.execute(
        """SELECT m.* FROM merchants m
           LEFT JOIN merchant_lookups l ON l.merchant_key = m.key
           WHERE l.merchant_key IS NULL"""
    ).fetchall()
    def unclear(m) -> bool:
        if _NOT_MERCHANT.search(m["display_name"] or ""):
            return False
        # Display names are cleaned by the normaliser now, so the plumbing
        # signals live in the raw sample descriptions.
        texts = [m["display_name"] or "", *json.loads(m["sample_descriptions"] or "[]")]
        return (
            any(_UNCLEAR.search(t) for t in texts)
            or m["llm_category"] is None
            or (m["llm_confidence"] is not None and m["llm_confidence"] < 0.7)
        )

    out = [dict(m) for m in rows if unclear(m)]
    out.sort(key=lambda m: abs(m["total_debit_cents"] or 0), reverse=True)
    return out


# ── the lookup agent ────────────────────────────────────────────────────────

def known_subcategories(store: Store) -> list[str]:
    """Existing taxonomy with parent categories, most-used first — fed to
    the agent so the set grows deliberately instead of sprawling."""
    ensure_schema(store)
    rows = store.con.execute(
        """SELECT s.name || ' [' || s.category || ']' AS lbl,
                  COUNT(l.merchant_key) n
           FROM subcategories s
           LEFT JOIN merchant_lookups l
             ON COALESCE(l.resolved_subcategory, l.proposed_subcategory) = s.name
           GROUP BY s.name ORDER BY n DESC, s.name""").fetchall()
    if rows:
        return [r["lbl"] for r in rows]
    return [r["s"] for r in store.con.execute(  # tree not built yet
        """SELECT COALESCE(resolved_subcategory, proposed_subcategory) s, COUNT(*) n
           FROM merchant_lookups
           WHERE COALESCE(resolved_subcategory, proposed_subcategory) IS NOT NULL
             AND COALESCE(resolved_subcategory, proposed_subcategory) != ''
           GROUP BY s ORDER BY n DESC, s""")]


def _prompt(m: dict, known_subcats: list[str] | None = None) -> str:
    samples = ", ".join(json.loads(m["sample_descriptions"] or "[]")[:3])
    avg = abs(m["total_debit_cents"] or 0) / max(m["debit_count"] or 1, 1) / 100
    taxonomy = ""
    if known_subcats:
        taxonomy = (
            "\n\nExisting subcategories, with their parent category in "
            "brackets (STRONGLY prefer one of these when it fits; invent a "
            "new one only when none does — and the subcategory must belong "
            "to the category it appears under):\n"
            + ", ".join(known_subcats[:60]))
    return (
        "Identify the real-world business behind this Australian bank "
        "statement descriptor. Processor prefixes (SQ*, PayPal, Zeller, EFTPOS "
        "terminal ids) are plumbing — name the underlying merchant. Use web "
        "search when the descriptor alone isn't conclusive.\n\n"
        f"Descriptor: {m['display_name']}\n"
        f"Other raw descriptions: {samples or '(none)'}\n"
        f"Seen {m['txn_count']} times, typical charge A${avg:.2f}, "
        f"between {m['first_seen']} and {m['last_seen']}\n\n"
        "Respond with ONLY a JSON object, no other text:\n"
        '{"merchant_name": "<canonical business name ONLY — no suburb, city, '
        "state, or country suffix; location belongs in summary. E.g. "
        "'Sample Canteen', not 'Sample Canteen Exampleville'. Empty string "
        'if unidentifiable>", "summary": "<one line: what this business is and '
        'where>", "subcategory": "<specific business type, 2-3 words — e.g. '
        "'Hair salon', 'Skincare clinic', 'Supermarket', 'Bottle shop', "
        "'AI subscription' — empty string if unknown>\", "
        '"confidence": <0-1>, "evidence_url": "<best source URL or null>"}\n\n'
        "Calibrate confidence honestly: 0.8+ only when the identification is "
        "essentially certain (recognisable brand, or search confirms name and "
        "location). A plausible guess is 0.5-0.7. Unknown is below 0.3."
        + taxonomy
    )


def _parse(resp) -> dict | None:
    """Last text block should be bare JSON; tolerate prose around it."""
    texts = [b.text for b in resp.content if b.type == "text" and b.text.strip()]
    for text in reversed(texts):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            p = json.loads(text[start:end + 1])
            return {
                "merchant_name": str(p.get("merchant_name") or "").strip(),
                "summary": str(p.get("summary") or "").strip(),
                "subcategory": str(p.get("subcategory") or "").strip() or None,
                "confidence": max(0.0, min(1.0, float(p.get("confidence", 0)))),
                "evidence_url": p.get("evidence_url") or None,
            }
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return None


def _lookup_one(client, m: dict, known_subcats: list[str] | None = None,
                max_searches: int = MAX_SEARCHES_PER_MERCHANT) -> dict | None:
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        # Refusals are vanishingly unlikely here, but the recommended
        # server-side fallback costs nothing to carry.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        tools=[{
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": max_searches,
        }],
        messages=[{"role": "user", "content": _prompt(m, known_subcats)}],
    )
    return _parse(resp) if resp.stop_reason != "refusal" else None


def lookup_merchants(store: Store, client, limit: int = 10,
                     workers: int | None = None) -> dict:
    """Run the agent over up to `limit` candidates and record proposals.

    `client` is an anthropic.Anthropic (or a test double). Direct calls,
    not the Batch API — results land row-by-row as they arrive, so the UI
    review queue fills while the run is still going. API calls fan out
    across `workers` threads; all writes stay on this thread. A merchant
    whose call errors gets no row, so a later run retries it for free.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ensure_schema(store)
    cfg = get_settings(store)
    threshold = cfg["auto_threshold"]
    if workers is None:
        workers = cfg["workers"]
    todo = candidates(store)[:limit]
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    subcats = known_subcategories(store)  # snapshot once per run
    auto = pending = failed = errored = 0
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_lookup_one, client, m, subcats,
                               cfg["max_searches"]): m for m in todo}
        done = 0
        for fut in as_completed(futures):
            m = futures[fut]
            done += 1
            try:
                p = fut.result()
            except Exception as e:  # rate limit after retries, network, …
                errored += 1
                print(f"[{done}/{len(todo)}] ERROR {m['display_name'][:40]}: {e}",
                      flush=True)
                continue
            if p is None or not p["merchant_name"]:
                failed += 1
                status, p = "pending", p or {
                    "merchant_name": "", "summary": "(agent could not identify)",
                    "subcategory": None, "confidence": 0.0, "evidence_url": None,
                }
            elif p["confidence"] >= threshold:
                status, auto = "auto", auto + 1
            else:
                status, pending = "pending", pending + 1
            store.con.execute(
                """INSERT OR REPLACE INTO merchant_lookups
                   (merchant_key, proposed_name, proposed_summary,
                    proposed_subcategory, confidence, evidence_url, status,
                    resolved_name, resolved_subcategory, model, looked_up_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (m["key"], p["merchant_name"], p["summary"], p["subcategory"],
                 p["confidence"], p["evidence_url"], status,
                 p["merchant_name"] if status == "auto" else None,
                 p["subcategory"] if status == "auto" else None, MODEL, now),
            )
            store.con.commit()  # each result lands as it arrives
            results.append({"descriptor": m["display_name"], "status": status, **p})
            print(f"[{done}/{len(todo)}] {status:8} ({p['confidence']:.2f}) "
                  f"{m['display_name'][:40]:40} → {p['merchant_name'] or '(unknown)'}",
                  flush=True)
    return {"status": "ok", "looked_up": len(todo), "auto": auto,
            "pending": pending, "failed": failed, "errored": errored,
            "results": results}


def decide(store: Store, merchant_key: str, name: str | None,
           subcategory: str | None = None, category: str | None = None,
           approve: bool = True) -> None:
    """Record the user's call on a proposal: approve as-is (name=None),
    override name/subcategory/category, or reject. Applies to every
    transaction under the key — one decision covers all matches."""
    ensure_schema(store)
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    row = store.con.execute(
        "SELECT proposed_name, proposed_subcategory FROM merchant_lookups "
        "WHERE merchant_key=?", (merchant_key,)).fetchone()
    if row is None:
        # Inline correction on a merchant the pipeline never proposed for
        # (e.g. straight from the transaction table) — seed the row.
        if not approve:
            return
        store.con.execute(
            """INSERT INTO merchant_lookups
               (merchant_key, proposed_name, proposed_summary, status, looked_up_at)
               VALUES (?, ?, 'Manually set from the transaction table',
                       'pending', ?)""",
            (merchant_key, name or "", now))
        row = store.con.execute(
            "SELECT proposed_name, proposed_subcategory FROM merchant_lookups "
            "WHERE merchant_key=?", (merchant_key,)).fetchone()
    if not approve:
        status, resolved, sub, cat = "rejected", None, None, None
    else:
        status = "overridden" if (name or subcategory or category) else "approved"
        resolved = name or (row["proposed_name"] if row else None)
        sub = subcategory or (row["proposed_subcategory"] if row else None)
        cat = category  # no proposal fallback — NULL means "keep derived"
    store.con.execute(
        """UPDATE merchant_lookups SET status=?, resolved_name=?,
           resolved_subcategory=?, resolved_category=?, decided_at=?
           WHERE merchant_key=?""",
        (status, resolved, sub, cat, now, merchant_key),
    )
    # A genuinely new subcategory joins the tree under the category decided
    # here (or the merchant's derived one) — the taxonomy grows deliberately.
    if approve and sub and not store.con.execute(
            "SELECT 1 FROM subcategories WHERE name=?", (sub,)).fetchone():
        parent = cat
        if not parent:
            try:
                r2 = store.con.execute(
                    """SELECT COALESCE(llm_category, cdr_category) c
                       FROM merchants WHERE key=?""", (merchant_key,)).fetchone()
                parent = r2["c"] if r2 and r2["c"] else None
            except Exception:  # enrichment aggregates absent
                parent = None
        if parent:
            store.con.execute(
                "INSERT OR IGNORE INTO subcategories (name, category) VALUES (?,?)",
                (sub, parent))
    store.con.commit()


def identify_clear_merchants(store: Store, client, poll_seconds: int = 10,
                             max_wait_seconds: int = 1800) -> dict:
    """Full-coverage sweep: every merchant with NO lookup row yet gets a
    Haiku Batch identification — canonical name + subcategory from the
    descriptor alone (clean names don't need web search; ~cents per
    hundred). Confident results auto-apply under the same threshold;
    everything else lands pending. Resumable like the classifier batch."""
    ensure_schema(store)
    cfg = get_settings(store)
    subcats = known_subcategories(store)
    taxonomy = ", ".join(subcats[:80]) or "(none yet)"

    pending_batch = store.con.execute(
        "SELECT value FROM meta WHERE key='sweep_batch_id'").fetchone()
    if pending_batch:
        batch_id = pending_batch["value"]
    else:
        todo = [dict(m) for m in store.con.execute(
            """SELECT m.* FROM merchants m
               LEFT JOIN merchant_lookups l ON l.merchant_key = m.key
               WHERE l.merchant_key IS NULL""")]
        todo = [m for m in todo if not _NOT_MERCHANT.search(m["display_name"] or "")]
        if not todo:
            return {"status": "ok", "identified": 0, "reason": "full coverage already"}
        fmt = {"type": "json_schema", "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "subcategory": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["name", "subcategory", "confidence"],
            "additionalProperties": False}}
        reqs = []
        for i, m in enumerate(todo):
            samples = ", ".join(json.loads(m["sample_descriptions"] or "[]")[:2])
            reqs.append({
                "custom_id": f"s-{i}",
                "params": {
                    "model": "claude-haiku-4-5", "max_tokens": 250,
                    "output_config": {"format": fmt},
                    "messages": [{"role": "user", "content":
                        "Australian bank statement merchant. Return the canonical "
                        "business name (no suburb/state/country suffix; keep it as "
                        "the descriptor if you genuinely can't tell) and the best "
                        "subcategory from this taxonomy (subcategory must belong "
                        "to the merchant's category, shown in brackets; empty "
                        "string if none fits):\n" + taxonomy
                        + f"\n\nDescriptor: {m['display_name']}\n"
                        f"Raw samples: {samples or '(none)'}\n"
                        f"Category: {m['llm_category'] or m['cdr_category'] or '?'}\n"
                        "confidence: 0-1 that the NAME identification is right."}],
                },
            })
        store.con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('sweep_batch_map', ?)",
            (json.dumps({f"s-{i}": m["key"] for i, m in enumerate(todo)}),))
        batch = client.messages.batches.create(requests=reqs)
        batch_id = batch.id
        store.con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('sweep_batch_id', ?)",
            (batch_id,))
        store.con.commit()

    waited = 0
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            break
        if waited >= max_wait_seconds:
            return {"status": "pending", "batch_id": batch_id,
                    "reason": "batch still processing — re-run to resume"}
        time.sleep(poll_seconds)
        waited += poll_seconds

    key_map = json.loads(store.con.execute(
        "SELECT value FROM meta WHERE key='sweep_batch_map'").fetchone()["value"])
    threshold = cfg["auto_threshold"]
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    auto = pend = errored = 0
    for result in client.messages.batches.results(batch_id):
        mkey = key_map.get(result.custom_id)
        if mkey is None or result.result.type != "succeeded":
            errored += 1
            continue
        text = next((blk.text for blk in result.result.message.content
                     if blk.type == "text"), "")
        try:
            p = json.loads(text)
            name = str(p["name"]).strip()
            sub = str(p.get("subcategory") or "").strip() or None
            conf = max(0.0, min(1.0, float(p["confidence"])))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            errored += 1
            continue
        status = "auto" if (name and conf >= threshold) else "pending"
        cur = store.con.execute(
            "INSERT OR IGNORE INTO merchant_lookups "
            "(merchant_key, proposed_name, proposed_summary, proposed_subcategory,"
            " confidence, status, resolved_name, resolved_subcategory, model,"
            " looked_up_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mkey, name, "Identified from descriptor (no web search)", sub, conf,
             status, name if status == "auto" else None,
             sub if status == "auto" else None, "claude-haiku-4-5 sweep", now))
        if cur.rowcount:
            auto += status == "auto"
            pend += status == "pending"
            if status == "auto" and sub and not store.con.execute(
                    "SELECT 1 FROM subcategories WHERE name=?", (sub,)).fetchone():
                r2 = store.con.execute(
                    """SELECT COALESCE(llm_category, cdr_category) c FROM merchants
                       WHERE key=?""", (mkey,)).fetchone()
                if r2 and r2["c"]:
                    store.con.execute(
                        "INSERT OR IGNORE INTO subcategories (name, category) "
                        "VALUES (?,?)", (sub, r2["c"]))
    store.con.execute(
        "DELETE FROM meta WHERE key IN ('sweep_batch_id','sweep_batch_map')")
    store.con.commit()
    return {"status": "ok", "auto": auto, "pending": pend, "errored": errored,
            "batch_id": batch_id}


def propagate_decisions(store: Store, client, decided: list[dict]) -> int:
    """After user decisions, ask a small model which remaining pending
    proposals those decisions inform (same brand, payment platform, or
    descriptor pattern) and refresh those proposals in place. Nothing is
    auto-approved — the rows stay pending, the user just gets better
    guesses to click on."""
    ensure_schema(store)
    pend = [dict(r) for r in store.con.execute(
        """SELECT merchant_key, proposed_name, proposed_summary,
                  proposed_subcategory
           FROM merchant_lookups WHERE status='pending'""")]
    if not pend or not decided:
        return 0
    resp = client.messages.create(
        model="claude-haiku-4-5", max_tokens=2000,
        messages=[{"role": "user", "content":
            "A user just confirmed these merchant identities in their "
            "personal-finance app:\n"
            + json.dumps(decided, indent=1)
            + "\n\nThese merchant proposals are still unresolved:\n"
            + json.dumps(pend, indent=1)
            + "\n\nWhich unresolved ones do the confirmed decisions directly "
            "inform — same business, same chain/brand, same payment platform, "
            "or the same descriptor pattern? Be conservative: only include a "
            "row when the confirmed decision genuinely tells you something "
            "about it. Respond with ONLY a JSON array, [] if none:\n"
            '[{"merchant_key": "...", "name": "...", "subcategory": "...", '
            '"reason": "<one short line>", "confidence": <0-1>}]'}],
    )
    text = next((b.text for b in resp.content if b.type == "text" and b.text), "")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return 0
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return 0
    keys = {p["merchant_key"] for p in pend}
    updated = 0
    for it in items if isinstance(items, list) else []:
        key = str(it.get("merchant_key") or "")
        name = str(it.get("name") or "").strip()
        sub = str(it.get("subcategory") or "").strip()
        reason = str(it.get("reason") or "").strip()
        if key not in keys or not (name or sub):
            continue
        store.con.execute(
            """UPDATE merchant_lookups SET
                 proposed_name=COALESCE(NULLIF(?,''), proposed_name),
                 proposed_subcategory=COALESCE(NULLIF(?,''), proposed_subcategory),
                 proposed_summary=? , confidence=?
               WHERE merchant_key=? AND status='pending'""",
            (name, sub,
             f"Inferred from your related decision: {reason}" if reason
             else "Inferred from a related decision",
             max(0.0, min(1.0, float(it.get("confidence") or 0.5))), key),
        )
        updated += 1
    store.con.commit()
    return updated


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 10
    cfg = Config.load()
    with Store(cfg.db_path) as store:
        if "--sweep" in args:
            import os

            from .config import REPO_ROOT, _parse_env_file
            api_key = os.environ.get("ANTHROPIC_API_KEY") or _parse_env_file(
                REPO_ROOT / ".env").get("ANTHROPIC_API_KEY")
            if not api_key:
                print("no ANTHROPIC_API_KEY — sweep needs one")
                return 1
            import anthropic
            result = identify_clear_merchants(
                store, anthropic.Anthropic(api_key=api_key))
            print("sweep:", json.dumps(result))
            return 0 if result["status"] in ("ok", "pending") else 1
        todo = candidates(store)
        print(f"candidates={len(todo)}")
        if "--dry-run" in args:
            for m in todo[:limit]:
                spend = abs(m["total_debit_cents"] or 0) / 100
                print(f"  A${spend:>9,.2f} {m['txn_count']:>3}x  {m['display_name']}")
            return 0
        import os
        from .config import _parse_env_file, REPO_ROOT
        api_key = os.environ.get("ANTHROPIC_API_KEY") or _parse_env_file(
            REPO_ROOT / ".env").get("ANTHROPIC_API_KEY")
        if not api_key:
            print("no ANTHROPIC_API_KEY — lookup needs one (keyless degradation: none)")
            return 1
        import anthropic
        result = lookup_merchants(store, anthropic.Anthropic(api_key=api_key),
                                  limit=limit)
        for r in result["results"]:
            flag = {"auto": "✓ auto", "pending": "? pending"}[r["status"]]
            print(f"{flag:10} ({r['confidence']:.2f}) {r['descriptor'][:44]:44} "
                  f"→ {r['merchant_name'] or '(unknown)'}")
        print(json.dumps({k: v for k, v in result.items() if k != "results"}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
