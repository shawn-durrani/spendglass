"""Themes — cross-cutting lenses over the category tree.

A theme answers "what is all of X costing me?" when X cuts across
categories: a renovation is trades + hardware + architects + skip hire; pets
are supplies + vet + insurance. Themes never change a transaction's
category — they are a read-time grouping, defined by rules:

  kind='subcategory'  → matches the merchant's resolved subcategory exactly
  kind='merchant'     → matches transactions.merchant_key with SQL LIKE

Rules live in the store and belong to the user. A fresh store has NO
themes — nothing presumes what a stranger's life contains; the templates
below are offered as starting points and instantiated only on request.
A deleted theme stays deleted.
"""

from __future__ import annotations

import sqlite3

from .trends import JOIN, SPEND, _complete_months

_SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
  name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS theme_rules (
  theme TEXT NOT NULL REFERENCES themes(name) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('subcategory', 'merchant')),
  value TEXT NOT NULL,
  PRIMARY KEY (theme, kind, value)
);
"""

# Starting points only — subcategory rules, no merchants (a merchant rule
# is always somebody's specific life). Values are editable the moment a
# template lands; a rule that matches nothing yet simply contributes $0.
TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "Renovation": [
        ("subcategory", "Building & Carpentry"),
        ("subcategory", "Landscaping"),
        ("subcategory", "Electrical & Plumbing"),
        ("subcategory", "Waste & Skip Hire"),
        ("subcategory", "Hardware & DIY"),
        ("subcategory", "Garden & Plants"),
        ("subcategory", "Home Maintenance"),
    ],
    "Pets": [
        ("subcategory", "Pet Supplies"),
        ("subcategory", "Pet Insurance"),
        ("subcategory", "Veterinary"),
    ],
    "Travel": [
        ("subcategory", "Flights"),
        ("subcategory", "Accommodation"),
        ("subcategory", "Car Hire"),
        ("subcategory", "Tours & Activities"),
    ],
    "Kids": [
        ("subcategory", "Childcare"),
        ("subcategory", "School Fees"),
        ("subcategory", "Kids' Activities"),
        ("subcategory", "Toys & Games"),
    ],
    "AI": [
        ("subcategory", "AI Services"),
    ],
    "Health & Fitness": [
        ("subcategory", "Gym & Fitness"),
        ("subcategory", "Sport & Recreation"),
        ("subcategory", "Health Services"),
        ("subcategory", "Pharmacy"),
    ],
}

# Self-contained match: usable in any query that has transactions aliased t,
# without requiring the ml join (parameter: theme name).
MATCH = """EXISTS (
    SELECT 1 FROM theme_rules r WHERE r.theme = ?
      AND ((r.kind = 'merchant' AND t.merchant_key LIKE r.value)
           OR (r.kind = 'subcategory' AND EXISTS (
               SELECT 1 FROM merchant_lookups ml2
               WHERE ml2.merchant_key = t.merchant_key
                 AND ml2.status IN ('auto','approved','overridden')
                 AND ml2.resolved_subcategory = r.value))))"""


def ensure_schema(store) -> None:
    store.con.executescript(_SCHEMA)
    store.con.commit()


def create_theme(store, name: str, template: str | None = None) -> None:
    """Create an empty theme, or instantiate a template's starter rules
    under the given name. Duplicate names raise sqlite3.IntegrityError for
    the caller to surface; an unknown template is an explicit error, never
    a silent empty theme."""
    name = (name or "").strip()
    if not name:
        raise ValueError("theme name is required")
    rules: list[tuple[str, str]] = []
    if template is not None:
        if template not in TEMPLATES:
            raise ValueError(f"unknown template '{template}'")
        rules = TEMPLATES[template]
    store.con.execute("INSERT INTO themes (name) VALUES (?)", (name,))
    store.con.executemany(
        "INSERT INTO theme_rules (theme, kind, value) VALUES (?,?,?)",
        [(name, k, v) for k, v in rules])
    store.con.commit()


def delete_theme(store, name: str) -> bool:
    """Delete a theme and its rules. True if a theme was removed.
    Rules go explicitly — correctness must not depend on the FK pragma."""
    store.con.execute("DELETE FROM theme_rules WHERE theme=?", (name,))
    cur = store.con.execute("DELETE FROM themes WHERE name=?", (name,))
    store.con.commit()
    return cur.rowcount > 0


def add_rule(store, theme: str, kind: str, value: str) -> None:
    """Add one rule. Adding an existing rule is a no-op, not an error."""
    if kind not in ("subcategory", "merchant"):
        raise ValueError("kind must be 'subcategory' or 'merchant'")
    value = (value or "").strip()
    if not value:
        raise ValueError("rule value is required")
    if not store.con.execute("SELECT 1 FROM themes WHERE name=?",
                             (theme,)).fetchone():
        raise ValueError(f"unknown theme '{theme}'")
    store.con.execute(
        "INSERT OR IGNORE INTO theme_rules (theme, kind, value) VALUES (?,?,?)",
        (theme, kind, value))
    store.con.commit()


def remove_rule(store, theme: str, kind: str, value: str) -> bool:
    """Remove one rule. True if a rule was removed."""
    cur = store.con.execute(
        "DELETE FROM theme_rules WHERE theme=? AND kind=? AND value=?",
        (theme, kind, value))
    store.con.commit()
    return cur.rowcount > 0


def list_themes(con: sqlite3.Connection) -> list[dict]:
    out = []
    for t in con.execute("SELECT name FROM themes ORDER BY name"):
        rules = [{"kind": r["kind"], "value": r["value"]}
                 for r in con.execute(
                     "SELECT kind, value FROM theme_rules WHERE theme=? "
                     "ORDER BY kind, value", (t["name"],))]
        out.append({"name": t["name"], "rules": rules})
    return out


def summary(con: sqlite3.Connection, months_back: int = 12) -> dict:
    """Per theme: spend by complete month, trailing total, txn count."""
    months = _complete_months(con, months_back)
    lo = f"{months[0]}-01" if months else "9999-12-31"
    themes = []
    for t in con.execute("SELECT name FROM themes ORDER BY name"):
        rows = con.execute(
            f"""SELECT substr(t.date,1,7) mo,
                       SUM(ABS(t.amount_cents)) cents, COUNT(*) n
                {JOIN}
                WHERE {SPEND} AND t.date >= ? AND {MATCH}
                GROUP BY mo ORDER BY mo""",
            (lo, t["name"])).fetchall()
        by_month = {r["mo"]: r["cents"] for r in rows}
        themes.append({
            "theme": t["name"],
            "months": [{"month": m, "cents": by_month.get(m, 0)}
                       for m in months],
            "total_cents": sum(r["cents"] for r in rows),
            "txn_count": sum(r["n"] for r in rows),
        })
    return {"months": months, "themes": themes}
