"""Passkey (WebAuthn) login for the UI gate — #22, mirroring membro#27.

The acceptance surface:

- Enrolment is possible ONLY from a live session, never the gate.
- The single Relying Party is `localhost` (loopback-only app): 127.0.0.1
  reports no passkey and refuses ceremonies, and an assertion signed for a
  different origin is refused.
- A successful assertion mints the same session a password login mints; a
  failed, replayed, or counter-regressed one mints nothing.
- Credentials persist in data/ui_passkeys.json (owner-only) across a
  "restart"; in-flight ceremonies do not. The password stays as fallback.

Keyless and offline: the "authenticator" is a software P-256 passkey built
on py_webauthn's own dependencies (cryptography, cbor2), producing the same
byte layouts a platform authenticator does.
"""

from __future__ import annotations

import hashlib
import json
import secrets as pysecrets

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from spendglass import passkeys
from spendglass.auth import Auth
from spendglass.ui import PAGE, create_app

SECRET = "test-recovery-secret"
PASSWORD = "correct-horse-battery"
LOCAL = "http://localhost:8903"
IP = "http://127.0.0.1:8903"


class SoftPasskey:
    """A P-256 passkey behaving like a platform authenticator for one RP:
    same byte layouts, same signatures, no hardware. `sign_count` stays 0
    unless a test drives it, exactly like Apple's authenticators."""

    def __init__(self, rp_id="localhost"):
        self.rp_id = rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = pysecrets.token_bytes(16)

    def _cose_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: nums.x.to_bytes(32, "big"),
                            -3: nums.y.to_bytes(32, "big")})

    @staticmethod
    def _client_data(kind: str, challenge_b64u: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge_b64u,
                           "origin": origin, "crossOrigin": False}).encode()

    def register(self, public_key_options: dict, origin: str) -> dict:
        cdj = self._client_data("webauthn.create",
                                public_key_options["challenge"], origin)
        flags = 0x01 | 0x04 | 0x40  # UP | UV | AT
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([flags]) + (0).to_bytes(4, "big")
                     + bytes(16)
                     + len(self.cred_id).to_bytes(2, "big")
                     + self.cred_id + self._cose_key())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": bytes_to_base64url(self.cred_id),
                "rawId": bytes_to_base64url(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cdj),
                             "attestationObject": bytes_to_base64url(att)}}

    def assertion(self, public_key_options: dict, origin: str, *,
                  sign_count: int = 0) -> dict:
        cdj = self._client_data("webauthn.get",
                                public_key_options["challenge"], origin)
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest()
                     + bytes([0x01 | 0x04]) + sign_count.to_bytes(4, "big"))
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(),
                            ec.ECDSA(hashes.SHA256()))
        return {"id": bytes_to_base64url(self.cred_id),
                "rawId": bytes_to_base64url(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": bytes_to_base64url(cdj),
                             "authenticatorData": bytes_to_base64url(auth_data),
                             "signature": bytes_to_base64url(sig),
                             "userHandle": None}}


@pytest.fixture()
def ui(tmp_path):
    auth = Auth(auth_file=tmp_path / "ui_auth.json", recovery_secret=SECRET)
    app = create_app(tmp_path / "store.db", auth)
    return app, auth


def _client(app, base_url=LOCAL):
    return TestClient(app, base_url=base_url)


def _owner(app):
    """A client that enrolled the password and holds a live session."""
    c = _client(app)
    r = c.post("/api/setup", json={"recovery_secret": SECRET,
                                   "password": PASSWORD})
    assert r.status_code == 200
    return c

def _login(app):
    c = _client(app)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
    return c


def _enrol_passkey(client, origin=LOCAL, rp="localhost", pk=None):
    pk = pk or SoftPasskey(rp)
    o = client.post("/api/webauthn/register/options",
                    headers={"Origin": origin})
    assert o.status_code == 200, o.text
    r = client.post("/api/webauthn/register",
                    json={"cid": o.json()["cid"],
                          "credential": pk.register(o.json()["publicKey"], origin)},
                    headers={"Origin": origin})
    return pk, r


def _passkey_login(client, pk, origin=LOCAL, *, sign_count=0, mangle=None):
    o = client.post("/api/webauthn/login/options", headers={"Origin": origin})
    if o.status_code != 200:
        return o
    cred = pk.assertion(o.json()["publicKey"],
                        origin if mangle is None else mangle,
                        sign_count=sign_count)
    return client.post("/api/webauthn/login",
                       json={"cid": o.json()["cid"], "credential": cred},
                       headers={"Origin": origin})


# ── policy: localhost is the only RP ────────────────────────────────────────

def test_rp_policy_is_localhost_only():
    assert passkeys.rp_for_host("localhost") == "localhost"
    assert passkeys.rp_for_host("LOCALHOST") == "localhost"
    assert passkeys.rp_for_host("127.0.0.1") is None
    assert passkeys.rp_for_host("::1") is None
    assert passkeys.rp_for_host("evil.example") is None
    assert passkeys.rp_for_host(None) is None
    assert passkeys.origin_ok(LOCAL, "localhost")
    assert passkeys.origin_ok("http://localhost", "localhost")
    assert not passkeys.origin_ok(IP, "localhost")
    assert not passkeys.origin_ok("https://evil.example", "localhost")
    assert not passkeys.origin_ok("", "localhost")


def test_ceremonies_refused_on_ip_host(ui):
    app, _ = ui
    owner = _owner(app)
    # same live session, browsed via the IP: no enrolment there
    ip_client = _client(app, base_url=IP)
    ip_client.cookies.set("spendglass_session",
                          owner.cookies.get("spendglass_session"))
    r = ip_client.post("/api/webauthn/register/options",
                       headers={"Origin": IP})
    assert r.status_code == 400
    assert "localhost" in r.json()["detail"]


# ── enrolment is never anonymous ────────────────────────────────────────────

def test_enrolment_requires_a_session(ui):
    app, _ = ui
    _owner(app)  # password exists, but THIS client is anonymous
    anon = _client(app)
    assert anon.post("/api/webauthn/register/options",
                     headers={"Origin": LOCAL}).status_code == 401
    assert anon.post("/api/webauthn/register", json={"cid": "x" * 24},
                     headers={"Origin": LOCAL}).status_code == 401
    assert anon.get("/api/webauthn/credentials").status_code == 401
    assert anon.post("/api/webauthn/credentials/remove",
                     json={"id": "x"}).status_code == 401


# ── the round trip ──────────────────────────────────────────────────────────

def test_enrol_then_unlock_with_passkey(ui):
    app, _ = ui
    pk, r = _enrol_passkey(_owner(app))
    assert r.status_code == 200 and r.json()["ok"]

    visitor = _client(app)
    assert visitor.get("/api/transactions").status_code == 401
    login = _passkey_login(visitor, pk)
    assert login.status_code == 200 and login.json()["ok"]
    assert visitor.cookies.get("spendglass_session")
    assert visitor.get("/api/health").status_code == 200


def test_session_reports_passkey_only_where_one_exists(ui):
    app, _ = ui
    owner = _owner(app)
    assert _client(app).get("/api/session").json()["passkey"] is False
    _enrol_passkey(owner)
    assert _client(app).get("/api/session").json()["passkey"] is True
    # the same install browsed via the IP quietly stays password-first
    assert _client(app, base_url=IP).get("/api/session").json()["passkey"] is False


def test_gate_markup_carries_the_passkey_path():
    # The gate is client-rendered; the page must ship the button, the
    # password fallback link, and the ceremony plumbing.
    for needle in ("Unlock with passkey", "Use your password instead",
                   "passkeyUnlock", "/api/webauthn/login/options",
                   "enrolPasskey"):
        assert needle in PAGE


def test_options_and_session_leak_no_credential_material(ui):
    app, _ = ui
    pk, _ = _enrol_passkey(_owner(app))
    anon = _client(app)
    options = anon.post("/api/webauthn/login/options",
                        headers={"Origin": LOCAL}).json()
    surfaces = json.dumps([options, anon.get("/api/session").json()])
    assert bytes_to_base64url(pk.cred_id) not in surfaces
    assert "public_key" not in surfaces
    assert not options["publicKey"].get("allowCredentials")


def test_password_fallback_still_works(ui):
    app, _ = ui
    _enrol_passkey(_owner(app))
    c = _client(app)
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200


# ── what must fail, fails ───────────────────────────────────────────────────

def test_assertion_for_another_origin_is_refused(ui):
    app, _ = ui
    pk, _ = _enrol_passkey(_owner(app))
    c = _client(app)
    r = _passkey_login(c, pk, mangle="https://evil.example")
    assert r.status_code == 403
    assert not c.cookies.get("spendglass_session")


def test_unknown_credential_is_refused(ui):
    app, _ = ui
    _enrol_passkey(_owner(app))
    assert _passkey_login(_client(app), SoftPasskey()).status_code == 403


def test_no_options_before_any_enrolment(ui):
    app, _ = ui
    _owner(app)
    assert _client(app).post("/api/webauthn/login/options",
                             headers={"Origin": LOCAL}).status_code == 400


def test_challenge_is_single_use(ui):
    app, _ = ui
    pk, _ = _enrol_passkey(_owner(app))
    c = _client(app)
    o = c.post("/api/webauthn/login/options",
               headers={"Origin": LOCAL}).json()
    cred = pk.assertion(o["publicKey"], LOCAL)
    body = {"cid": o["cid"], "credential": cred}
    assert c.post("/api/webauthn/login", json=body,
                  headers={"Origin": LOCAL}).status_code == 200
    assert _client(app).post("/api/webauthn/login", json=body,
                             headers={"Origin": LOCAL}).status_code == 403


def test_sign_count_regression_is_refused(ui):
    app, _ = ui
    pk, _ = _enrol_passkey(_owner(app))
    assert _passkey_login(_client(app), pk, sign_count=5).status_code == 200
    assert _passkey_login(_client(app), pk, sign_count=3).status_code == 403
    assert _passkey_login(_client(app), pk, sign_count=6).status_code == 200


def test_duplicate_enrolment_is_refused(ui):
    app, _ = ui
    owner = _owner(app)
    pk, first = _enrol_passkey(owner)
    assert first.status_code == 200
    assert _enrol_passkey(owner, pk=pk)[1].status_code == 409


# ── lifecycle: persistence, restart, removal ────────────────────────────────

def test_credentials_survive_a_restart_ceremonies_do_not(ui, tmp_path):
    app1, auth = ui
    pk, _ = _enrol_passkey(_owner(app1))
    o = _client(app1).post("/api/webauthn/login/options",
                           headers={"Origin": LOCAL}).json()

    app2 = create_app(tmp_path / "store.db",
                      Auth(auth_file=auth.auth_file, recovery_secret=SECRET))
    cred = pk.assertion(o["publicKey"], LOCAL)
    stale = _client(app2).post("/api/webauthn/login",
                               json={"cid": o["cid"], "credential": cred},
                               headers={"Origin": LOCAL})
    assert stale.status_code == 403          # ceremony died with app1
    assert _passkey_login(_client(app2), pk).status_code == 200  # credential lived


def test_removal_stops_unlocking_but_password_remains(ui):
    app, _ = ui
    owner = _owner(app)
    pk, _ = _enrol_passkey(owner)
    listed = owner.get("/api/webauthn/credentials").json()["credentials"]
    assert len(listed) == 1 and listed[0]["rp_id"] == "localhost"

    assert owner.post("/api/webauthn/credentials/remove",
                      json={"id": listed[0]["id"]}).status_code == 200
    assert owner.get("/api/webauthn/credentials").json()["credentials"] == []
    assert _client(app).post("/api/webauthn/login/options",
                             headers={"Origin": LOCAL}).status_code == 400
    assert _client(app).post("/api/login",
                             json={"password": PASSWORD}).status_code == 200


def test_stored_record_is_public_material_only_and_owner_only(ui):
    app, auth = ui
    pk, _ = _enrol_passkey(_owner(app))
    path = auth.auth_file.parent / "ui_passkeys.json"
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    raw = path.read_text()
    rec = json.loads(raw)["credentials"][0]
    assert rec["rp_id"] == "localhost"
    assert base64url_to_bytes(rec["id"]) == pk.cred_id
    priv = pk.key.private_numbers().private_value.to_bytes(32, "big")
    assert bytes_to_base64url(priv) not in raw
