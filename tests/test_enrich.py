"""Enrichment: normalisation, recurring detection, classification (mocked)."""

import json
from datetime import date, timedelta

import pytest

from spendglass import enrich, queries
from spendglass.enrich import merchant_key
from spendglass.store import Store
from tests.conftest import ACCOUNTS, CATEGORIES, CONNECTIONS


def _txn(i, date_str, amount, merchant=None, desc="X", category=None,
         direction="debit"):
    return {"id": f"gen-{merchant}-{i}", "accountId": "acc-1",
            "accountName": "Everyday", "status": "posted", "date": date_str,
            "datetime": None, "postDate": None, "postDatetime": None,
            "valueDate": None, "valueDatetime": None, "description": desc,
            "amount": amount, "direction": direction, "category": category,
            "customCategory": None, "customCategoryGroup": None,
            "merchantName": merchant, "merchantCategoryCode": None}


def _monthly(merchant, amount, n=6, start=date(2026, 1, 15), jitter=(0, 1, -1)):
    out = []
    d = start
    for i in range(n):
        out.append(_txn(i, d.isoformat(), amount, merchant=merchant,
                        desc=f"{merchant} PAYMENT"))
        d = d + timedelta(days=30 + jitter[i % len(jitter)])
    return out


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "store.db"
    with Store(path) as s:
        s.upsert_connections(CONNECTIONS)
        s.upsert_accounts(ACCOUNTS)
        s.upsert_categories(CATEGORIES)
    return path


# ── normalisation ───────────────────────────────────────────────────────────

def test_merchant_key_strips_processor_noise():
    assert merchant_key("SQ *COFFEE CO", None) == merchant_key("Coffee Co", None)
    assert merchant_key("PAYPAL *STREAMCO", None) == merchant_key("Streamco", None)
    assert merchant_key("SAMPLE MART  2104", None) == merchant_key("Sample Mart", None)


def test_merchant_key_falls_back_to_description():
    # Bank plumbing prefixes are noise, not identity.
    assert merchant_key(None, "DIRECT DEBIT GYM") == "gym"
    assert merchant_key(None, None) == "(unknown)"


def test_merchant_key_strips_currency_tails():
    # Foreign-charge tails must not split one merchant per amount. The
    # mangled city ("SAN FRANCISC" truncated into the "CA" state code) is
    # the card network's own artifact — keys must survive it unmodified.
    a = merchant_key(None, "EXAMPLE SAAS SAN FRANCISCCA, ##0101 12.34 US DOLLAR")
    b = merchant_key(None, "EXAMPLE SAAS SAN FRANCISCCA, ##0201 23.45 US DOLLAR")
    assert a == b == "example saas san franciscca"
    assert merchant_key(
        None,
        "EXAMPLESHOP.COM SPRINGFIELD USA USD 23.45 incl. Foreign Transaction Fee AUD $0.99",
    ) == "exampleshop.com springfield"


def test_merchant_key_strips_phones_and_rails():
    # Processors smuggle phone-shaped tokens into descriptors; rails markers
    # ("(via PayPal)") are plumbing, not identity.
    assert merchant_key("Janedoecrafts (via PayPal) +1 (555) 010-4477", None) == "janedoecrafts"
    assert merchant_key(None, "DEBIT CARD PURCHASE GROCER 1234 EXAMPLEVILLE   AUS") == \
        merchant_key("Grocer 1234 Exampleville", None)


# ── recurring detection ─────────────────────────────────────────────────────

def test_monthly_subscription_detected(db):
    with Store(db) as s:
        s.upsert_transactions(_monthly("Spotify", "-13.99"), "conn-bank-1")
        enrich.rebuild_merchants(s)
        assert enrich.detect_recurring(s) == 1
        row = s.con.execute("SELECT * FROM recurring_charges").fetchone()
        assert row["cadence"] == "monthly"
        assert row["typical_amount"] == "-13.99"
        assert row["occurrences"] == 6
        assert row["next_expected"] > row["last_seen"]


def test_weekly_and_variable_amount_detected(db):
    txns = []
    d = date(2026, 5, 1)
    for i in range(8):  # weekly groceries, varying amounts
        txns.append(_txn(i, d.isoformat(), f"-{80 + i * 7}.50", merchant="Grocer"))
        d += timedelta(days=7)
    with Store(db) as s:
        s.upsert_transactions(txns, "conn-bank-1")
        enrich.rebuild_merchants(s)
        assert enrich.detect_recurring(s) == 1
        row = s.con.execute("SELECT * FROM recurring_charges").fetchone()
        assert row["cadence"] == "weekly"
        assert row["amount_min"] != row["amount_max"]  # variability reported


def test_irregular_spending_not_flagged(db):
    txns = [_txn(i, d, "-20.00", merchant="Random Cafe")
            for i, d in enumerate(["2026-01-03", "2026-01-05", "2026-03-20",
                                   "2026-04-02", "2026-07-19"])]
    with Store(db) as s:
        s.upsert_transactions(txns, "conn-bank-1")
        enrich.rebuild_merchants(s)
        assert enrich.detect_recurring(s) == 0


def test_two_occurrences_never_recurring(db):
    with Store(db) as s:
        s.upsert_transactions(_monthly("Almost", "-5.00", n=2), "conn-bank-1")
        enrich.rebuild_merchants(s)
        assert enrich.detect_recurring(s) == 0


# ── merchants table ─────────────────────────────────────────────────────────

def test_merchants_aggregate_and_preserve_llm_cache(db):
    with Store(db) as s:
        s.upsert_transactions(_monthly("Spotify", "-13.99"), "conn-bank-1")
        enrich.rebuild_merchants(s)
        s.con.execute("UPDATE merchants SET llm_category='ENTERTAINMENT', "
                      "llm_confidence=0.9 WHERE key='spotify'")
        s.con.commit()
        enrich.rebuild_merchants(s)  # stats refresh must NOT clobber the cache
        row = s.con.execute("SELECT * FROM merchants WHERE key='spotify'").fetchone()
        assert row["llm_category"] == "ENTERTAINMENT"
        assert row["txn_count"] == 6


# ── classification (mocked Anthropic client) ────────────────────────────────

class _FakeBatches:
    def __init__(self, label):
        self.label = label
        self.created = None

    def create(self, requests):
        self.created = requests
        return type("B", (), {"id": "batch_fake"})()

    def retrieve(self, batch_id):
        return type("B", (), {"processing_status": "ended"})()

    def results(self, batch_id):
        for req in self.created:
            text = json.dumps({"category": self.label, "confidence": 0.87})
            blk = type("Blk", (), {"type": "text", "text": text})()
            msg = type("Msg", (), {"content": [blk]})()
            res = type("Res", (), {"type": "succeeded", "message": msg})()
            yield type("R", (), {"custom_id": req["custom_id"], "result": res})()


class _FakeAnthropic:
    def __init__(self, label="EATING_OUT"):
        self.messages = type("M", (), {})()
        self.messages.batches = _FakeBatches(label)


def test_classification_caches_labels(db):
    with Store(db) as s:
        s.upsert_transactions(_monthly("Spotify", "-13.99"), "conn-bank-1")
        enrich.rebuild_merchants(s)
        fake = _FakeAnthropic(label="EATING_OUT")
        out = enrich.classify_merchants(s, fake, poll_seconds=0)
        assert out["status"] == "ok" and out["classified"] == 1
        row = s.con.execute("SELECT llm_category, llm_model FROM merchants "
                            "WHERE key='spotify'").fetchone()
        assert row["llm_category"] == "EATING_OUT"
        assert row["llm_model"] == enrich.MODEL
        # request shape: batch of ONE (unique merchant, not 6 transactions)
        assert len(fake.messages.batches.created) == 1
        params = fake.messages.batches.created[0]["params"]
        assert params["model"] == enrich.MODEL
        assert params["output_config"]["format"]["type"] == "json_schema"
        # second run: cache hit, no new batch
        out2 = enrich.classify_merchants(s, _FakeAnthropic(), poll_seconds=0)
        assert out2["classified"] == 0 and out2["reason"] == "nothing unclassified"


def test_classification_rejects_labels_outside_taxonomy(db):
    with Store(db) as s:
        s.upsert_transactions(_monthly("Spotify", "-13.99"), "conn-bank-1")
        enrich.rebuild_merchants(s)
        out = enrich.classify_merchants(s, _FakeAnthropic(label="MADE_UP"), poll_seconds=0)
        assert out["invalid"] == 1 and out["classified"] == 0
        row = s.con.execute("SELECT llm_category FROM merchants WHERE key='spotify'").fetchone()
        assert row["llm_category"] is None  # garbage never lands in the cache


def test_keyless_cli_degrades(db, monkeypatch, capsys):
    monkeypatch.setenv("SPENDGLASS_DB", str(db))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("spendglass.enrich._parse_env_file",
                        lambda p: {}, raising=False)
    import spendglass.config as config
    monkeypatch.setattr(config, "_parse_env_file", lambda p: {})
    assert enrich.main(["--classify"]) == 0
    out = capsys.readouterr().out
    assert "keyless degradation" in out


# ── the new MCP queries ─────────────────────────────────────────────────────

def test_recurring_tool_reads_derived_table(db):
    with Store(db) as s:
        s.upsert_transactions(_monthly("Spotify", "-13.99"), "conn-bank-1")
        enrich.rebuild_merchants(s)
        enrich.detect_recurring(s)
    con = queries.open_readonly(db)
    r = queries.list_recurring_charges(con)
    con.close()
    assert r["recurring"][0]["cadence"] == "monthly"


def test_recurring_tool_before_enrichment_says_so(db):
    con = queries.open_readonly(db)
    r = queries.list_recurring_charges(con)
    con.close()
    assert r["recurring"] == [] and "enrichment has never run" in r["note"]


# The MCP tool-surface pin lives in test_queries.py (single canonical pin —
# two copies of the same list just drift apart).