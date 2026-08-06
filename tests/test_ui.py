"""UI auth + query behavior — keyless, in-process via TestClient."""

import pytest
from fastapi.testclient import TestClient

from spendglass.auth import Auth
from spendglass.store import Store
from spendglass.ui import create_app
from tests.conftest import ACCOUNTS, CATEGORIES, CONNECTIONS, TXNS

SECRET = "test-recovery-secret"
PASSWORD = "correct-horse-battery"


@pytest.fixture()
def ui(tmp_path):
    db = tmp_path / "store.db"
    with Store(db) as s:
        s.upsert_connections(CONNECTIONS)
        s.upsert_accounts(ACCOUNTS)
        s.upsert_categories(CATEGORIES)
        s.upsert_transactions(TXNS, "conn-bank-1")
    auth = Auth(auth_file=tmp_path / "ui_auth.json", recovery_secret=SECRET)
    app = create_app(db, auth)
    return TestClient(app, base_url="http://127.0.0.1:8903"), auth


def _enroll(client) -> None:
    r = client.post("/api/setup", json={"recovery_secret": SECRET, "password": PASSWORD})
    assert r.status_code == 200


# ── auth ────────────────────────────────────────────────────────────────────

def test_data_requires_session(ui):
    client, _ = ui
    for path in ("/api/transactions", "/api/health", "/api/accounts", "/api/categories"):
        assert client.get(path).status_code == 401


def test_first_run_setup_requires_recovery_secret(ui):
    client, _ = ui
    r = client.post("/api/setup", json={"recovery_secret": "wrong", "password": PASSWORD})
    assert r.status_code == 403
    _enroll(client)
    assert client.get("/api/transactions").status_code == 200


def test_short_password_rejected(ui):
    client, _ = ui
    r = client.post("/api/setup", json={"recovery_secret": SECRET, "password": "short"})
    assert r.status_code == 400


def test_login_after_enrollment(ui):
    client, _ = ui
    _enroll(client)
    client.post("/api/logout")
    assert client.get("/api/transactions").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 403
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/transactions").status_code == 200


def test_restart_keeps_sessions(ui, tmp_path):
    """Sessions persist across restarts (hashed tokens on disk) — a code
    deploy no longer logs every browser out. Revocation persists too."""
    client, auth = ui
    _enroll(client)
    cookie = client.cookies.get("spendglass_session")
    restarted = Auth(auth_file=auth.auth_file, recovery_secret=SECRET)
    assert restarted.first_run is False              # password survived
    assert restarted.check_session(cookie) is True   # session survived
    # Only digests are stored — the file must not contain the raw token.
    assert cookie not in (auth.auth_file.parent / "ui_sessions.json").read_text()
    restarted.revoke_session(cookie)
    assert Auth(auth_file=auth.auth_file,
                recovery_secret=SECRET).check_session(cookie) is False


def test_lookup_review_flow(ui):
    """The one write path: list proposals, approve/override/reject in bulk;
    decisions land in merchant_lookups only."""
    from spendglass.lookup import ensure_schema
    from spendglass.store import Store

    client, auth = ui
    _enroll(client)
    with Store(auth.auth_file.parent / "store.db") as s:
        ensure_schema(s)
        s.con.execute(
            """INSERT INTO merchant_lookups (merchant_key, proposed_name,
               proposed_subcategory, confidence, status)
               VALUES ('cafe x', 'Cafe X', 'Cafe', 0.5, 'pending'),
                      ('gym y', 'Gym Y', 'Gym', 0.4, 'pending')""")
        s.con.commit()

    r = client.get("/api/lookups?status=pending").json()
    assert r["counts"]["pending"] == 2 and len(r["data"]) == 2

    cat = CATEGORIES[0]["key"]
    r = client.post("/api/lookups/decide", json={"decisions": [
        {"merchant_key": "cafe x", "action": "approve",
         "subcategory": "Hobby supplies", "category": cat},  # override both
        {"merchant_key": "gym y", "action": "reject"},
    ]})
    assert r.status_code == 200 and r.json()["decided"] == 2

    with Store(auth.auth_file.parent / "store.db") as s:
        rows = {r["merchant_key"]: dict(r) for r in s.con.execute(
            "SELECT * FROM merchant_lookups")}
    assert rows["cafe x"]["status"] == "overridden"
    assert rows["cafe x"]["resolved_subcategory"] == "Hobby supplies"
    assert rows["cafe x"]["resolved_category"] == cat
    assert rows["cafe x"]["resolved_name"] == "Cafe X"
    assert rows["gym y"]["status"] == "rejected"

    # malformed decisions are rejected wholesale
    assert client.post("/api/lookups/decide", json={"decisions": [
        {"merchant_key": "cafe x", "action": "explode"}]}).status_code == 400
    # category overrides are constrained to the real category keys
    assert client.post("/api/lookups/decide", json={"decisions": [
        {"merchant_key": "cafe x", "action": "approve",
         "category": "VIBES"}]}).status_code == 400

    # inline correction on a merchant the pipeline never proposed for:
    # the decision seeds the row (all matching transactions pick it up)
    cat2 = CATEGORIES[-1]["key"]
    assert client.post("/api/lookups/decide", json={"decisions": [
        {"merchant_key": "example outfitters", "action": "approve",
         "name": "Example Outfitters", "category": cat2}]}).status_code == 200
    with Store(auth.auth_file.parent / "store.db") as s:
        row = dict(s.con.execute(
            "SELECT * FROM merchant_lookups WHERE merchant_key='example outfitters'"
        ).fetchone())
    assert row["status"] == "overridden"
    assert row["resolved_name"] == "Example Outfitters"
    assert row["resolved_category"] == cat2


def test_propagate_decisions_refreshes_pendings(tmp_path):
    """A confirmed identity refreshes related pending proposals via the
    small model — rows stay pending, proposals improve."""
    from types import SimpleNamespace

    from spendglass.lookup import ensure_schema, propagate_decisions

    with Store(tmp_path / "store.db") as s:
        ensure_schema(s)
        s.con.execute(
            """INSERT INTO merchant_lookups (merchant_key, proposed_name,
               confidence, status) VALUES
               ('cplus ph north', '', 0.2, 'pending'),
               ('unrelated cafe', 'Cafe Z', 0.5, 'pending')""")
        s.con.commit()

        canned = ('[{"merchant_key": "cplus ph north", '
                  '"name": "Chemplus Pharmacy", "subcategory": "Pharmacy", '
                  '"reason": "same CPLUS descriptor pattern", "confidence": 0.85}]')
        client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw:
            SimpleNamespace(content=[SimpleNamespace(type="text", text=canned)])))
        n = propagate_decisions(s, client, [
            {"merchant_key": "cplus ph mall", "name": "Chemplus Pharmacy",
             "subcategory": "Pharmacy"}])

        assert n == 1
        row = dict(s.con.execute(
            "SELECT * FROM merchant_lookups WHERE merchant_key='cplus ph north'"
        ).fetchone())
        assert row["status"] == "pending"           # never auto-approved
        assert row["proposed_name"] == "Chemplus Pharmacy"
        assert row["confidence"] == 0.85
        other = dict(s.con.execute(
            "SELECT * FROM merchant_lookups WHERE merchant_key='unrelated cafe'"
        ).fetchone())
        assert other["proposed_name"] == "Cafe Z"   # untouched


def test_admin_settings_roundtrip(ui):
    client, _ = ui
    _enroll(client)
    r = client.get("/api/admin").json()
    assert r["settings"]["auto_threshold"] == 0.8      # default
    assert r["data"]["n"] > 0
    assert client.post("/api/admin/config", json={
        "auto_threshold": 0.9, "workers": 2}).status_code == 200
    assert client.get("/api/admin").json()["settings"]["auto_threshold"] == 0.9
    assert client.post("/api/admin/config",
                       json={"auto_threshold": 7}).status_code == 400
    assert client.post("/api/admin/run",
                       json={"miner": "nonsense"}).status_code == 400


def test_env_writer_preserves_lines(tmp_path):
    from spendglass.capabilities import write_env_var
    env = tmp_path / ".env"
    env.write_text("# keys live here\nREDBARK_API_KEY=abc\n\nexport OTHER=1\n")
    write_env_var(env, "ANTHROPIC_API_KEY", "sk-new")
    text = env.read_text()
    assert "# keys live here" in text and "REDBARK_API_KEY=abc" in text
    assert "ANTHROPIC_API_KEY=sk-new" in text
    write_env_var(env, "OTHER", "2")           # export form replaced in place
    assert "export OTHER=2" in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600


def test_reset_invalidates_existing_sessions(ui):
    client, auth = ui
    _enroll(client)
    token = client.cookies.get("spendglass_session")
    auth.set_password(SECRET, "a-brand-new-password")
    assert auth.check_session(token) is False


def test_host_header_allowlist(ui):
    client, _ = ui
    r = client.get("/api/session", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_cross_site_post_refused(ui):
    client, _ = ui
    r = client.post("/api/login", json={"password": PASSWORD},
                    headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 403


# ── the table, server-side ──────────────────────────────────────────────────

def test_transactions_filter_and_totals(ui):
    client, _ = ui
    _enroll(client)
    r = client.get("/api/transactions", params={"direction": "debit"}).json()
    assert r["total"] == 1
    assert r["sum_cents"] == -450          # money math in SQL, not JS
    assert r["data"][0]["merchant_name"] == "Coffee Co"


def test_transactions_search(ui):
    client, _ = ui
    _enroll(client)
    r = client.get("/api/transactions", params={"q": "coffee"}).json()
    assert r["total"] == 1


def test_sort_allowlist_rejects_unknown_column(ui):
    client, _ = ui
    _enroll(client)
    r = client.get("/api/transactions", params={"sort": "amount; DROP TABLE"})
    assert r.status_code == 400            # allowlisted, never interpolated


def test_sort_by_amount(ui):
    client, _ = ui
    _enroll(client)
    r = client.get("/api/transactions", params={"sort": "amount", "dir": "asc"}).json()
    cents = [x["amount_cents"] for x in r["data"]]
    assert cents == sorted(cents)


def test_no_write_path_exists(ui):
    """The read-only invariant: no mutating method on any data endpoint."""
    client, _ = ui
    _enroll(client)
    for path in ("/api/transactions", "/api/health", "/api/accounts"):
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)(path).status_code == 405, (method, path)


# ── themes management API ───────────────────────────────────────────────────

def test_themes_crud_roundtrip(ui):
    client, _ = ui
    _enroll(client)
    r = client.get("/api/themes").json()
    assert r["definitions"] == []                      # virgin store: no themes
    assert {t["name"] for t in r["templates"]} >= {"Renovation", "Pets",
                                                   "Travel", "Kids"}

    # create from template; duplicate conflicts, unknown template is explicit
    assert client.post("/api/themes",
                       json={"name": "Pets", "template": "Pets"}).status_code == 200
    assert client.post("/api/themes", json={"name": "Pets"}).status_code == 409
    assert client.post("/api/themes",
                       json={"name": "X", "template": "Nope"}).status_code == 400
    assert client.post("/api/themes", json={"name": "  "}).status_code == 400

    # blank theme + rule add/remove; validation surfaces as 400s
    assert client.post("/api/themes", json={"name": "Workshop"}).status_code == 200
    ok = client.post("/api/themes/rules", json={
        "theme": "Workshop", "action": "add",
        "kind": "merchant", "value": "example builders%"})
    assert ok.status_code == 200
    by = {t["name"]: t for t in ok.json()["definitions"]}
    assert by["Workshop"]["rules"] == [{"kind": "merchant",
                                        "value": "example builders%"}]
    assert client.post("/api/themes/rules", json={
        "theme": "Workshop", "action": "add",
        "kind": "colour", "value": "x"}).status_code == 400
    assert client.post("/api/themes/rules", json={
        "theme": "Workshop", "action": "remove",
        "kind": "merchant", "value": "nope%"}).status_code == 404
    assert client.post("/api/themes/rules", json={
        "theme": "Workshop", "action": "remove",
        "kind": "merchant", "value": "example builders%"}).status_code == 200

    # delete is final and reported honestly
    assert client.post("/api/themes/delete",
                       json={"name": "Workshop"}).status_code == 200
    assert client.post("/api/themes/delete",
                       json={"name": "Workshop"}).status_code == 404
    names = {t["name"] for t in client.get("/api/themes").json()["definitions"]}
    assert names == {"Pets"}


def test_theme_writes_require_session(ui):
    client, _ = ui
    for path, body in (("/api/themes", {"name": "X"}),
                       ("/api/themes/delete", {"name": "X"}),
                       ("/api/themes/rules", {"theme": "X", "action": "add",
                                              "kind": "merchant", "value": "y%"})):
        assert client.post(path, json=body).status_code == 401


def test_viz_page_teaches_empty_themes():
    from spendglass.ui import PAGE_VIZ
    assert "No themes yet" in PAGE_VIZ           # honest empty state ships
    assert "quickTheme" in PAGE_VIZ              # with one-click templates
