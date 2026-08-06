"""UI auth — recovery-secret bootstrap, durable password, hashed sessions.

- A RECOVERY SECRET is printed to the terminal at startup (stable if
  SPENDGLASS_RECOVERY_SECRET is set in .env, random per start otherwise). It
  gates first-run password enrollment and password reset — never everyday
  login.
- The everyday login is a durable PASSWORD (scrypt-hashed in
  data/ui_auth.json, which is gitignored via data/). It survives restarts.
- A successful login sets an opaque, expiring, server-revocable session
  cookie (24h). Sessions survive restarts: only SHA-256 digests of tokens
  are written to data/ui_sessions.json (gitignored), so the file never
  holds a usable bearer token, and a restart no longer logs browsers out.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

SESSION_TTL_SECONDS = 24 * 3600
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


class Auth:
    def __init__(self, auth_file: Path, recovery_secret: str | None = None):
        self.auth_file = auth_file
        self.recovery_secret = recovery_secret or secrets.token_urlsafe(24)
        self._sessions_file = auth_file.parent / "ui_sessions.json"
        # sha256(token) -> expiry epoch; loaded from disk, expired pruned
        self._sessions: dict[str, float] = self._load_sessions()
        self._failed_logins = 0

    def _load_sessions(self) -> dict[str, float]:
        try:
            raw = json.loads(self._sessions_file.read_text())
            now = time.time()
            return {k: float(v) for k, v in raw.items()
                    if isinstance(v, (int, float)) and v > now}
        except (OSError, ValueError):
            return {}

    def _save_sessions(self) -> None:
        self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
        self._sessions_file.write_text(json.dumps(self._sessions))

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    # ── password (durable) ──────────────────────────────────────────────────

    @property
    def first_run(self) -> bool:
        return not self.auth_file.is_file()

    def set_password(self, recovery_secret: str, password: str) -> bool:
        """First-run enrollment AND reset both prove possession of the secret."""
        if not hmac.compare_digest(recovery_secret, self.recovery_secret):
            return False
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        salt = os.urandom(16)
        self.auth_file.parent.mkdir(parents=True, exist_ok=True)
        self.auth_file.write_text(json.dumps({
            "salt": salt.hex(),
            "hash": _hash(password, salt),
            "created_at": int(time.time()),
        }))
        self._sessions.clear()  # a reset invalidates every existing session
        self._save_sessions()
        return True

    def check_password(self, password: str) -> bool:
        if self.first_run:
            return False
        stored = json.loads(self.auth_file.read_text())
        candidate = _hash(password, bytes.fromhex(stored["salt"]))
        ok = hmac.compare_digest(candidate, stored["hash"])
        if not ok:
            self._failed_logins += 1
            time.sleep(min(0.3 * self._failed_logins, 3.0))  # slow brute force
        else:
            self._failed_logins = 0
        return ok

    # ── sessions (in-memory, revocable, expiring) ───────────────────────────

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[self._digest(token)] = time.time() + SESSION_TTL_SECONDS
        self._save_sessions()
        return token

    def check_session(self, token: str | None) -> bool:
        if not token:
            return False
        digest = self._digest(token)
        expiry = self._sessions.get(digest)
        if expiry is None:
            return False
        if time.time() > expiry:
            del self._sessions[digest]
            self._save_sessions()
            return False
        return True

    def revoke_session(self, token: str | None) -> None:
        if token and self._sessions.pop(self._digest(token), None) is not None:
            self._save_sessions()
