"""Passkey (WebAuthn) credentials for the UI gate (#22).

The everyday unlock becomes a platform-authenticator passkey (Touch ID on
the Mac this runs on); the password stays as the fallback and the recovery
secret keeps its first-run/reset role unchanged. A passkey replaces only the
password proof — sessions (auth.py) are untouched.

Mirrors membro's design (membro#27), narrowed to spendglass's world: this UI
is loopback-only, so the single Relying Party is `localhost`. A passkey is
bound to the exact origin it was created on, and an IP address is not a
valid RP at all (browser rule, verified live on the membro build), so
browsing via 127.0.0.1 simply keeps the password-first gate. There is no
trusted-host machinery here because there is no non-loopback surface.

What lives here is storage and the origin policy, never a ceremony: the
challenge lifecycle and the cryptographic verification (py_webauthn) live in
the HTTP layer, the same split auth.py draws. Only the credential's PUBLIC
key and id are stored — in data/ui_passkeys.json beside the password record,
owner-only like everything in the store directory (issue #23 tightens it on
every start). The private half lives in the Secure Enclave / iCloud Keychain
and never reaches this process, so a copied store cannot impersonate the
owner's passkey.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def rp_for_host(host: str | None) -> str | None:
    """The WebAuthn Relying Party ID a request on `host` may use, or None if
    passkeys cannot work there. Loopback-only service, so the policy is one
    line of intent: `localhost` is the only valid RP — IP addresses are
    refused by browsers as RP IDs, and no other hostname reaches this app
    (the Host allowlist sees to that)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return None
    try:
        ipaddress.ip_address(h.strip("[]"))
        return None
    except ValueError:
        pass
    return h if h == "localhost" else None


def origin_ok(origin: str | None, host: str | None) -> bool:
    """True iff `origin` (an Origin request header) names this same host
    acceptably: plain http is fine only because the host can only ever be
    localhost, a secure context by definition."""
    try:
        parts = urlsplit((origin or "").strip())
        o_host = (parts.hostname or "").lower()
    except ValueError:
        return False
    h = (host or "").strip().lower().rstrip(".")
    if not o_host or o_host != h:
        return False
    return parts.scheme in ("http", "https") and o_host == "localhost"


class PasskeyStore:
    """The durable credential list, one JSON file beside ui_auth.json. Every
    method is total: a missing or malformed file reads as "no passkeys", so
    the gate always renders whatever state the store is in."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))
        try:
            self.path.chmod(0o600)  # owner-only from the first byte, not
        except OSError:             # just from the next startup repair (#23)
            pass

    def list_credentials(self) -> list[dict]:
        rows = self._read().get("credentials", [])
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def credentials_for_rp(self, rp_id: str) -> list[dict]:
        return [r for r in self.list_credentials() if r.get("rp_id") == rp_id]

    def add_credential(self, rec: dict) -> None:
        data = self._read()
        data.setdefault("credentials", [])
        data["credentials"] = self.list_credentials() + [rec]
        self._write(data)

    def remove_credential(self, cred_id: str) -> bool:
        rows = self.list_credentials()
        kept = [r for r in rows if r.get("id") != cred_id]
        if len(kept) == len(rows):
            return False
        data = self._read()
        data["credentials"] = kept
        self._write(data)
        return True

    def update_sign_count(self, cred_id: str, sign_count: int) -> None:
        """Persist the authenticator's signature counter so a cloned
        credential (counter going backwards) is detectable next login.
        Apple platform authenticators report a constant 0; that stores and
        verifies fine."""
        data = self._read()
        rows = self.list_credentials()
        for r in rows:
            if r.get("id") == cred_id:
                r["sign_count"] = int(sign_count)
        data["credentials"] = rows
        self._write(data)

    def user_handle(self) -> bytes:
        """One stable random WebAuthn user handle for the single owner,
        minted on first use. Deliberately NOT personal data — 16 random
        bytes satisfy every authenticator and identify nothing."""
        data = self._read()
        stored = data.get("user_handle", "")
        if stored:
            try:
                return bytes.fromhex(stored)
            except ValueError:
                pass
        handle = secrets.token_bytes(16)
        data["user_handle"] = handle.hex()
        self._write(data)
        return handle
