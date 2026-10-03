"""Category overrides — deterministic, user-editable rules that correct
known bank-category mislabelling (issue #90) without ever touching a raw
transaction row.

A rule only changes what the EFFECTIVE category resolves to at read time —
the same idea as `merchant_lookups.resolved_category`, but keyed on a LIKE
pattern instead of one exact merchant, so it keeps working however a bank
renders a descriptor ("BWS 0412 FAIRHAVEN" today, something else tomorrow).
Delete a rule (or set an exact per-merchant override via the lookup queue,
which still wins) and the transaction reads exactly as it synced — nothing
here is destructive or hard to reverse.

Seeded once, the moment this module's schema first lands (tracked by a meta
flag, same device store.py uses for schema_version): a deleted or edited
built-in rule stays that way on every later restart, same promise themes.py
makes for theme rules.
"""

from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS category_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK (kind IN ('merchant', 'description')),
  pattern TEXT NOT NULL,         -- SQL LIKE pattern (merchant_key or description)
  category TEXT NOT NULL,        -- override category key, also cached in `categories`
  subcategory TEXT,              -- optional override subcategory
  note TEXT NOT NULL DEFAULT '',
  UNIQUE (kind, pattern, category)
);
"""

# Built-in fixes for issue #90's known mislabelling. `kind='description'`
# matches the raw bank description (loan/card interest lines carry no real
# merchant); `kind='merchant'` matches the normalised merchant_key, which
# survives whatever suburb/terminal-id suffix a bank tacks onto a brand.
BUILTIN_RULES: list[tuple[str, str, str, str, str]] = [
    ("description", "%interest charged%", "LOAN_INTEREST", "Mortgage Interest",
     "Loan interest lands as BANK_FEES on the raw feed."),
    ("description", "%interest on cash adv%", "LOAN_INTEREST", "Card Interest",
     "Credit-card cash-advance interest lands as BANK_FEES on the raw feed."),
    ("merchant", "%liquorland%", "ALCOHOL", "Bottle Shop",
     "Bottle shop lands as Eating Out on the raw feed."),
    # Word-anchored, not "%bws%": three letters inside a longer word must
    # not reclassify a merchant to ALCOHOL (same device as the Sport &
    # Fitness theme's "bike" patterns, #114).
    ("merchant", "bws%", "ALCOHOL", "Bottle Shop",
     "Bottle shop lands as Eating Out on the raw feed."),
    ("merchant", "% bws%", "ALCOHOL", "Bottle Shop",
     "Bottle shop lands as Eating Out on the raw feed."),
    ("merchant", "%dan murphy%", "ALCOHOL", "Bottle Shop",
     "Bottle shop lands as Eating Out on the raw feed."),
    ("merchant", "%anthropic%", "SOFTWARE_AI", "AI Services",
     "AI/API usage lands as Shopping on the raw feed."),
    ("merchant", "%spriggy%", "FAMILY_TRANSFERS", "Kids Pocket Money",
     "Kids' pocket-money top-ups land as Shopping on the raw feed."),
]

# New category keys this rule set introduces, cached into `categories`
# alongside whatever the bank's own 16 CDR keys synced there, so the UI's
# category picker and the lookup queue's validation both already know them.
NEW_CATEGORIES: list[tuple[str, str]] = [
    ("LOAN_INTEREST", "Loan & Credit Interest"),
    ("ALCOHOL", "Alcohol"),
    ("SOFTWARE_AI", "Software & AI"),
    ("FAMILY_TRANSFERS", "Family Transfers"),
]

_SEEDED_KEY = "category_overrides_seeded"

# Self-contained match for any query that has transactions aliased t — same
# shape as themes.MATCH. The lowest-id matching rule wins, so an older,
# more specific rule added first takes precedence over a later broad one.
OCAT = """(SELECT co.category FROM category_overrides co
    WHERE (co.kind='merchant' AND t.merchant_key LIKE co.pattern)
       OR (co.kind='description' AND t.description LIKE co.pattern)
    ORDER BY co.id LIMIT 1)"""

OSUB = """(SELECT co.subcategory FROM category_overrides co
    WHERE (co.kind='merchant' AND t.merchant_key LIKE co.pattern)
       OR (co.kind='description' AND t.description LIKE co.pattern)
    ORDER BY co.id LIMIT 1)"""


def ensure_schema(store) -> None:
    """Additive and idempotent: CREATE TABLE IF NOT EXISTS, then seed the
    built-ins exactly once. Calling this on every startup (as create_app
    does for every other derived schema) is always safe to re-run."""
    store.con.executescript(_SCHEMA)
    seeded = store.con.execute(
        "SELECT 1 FROM meta WHERE key=?", (_SEEDED_KEY,)).fetchone()
    if not seeded:
        store.con.executemany(
            "INSERT OR IGNORE INTO category_overrides "
            "(kind, pattern, category, subcategory, note) VALUES (?,?,?,?,?)",
            BUILTIN_RULES)
        store.con.executemany(
            "INSERT OR IGNORE INTO categories (key, label) VALUES (?,?)",
            NEW_CATEGORIES)
        store.con.execute(
            "INSERT INTO meta (key, value) VALUES (?, '1')", (_SEEDED_KEY,))
    store.con.commit()


def list_rules(con: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT id, kind, pattern, category, subcategory, note "
        "FROM category_overrides ORDER BY id")]


def add_rule(store, kind: str, pattern: str, category: str,
             subcategory: str | None = None, note: str = "") -> int:
    """Add one rule. Adding an identical (kind, pattern, category) is a
    no-op, not an error — same shape as themes.add_rule."""
    if kind not in ("merchant", "description"):
        raise ValueError("kind must be 'merchant' or 'description'")
    pattern = (pattern or "").strip()
    category = (category or "").strip()
    if not pattern or not category:
        raise ValueError("pattern and category are required")
    cur = store.con.execute(
        "INSERT OR IGNORE INTO category_overrides "
        "(kind, pattern, category, subcategory, note) VALUES (?,?,?,?,?)",
        (kind, pattern, category, subcategory, note))
    store.con.commit()
    return cur.lastrowid or 0


def remove_rule(store, rule_id: int) -> bool:
    """Remove one rule by id. True if a rule was removed. Deleting a
    built-in here is exactly as final as deleting a theme rule — restarting
    the app never brings it back, because seeding only runs once, ever."""
    cur = store.con.execute("DELETE FROM category_overrides WHERE id=?", (rule_id,))
    store.con.commit()
    return cur.rowcount > 0
