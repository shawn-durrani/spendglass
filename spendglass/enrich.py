"""Enrichment — derivation, not judgement.

Three passes, in order of how much they can be trusted:

1. DETERMINISTIC (always runs, no key, no model):
   - merchant normalisation → a `merchants` table aggregating every unique
     merchant with stats, dominant CDR category, and sample descriptions
   - recurring-charge detection: interval regularity per merchant, pure
     date/int arithmetic — a subscription is a fact, not an opinion
2. CLASSIFICATION (needs ANTHROPIC_API_KEY; degrades to nothing without it):
   - unique MERCHANTS (never per-transaction) that lack a cached label are
     classified into the 16 CDR category keys by claude-haiku-4-5 via the
     Batch API (50% off; nothing here is latency-sensitive), constrained by
     structured outputs so a label is always a valid key. Results cache in
     `merchants` — a repeat merchant never costs twice.
3. Month-over-month deltas are served live from queries.py — sums in SQL,
   integer subtraction only; nothing to materialise.

Scope boundary (issue #4): spending only. Holdings and trades get no
advisory derivation here, by design.

Run:  .venv/bin/python -m spendglass.enrich            # deterministic only
      .venv/bin/python -m spendglass.enrich --classify # + Haiku batch
"""

from __future__ import annotations

import json
import re
import statistics
import sys
import time
from datetime import date, timedelta

from .config import Config
from .store import Store

MODEL = "claude-haiku-4-5"

# Cadence buckets: (name, min_days, max_days, interval_tolerance_days)
CADENCES = [
    ("weekly", 6, 8, 2),
    ("fortnightly", 12, 16, 3),
    ("monthly", 26, 35, 5),
    ("quarterly", 80, 100, 10),
    ("yearly", 350, 385, 15),
]
MIN_OCCURRENCES = 3

_NOISE = re.compile(
    r"^(sq\s*\*|paypal\s*\*|pp\*|sp\s+|zeller\s*\*|ezi\s*\*)|[*#]\s*\d+\s*$|\s+\d{4,}\s*$",
    re.IGNORECASE,
)
# More plumbing that is never identity: transaction-type prefixes the banks
# prepend ("DEBIT CARD PURCHASE GROCER…"), phone numbers processors smuggle
# into descriptors (PayPal famously appends its publicly documented support
# line), "(via PayPal)"-style rail markers, and trailing country tags.
_TXN_PREFIX = re.compile(
    r"^(eftpos debit|debit card purchase|direct debit|osko payment|osko withdrawal"
    r"|withdrawal(?:[- ](?:mobile|online|osko payment))?"
    r"|deposit(?:[- ](?:online|osko|salary))?"
    r"|payment by authority to)"
    r"\s*\d*\s*",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\+?\d{1,3}[\s.-]?\(\d{2,4}\)[\s.-]?\d{3}[\s.-]?\d{3,4}|\+\d{7,15}")
_VIA = re.compile(r"\(via\s+[a-z0-9 ]+\)", re.IGNORECASE)
_COUNTRY = re.compile(r"\s+(aus?|usa)\s*$", re.IGNORECASE)
# Card-scheme currency tails make every foreign charge its own "merchant":
# "EXAMPLE SAAS …, ##0101 12.34 US DOLLAR", "… USD 23.45 incl. Foreign
# Transaction Fee AUD $0.99". Reference + amount + currency: all noise.
_CCY_TAIL = re.compile(
    r",?\s*#{2,}\d+.*$"
    r"|\s+usd\s+\d+(?:\.\d+)?\b.*$"
    r"|\s+\d+(?:\.\d+)?\s+us\s+dollar\b.*$",
    re.IGNORECASE,
)
# EFTPOS descriptors end in a dd/mm transaction date and sometimes a stray
# backslash ("SAMPLE CANTEEN Exampleville 01/01", "SAMPLE LANE\") — dates
# and escapes are not identity either.
_TRAIL_DATE = re.compile(r"\s+\d{1,2}/\d{1,2}\s*$|\\+\s*$")
_SPACES = re.compile(r"\s+")


def strip_noise(text: str) -> str:
    """Case-preserving label cleaner shared by keys and display names.
    Deliberately conservative — a wrong merge poisons aggregates, a missed
    merge just splits one merchant in two."""
    base = text.strip()
    prev = None
    while prev != base:
        prev = base
        for rx in (_TXN_PREFIX, _NOISE, _PHONE, _VIA, _CCY_TAIL, _COUNTRY, _TRAIL_DATE):
            base = rx.sub("", base).strip()
    return _SPACES.sub(" ", base).strip(" ,-")


def merchant_key(merchant_name: str | None, description: str | None) -> str:
    """Stable key for 'the same merchant': strip_noise + casefold."""
    return strip_noise(merchant_name or description or "").casefold() or "(unknown)"


_MERCHANTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
  key TEXT PRIMARY KEY,
  display_name TEXT,
  txn_count INTEGER, debit_count INTEGER,
  total_debit_cents INTEGER, first_seen TEXT, last_seen TEXT,
  cdr_category TEXT,           -- dominant CDR category from the bank feed
  sample_descriptions TEXT,    -- up to 3, JSON, for the classification prompt
  llm_category TEXT,           -- cached Haiku label (one of the 16 CDR keys)
  llm_confidence REAL, llm_model TEXT, llm_classified_at TEXT
);
CREATE TABLE IF NOT EXISTS recurring_charges (
  merchant_key TEXT PRIMARY KEY,
  display_name TEXT, cadence TEXT, occurrences INTEGER,
  typical_amount TEXT, amount_min TEXT, amount_max TEXT,
  first_seen TEXT, last_seen TEXT, next_expected TEXT,
  median_interval_days REAL
);
"""


def ensure_schema(store: Store) -> None:
    store.con.executescript(_MERCHANTS_SCHEMA)
    # Materialised merchant key on transactions: lets SQL join/sort against
    # merchants and merchant_lookups. Derived, refreshed by rebuild_merchants.
    cols = {r["name"] for r in store.con.execute("PRAGMA table_info(transactions)")}
    if "merchant_key" not in cols:
        store.con.execute("ALTER TABLE transactions ADD COLUMN merchant_key TEXT")
    store.con.execute(
        "CREATE INDEX IF NOT EXISTS idx_txn_merchant_key ON transactions(merchant_key)")
    store.con.commit()


# ── pass 1a: the merchants table ────────────────────────────────────────────

def rebuild_merchants(store: Store) -> int:
    """Refresh aggregates for every merchant; PRESERVE cached llm_* columns."""
    ensure_schema(store)
    rows = store.con.execute(
        """SELECT id, merchant_name, description, amount_cents, direction, date, category
           FROM transactions ORDER BY date"""
    ).fetchall()
    agg: dict[str, dict] = {}
    txn_keys: list[tuple[str, str]] = []
    for r in rows:
        key = merchant_key(r["merchant_name"], r["description"])
        txn_keys.append((key, r["id"]))
        m = agg.setdefault(key, {
            "names": {}, "cats": {}, "samples": [], "txn": 0, "debit": 0,
            "debit_cents": 0, "first": r["date"], "last": r["date"],
        })
        display = strip_noise(r["merchant_name"] or r["description"] or "") or key
        m["names"][display] = m["names"].get(display, 0) + 1
        if r["category"]:
            m["cats"][r["category"]] = m["cats"].get(r["category"], 0) + 1
        if len(m["samples"]) < 3 and r["description"] and r["description"] not in m["samples"]:
            m["samples"].append(r["description"])
        m["txn"] += 1
        if r["direction"] == "debit":
            m["debit"] += 1
            m["debit_cents"] += r["amount_cents"] or 0
        m["first"] = min(m["first"], r["date"] or m["first"])
        m["last"] = max(m["last"], r["date"] or m["last"])
    for key, m in agg.items():
        store.con.execute(
            """INSERT INTO merchants
               (key, display_name, txn_count, debit_count, total_debit_cents,
                first_seen, last_seen, cdr_category, sample_descriptions)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 display_name=excluded.display_name, txn_count=excluded.txn_count,
                 debit_count=excluded.debit_count,
                 total_debit_cents=excluded.total_debit_cents,
                 first_seen=excluded.first_seen, last_seen=excluded.last_seen,
                 cdr_category=excluded.cdr_category,
                 sample_descriptions=excluded.sample_descriptions""",
            (
                key, max(m["names"], key=m["names"].get), m["txn"], m["debit"],
                m["debit_cents"], m["first"], m["last"],
                max(m["cats"], key=m["cats"].get) if m["cats"] else None,
                json.dumps(m["samples"]),
            ),
        )
    # Keys are derived; when the normaliser improves, rows under old keys
    # must go or every merchant view double-counts. Cached llm_* under a
    # renamed key is re-earned on the next --classify run.
    store.con.executemany(
        "UPDATE transactions SET merchant_key=? WHERE id=?", txn_keys)
    store.con.execute("CREATE TEMP TABLE IF NOT EXISTS live_keys (key TEXT PRIMARY KEY)")
    store.con.execute("DELETE FROM live_keys")
    store.con.executemany("INSERT INTO live_keys VALUES (?)", [(k,) for k in agg])
    store.con.execute("DELETE FROM merchants WHERE key NOT IN (SELECT key FROM live_keys)")
    store.con.commit()
    return len(agg)


# ── pass 1b: recurring detection (pure arithmetic) ──────────────────────────

def detect_recurring(store: Store) -> int:
    """A charge is recurring when its intervals keep a steady beat: >=3 debits
    from one merchant whose median interval lands in a cadence bucket and
    whose intervals stay within that bucket's tolerance. Amounts may vary
    (utilities do); the variability is reported, not judged."""
    ensure_schema(store)
    store.con.execute("DELETE FROM recurring_charges")  # derived table: rebuild
    rows = store.con.execute(
        """SELECT merchant_name, description, date, amount_cents
           FROM transactions WHERE direction='debit' AND date IS NOT NULL
           ORDER BY date"""
    ).fetchall()
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(merchant_key(r["merchant_name"], r["description"]), []).append(r)
    found = 0
    for key, txns in series.items():
        if len(txns) < MIN_OCCURRENCES:
            continue
        dates = [date.fromisoformat(t["date"]) for t in txns]
        intervals = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if len(intervals) < MIN_OCCURRENCES - 1:
            continue
        med = statistics.median(intervals)
        cadence = next(
            (name for name, lo, hi, tol in CADENCES
             if lo <= med <= hi
             and sum(1 for i in intervals if abs(i - med) <= tol) / len(intervals) >= 0.7),
            None,
        )
        if cadence is None:
            continue
        cents = [t["amount_cents"] or 0 for t in txns]
        med_cents = int(statistics.median(cents))
        display = strip_noise(txns[-1]["merchant_name"] or txns[-1]["description"] or "") or key
        store.con.execute(
            """INSERT OR REPLACE INTO recurring_charges
               (merchant_key, display_name, cadence, occurrences, typical_amount,
                amount_min, amount_max, first_seen, last_seen, next_expected,
                median_interval_days)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, display, cadence, len(txns),
                f"{med_cents / 100:.2f}", f"{min(cents) / 100:.2f}",
                f"{max(cents) / 100:.2f}", dates[0].isoformat(),
                dates[-1].isoformat(),
                (dates[-1] + timedelta(days=round(med))).isoformat(), med,
            ),
        )
        found += 1
    store.con.commit()
    return found


# ── pass 2: merchant classification (Haiku, Batch API, cached forever) ──────

_SCHEMA_FMT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string"},   # enum injected at call time
            "confidence": {"type": "number"},
        },
        "required": ["category", "confidence"],
        "additionalProperties": False,
    },
}


def _prompt(m: dict, labels: dict[str, str]) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in labels.items())
    samples = ", ".join(json.loads(m["sample_descriptions"] or "[]")[:3])
    return (
        "Classify this Australian merchant into exactly one spending category.\n\n"
        f"Merchant: {m['display_name']}\n"
        f"Sample bank descriptions: {samples or '(none)'}\n"
        f"Typical spend: ${abs(m['total_debit_cents'] or 0) / max(m['debit_count'] or 1, 1) / 100:.2f} "
        f"per transaction across {m['debit_count']} debits\n\n"
        f"Categories:\n{cats}\n\n"
        "Pick the single best category key. confidence is 0-1."
    )


def classify_merchants(store: Store, client, poll_seconds: int = 10,
                       max_wait_seconds: int = 900) -> dict:
    """Submit unclassified merchants to the Batch API and cache the labels.

    `client` is an anthropic.Anthropic (or a test double). Returns a summary;
    if the batch outlives max_wait_seconds the batch id is persisted in meta
    and a later run resumes it instead of paying again.
    """
    ensure_schema(store)
    labels = {r["key"]: r["label"] for r in store.con.execute(
        "SELECT key, label FROM categories ORDER BY key")}
    if not labels:
        return {"status": "skipped", "reason": "no categories synced yet"}
    valid = set(labels)
    _SCHEMA_FMT["schema"]["properties"]["category"]["enum"] = sorted(valid)

    # Resume a previously-submitted batch rather than re-paying for it.
    pending = store.con.execute(
        "SELECT value FROM meta WHERE key='enrich_batch_id'").fetchone()
    if pending:
        batch_id = pending["value"]
    else:
        todo = store.con.execute(
            "SELECT * FROM merchants WHERE llm_category IS NULL ORDER BY key"
        ).fetchall()
        if not todo:
            return {"status": "ok", "classified": 0, "reason": "nothing unclassified"}
        requests = [
            {
                "custom_id": f"m-{i}",
                "params": {
                    "model": MODEL,
                    "max_tokens": 200,
                    "output_config": {"format": _SCHEMA_FMT},
                    "messages": [{"role": "user", "content": _prompt(dict(m), labels)}],
                },
            }
            for i, m in enumerate(todo)
        ]
        # custom_id -> merchant key mapping persisted alongside the batch id,
        # so a resume after restart can still route results.
        store.con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('enrich_batch_map', ?)",
            (json.dumps({f"m-{i}": m["key"] for i, m in enumerate(todo)}),),
        )
        batch = client.messages.batches.create(requests=requests)
        batch_id = batch.id
        store.con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('enrich_batch_id', ?)",
            (batch_id,),
        )
        store.con.commit()

    waited = 0
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            break
        if waited >= max_wait_seconds:
            return {"status": "pending", "batch_id": batch_id,
                    "reason": "batch still processing — re-run to resume, nothing is re-billed"}
        time.sleep(poll_seconds)
        waited += poll_seconds

    key_map = json.loads(store.con.execute(
        "SELECT value FROM meta WHERE key='enrich_batch_map'").fetchone()["value"])
    classified = errored = invalid = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    for result in client.messages.batches.results(batch_id):
        mkey = key_map.get(result.custom_id)
        if mkey is None or result.result.type != "succeeded":
            errored += 1
            continue
        text = next((blk.text for blk in result.result.message.content
                     if blk.type == "text"), "")
        try:
            payload = json.loads(text)
            category, confidence = payload["category"], float(payload["confidence"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            invalid += 1
            continue
        if category not in valid:
            invalid += 1
            continue
        store.con.execute(
            """UPDATE merchants SET llm_category=?, llm_confidence=?,
               llm_model=?, llm_classified_at=? WHERE key=?""",
            (category, confidence, MODEL, now, mkey),
        )
        classified += 1
    store.con.execute("DELETE FROM meta WHERE key IN ('enrich_batch_id','enrich_batch_map')")
    store.con.commit()
    return {"status": "ok", "classified": classified, "errored": errored,
            "invalid": invalid, "batch_id": batch_id}


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cfg = Config.load()
    from .transfers import match_transfers
    with Store(cfg.db_path) as store:
        merchants = rebuild_merchants(store)
        recurring = detect_recurring(store)
        links = match_transfers(store)
        print(f"merchants={merchants} recurring={recurring} "
              f"internal_transfer_pairs={links['pairs']}")
        if "--classify" not in args:
            print("(deterministic passes only — add --classify to run the Haiku batch)")
            return 0
        import os
        from .config import _parse_env_file, REPO_ROOT
        api_key = os.environ.get("ANTHROPIC_API_KEY") or _parse_env_file(
            REPO_ROOT / ".env").get("ANTHROPIC_API_KEY")
        if not api_key:
            print("no ANTHROPIC_API_KEY — classification skipped; CDR categories "
                  "and heuristics still apply (keyless degradation)")
            return 0
        import anthropic
        result = classify_merchants(store, anthropic.Anthropic(api_key=api_key))
        print("classify:", json.dumps(result))
        return 0 if result["status"] in ("ok", "pending") else 1


if __name__ == "__main__":
    raise SystemExit(main())
