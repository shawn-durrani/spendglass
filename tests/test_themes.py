"""Themes — a read-time lens, never a recategorisation. A fresh store has
no themes; templates instantiate only on request; deletions are final."""

import sqlite3
from datetime import date, timedelta

import pytest

from spendglass import themes
from spendglass.enrich import ensure_schema as enrich_schema
from spendglass.enrich import merchant_key as _mkey
from spendglass.lookup import ensure_schema as lookup_schema
from spendglass.overrides import ensure_schema as overrides_schema
from spendglass.store import Store
from spendglass.transfers import ensure_schema as transfers_schema


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "store.db") as s:
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)
        overrides_schema(s)
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


# ── Sport & Fitness (issue #113) ─────────────────────────────────────────
#
# Separate from the "store" fixture above: these tests build their own
# synthetic rows so each one stays readable about exactly what it's
# proving, using the house synthetic roster (Example Bank/Builders-style
# fictional merchants), never anything from a real statement.

@pytest.fixture()
def sf_store(tmp_path):
    with Store(tmp_path / "sport.db") as s:
        enrich_schema(s)
        lookup_schema(s)
        transfers_schema(s)
        overrides_schema(s)
        themes.ensure_schema(s)
        themes.create_theme(s, "Health & Fitness", template="Health & Fitness")
        themes.create_theme(s, "Sport & Fitness", template="Sport & Fitness")
        yield s


def _mo():
    first = date.today().replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")  # last complete month


def _insert(store, id_, *, merchant_name, description=None, cents, direction="debit",
            category="SHOPPING", key=None):
    mo = _mo()
    key = key if key is not None else _mkey(merchant_name, description)
    store.con.execute(
        """INSERT INTO transactions (id, account_id, date, description,
           merchant_name, merchant_key, amount, amount_cents, direction,
           category, status, raw, connection_id, first_seen_at, synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,'posted','{}','c1',?,?)""",
        (id_, "a1", f"{mo}-10", description or merchant_name, merchant_name, key,
         f"{cents // 100}.{cents % 100:02d}",
         abs(cents) if direction == "credit" else -abs(cents),
         direction, category, f"{mo}-10", f"{mo}-10"))
    store.con.commit()


def test_sport_fitness_template_excludes_pharmacy_and_medical():
    subs = {v for k, v in themes.TEMPLATES["Sport & Fitness"] if k == "subcategory"}
    assert "Pharmacy" not in subs
    assert "Health Services" not in subs
    # Health & Fitness itself is untouched by adding the new template.
    assert ("subcategory", "Pharmacy") in themes.TEMPLATES["Health & Fitness"]
    assert ("subcategory", "Health Services") in themes.TEMPLATES["Health & Fitness"]


def test_bike_shop_and_bike_park_caught_despite_wrong_category(sf_store):
    """The actual bug: a bike shop filed as Shopping and a bike park filed
    as Transportation never reach a Gym & Fitness/Sport & Recreation
    subcategory, so only a merchant rule can find them."""
    _insert(sf_store, "t1", merchant_name="Example Bike Shop", cents=235400,
            category="SHOPPING")
    _insert(sf_store, "t2", merchant_name="Example Bike Park", cents=8900,
            category="TRANSPORT")
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 235400 + 8900
    assert by["Sport & Fitness"]["txn_count"] == 2
    assert by["Health & Fitness"]["total_cents"] == 0  # unrelated theme, untouched


def test_matches_bike_merchant_with_no_merchant_name(sf_store):
    """merchant_name is often absent; the merchant_key normaliser falls
    back to the description, and the rule must still see it there."""
    _insert(sf_store, "t1", merchant_name=None, description="Example Bike Park entry",
            cents=8900, category="TRANSPORT")
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 8900


def test_avoids_broad_false_positives(sf_store):
    """'cycle' alone would also catch a recycling centre and a motorcycle
    shop, and a bare '%bike%' would catch a motorbike shop — which is why
    the template anchors 'bike' to a word start and spells the rest out."""
    _insert(sf_store, "t1", merchant_name="Example Recycling Centre", cents=4000)
    _insert(sf_store, "t2", merchant_name="Example Motorcycle Service", cents=9000)
    _insert(sf_store, "t3", merchant_name="Example Motorbike Service", cents=7000)
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 0
    assert by["Sport & Fitness"]["txn_count"] == 0


def test_subcategory_and_merchant_rule_both_matching_does_not_double_count(sf_store):
    sf_store.con.execute(
        """INSERT INTO merchant_lookups (merchant_key, status, resolved_name,
           resolved_subcategory) VALUES (?, 'approved', 'Example Bike Club',
           'Sport & Recreation')""", (_mkey("Example Bike Club", None),))
    sf_store.con.commit()
    _insert(sf_store, "t1", merchant_name="Example Bike Club", cents=6000)
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 6000  # once, not twice
    assert by["Sport & Fitness"]["txn_count"] == 1


def test_refund_does_not_inflate_the_theme_total(sf_store):
    """A charge that comes back as a refund is a credit, and credits are
    never spend here (same rule every other view in the app follows) — so
    the theme shows the original charge and never adds the refund on top."""
    _insert(sf_store, "t1", merchant_name="Example Bike Park", cents=16000,
            direction="debit", category="TRANSPORT")
    _insert(sf_store, "t2", merchant_name="Example Bike Park", cents=16000,
            direction="credit", category="TRANSPORT")
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 16000
    assert by["Sport & Fitness"]["txn_count"] == 1


def test_personal_training_joins_only_once_a_merchant_is_actually_identified(sf_store):
    """No rule here assumes a specific trainer. Before identification an
    arbitrary merchant never leaks in just for sounding fitness-adjacent;
    after a lookup resolves one to the Personal Training subcategory, the
    existing subcategory rule picks it up with no code change."""
    _insert(sf_store, "unidentified", merchant_name="Example Studio 12", cents=9000)
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 0

    key = _mkey("Example PT Studio", None)
    sf_store.con.execute(
        """INSERT INTO merchant_lookups (merchant_key, status, resolved_name,
           resolved_subcategory) VALUES (?, 'approved', 'Example PT Studio',
           'Personal Training')""", (key,))
    sf_store.con.commit()
    _insert(sf_store, "identified", merchant_name="Example PT Studio", cents=12000,
            key=key)
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 12000


def test_user_can_add_a_merchant_rule_once_a_trainer_is_identified(sf_store):
    """The editable path the issue asks for: add_rule already lets any
    theme take a merchant-specific rule the moment its key is known,
    without waiting on a lookup."""
    key = _mkey("Example Personal Trainer", None)
    _insert(sf_store, "t1", merchant_name="Example Personal Trainer", cents=15000,
            key=key)
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 0  # not yet, no rule for it

    themes.add_rule(sf_store, "Sport & Fitness", "merchant", key)
    by = {t["theme"]: t for t in themes.summary(sf_store.con, 3)["themes"]}
    assert by["Sport & Fitness"]["total_cents"] == 15000
