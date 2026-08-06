"""Themes — a read-time lens, never a recategorisation. A fresh store has
no themes; templates instantiate only on request; deletions are final."""

import sqlite3
from datetime import date, timedelta

import pytest

from spendglass import themes
from spendglass.enrich import ensure_schema as enrich_schema
from spendglass.lookup import ensure_schema as lookup_schema
from spendglass.store import Store
from spendglass.transfers import ensure_schema as transfers_schema


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "store.db") as s:
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)
        themes.ensure_schema(s)
        themes.create_theme(s, "Pets", template="Pets")
        themes.create_theme(s, "Renovation")           # blank, user-built
        themes.add_rule(s, "Renovation", "merchant", "example builders%")
        themes.create_theme(s, "Health & Fitness", template="Health & Fitness")
        # last complete month, so summary() picks the rows up
        first = date.today().replace(day=1)
        mo = (first - timedelta(days=1)).strftime("%Y-%m")

        def txn(i, key, cents, cat="MERCHANDISE"):
            s.con.execute(
                """INSERT INTO transactions (id, account_id, date, description,
                   merchant_name, merchant_key, amount, amount_cents, direction,
                   category, status, raw, connection_id, first_seen_at, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,'debit',?,'posted','{}','c1',?,?)""",
                (f"t{i}", "a1", f"{mo}-10", key, key, key, str(cents / 100),
                 -abs(cents), cat, f"{mo}-10", f"{mo}-10"))

        s.con.execute("INSERT INTO merchants (key, display_name) VALUES (?,?)",
                      ("pet shop co", "Pet Shop Co"))
        s.con.execute(
            """INSERT INTO merchant_lookups (merchant_key, status,
               resolved_name, resolved_subcategory)
               VALUES ('pet shop co', 'approved', 'Pet Shop Co', 'Pet Supplies')""")
        txn(1, "pet shop co", 5000)                    # theme via subcategory
        txn(2, "example builders 04 may", 100000)      # theme via merchant LIKE
        txn(3, "some cafe", 700, "FOOD_AND_DRINK")     # no theme
        s.con.commit()
        yield s


def test_virgin_store_has_no_themes(tmp_path):
    with Store(tmp_path / "virgin.db") as s:
        themes.ensure_schema(s)
        assert themes.list_themes(s.con) == []


def test_deleted_theme_stays_deleted(store):
    assert themes.delete_theme(store, "Health & Fitness")
    themes.ensure_schema(store)                        # never resurrects
    got = {t["name"] for t in themes.list_themes(store.con)}
    assert "Health & Fitness" not in got
    # its rules went with it, whatever the FK pragma says
    assert store.con.execute(
        "SELECT COUNT(*) FROM theme_rules WHERE theme='Health & Fitness'"
    ).fetchone()[0] == 0
    assert not themes.delete_theme(store, "Health & Fitness")  # already gone


def test_template_instantiation_and_duplicates(store):
    by = {t["name"]: t for t in themes.list_themes(store.con)}
    assert [(r["kind"], r["value"]) for r in by["Pets"]["rules"]] == \
        sorted(themes.TEMPLATES["Pets"])
    with pytest.raises(sqlite3.IntegrityError):
        themes.create_theme(store, "Pets")
    with pytest.raises(ValueError):
        themes.create_theme(store, "Trips", template="No Such Template")
    with pytest.raises(ValueError):
        themes.create_theme(store, "   ")


def test_rule_validation_and_idempotence(store):
    with pytest.raises(ValueError):
        themes.add_rule(store, "Pets", "colour", "blue")
    with pytest.raises(ValueError):
        themes.add_rule(store, "Pets", "merchant", "  ")
    with pytest.raises(ValueError):
        themes.add_rule(store, "No Such Theme", "merchant", "x%")
    themes.add_rule(store, "Pets", "merchant", "pet shop%")
    themes.add_rule(store, "Pets", "merchant", "pet shop%")   # no-op repeat
    by = {t["name"]: t for t in themes.list_themes(store.con)}
    assert len(by["Pets"]["rules"]) == len(themes.TEMPLATES["Pets"]) + 1
    assert themes.remove_rule(store, "Pets", "merchant", "pet shop%")
    assert not themes.remove_rule(store, "Pets", "merchant", "pet shop%")


def test_summary_matches_both_rule_kinds(store):
    by = {t["theme"]: t for t in themes.summary(store.con, 3)["themes"]}
    assert by["Pets"]["total_cents"] == 5000
    assert by["Renovation"]["total_cents"] == 100000
    assert by["Health & Fitness"]["total_cents"] == 0
    assert by["Pets"]["txn_count"] == 1


def test_match_clause_filters_transactions(store):
    rows = store.con.execute(
        f"SELECT t.id FROM transactions t WHERE {themes.MATCH}",
        ("Renovation",)).fetchall()
    assert [r["id"] for r in rows] == ["t2"]
