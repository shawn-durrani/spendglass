"""Provider capabilities — one registry dict per integration; everything
else derives from it.

- The probe is DATA, not code: a declarative HTTP request with a single
  `{key}` substitution, run before a key is ever persisted
  (validate-then-persist, never persist-then-hope).
- Config stores env var NAMES; values live only in .env (0600).
- Status APIs return {configured, valid, detail} — a key is never logged,
  echoed, or shown back. `valid: None` means "configured but not checked
  this session", an honest third state.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from .config import REPO_ROOT, _parse_env_file

CAPABILITIES: dict[str, dict] = {
    "anthropic": {
        "name": "Anthropic",
        "detail": "Powers every miner: merchant lookup, identity sweep, "
                  "classification, and decision propagation.",
        "unlocked": "Miners can run — lookup, sweep, classify and propagation "
                    "are all live.",
        "env": "ANTHROPIC_API_KEY",
        "placeholder": "Paste your Anthropic key (starts with sk-ant-…)",
        "get_key_url": "https://platform.claude.com/",
        "probe": {
            "method": "GET",
            "url": "https://api.anthropic.com/v1/models",
            "headers": {"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
        },
    },
    "openai": {
        "name": "OpenAI",
        "detail": "Optional second model family for future miners and "
                  "cross-checks. Nothing uses it yet.",
        "unlocked": "OpenAI key saved — available to future miners.",
        "env": "OPENAI_API_KEY",
        "placeholder": "Paste your OpenAI key (starts with sk-…)",
        "get_key_url": "https://platform.openai.com/api-keys",
        "probe": {
            "method": "GET",
            "url": "https://api.openai.com/v1/models",
            "headers": {"Authorization": "Bearer {key}"},
        },
    },
}


def _resolve(env_name: str) -> str | None:
    return os.environ.get(env_name) or _parse_env_file(
        REPO_ROOT / ".env").get(env_name)


def status() -> list[dict]:
    """Booleans out, secrets never out."""
    out = []
    for cid, cap in CAPABILITIES.items():
        out.append({
            "id": cid, "name": cap["name"], "detail": cap["detail"],
            "env": cap["env"], "placeholder": cap["placeholder"],
            "get_key_url": cap["get_key_url"],
            "configured": bool(_resolve(cap["env"])),
            "valid": None,  # not checked this session
        })
    return out


def probe_key(cid: str, key: str) -> tuple[bool, str]:
    """Run the capability's declarative probe. 429 counts as valid — the key
    authenticated, we were just rate-limited. Errors are sentences."""
    cap = CAPABILITIES[cid]
    p = cap["probe"]
    try:
        resp = httpx.request(
            p["method"], p["url"],
            headers={k: v.replace("{key}", key) for k, v in p["headers"].items()},
            timeout=15.0,
        )
    except httpx.HTTPError:
        return False, (f"Couldn't reach {cap['name']} — check your internet "
                       "connection. The key was not saved.")
    if resp.status_code in (401, 403):
        return False, (f"That key wasn't accepted by {cap['name']} — check you "
                       "copied all of it (keys are long and easy to cut short).")
    if resp.status_code == 429 or resp.status_code < 400:
        return True, cap["unlocked"]
    return False, (f"{cap['name']} answered with an unexpected error "
                   f"(HTTP {resp.status_code}). The key was not saved.")


def write_env_var(path: Path, name: str, value: str) -> None:
    """Line-preserving .env write: replace the first `NAME=`/`export NAME=`
    line, else append. Every other line, comment, and the ordering survive.
    File ends up 0600 either way."""
    lines = path.read_text().splitlines() if path.is_file() else []
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{name}=") or stripped.startswith(f"export {name}="):
            prefix = "export " if stripped.startswith("export ") else ""
            lines[i] = f"{prefix}{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def save_key(cid: str, key: str) -> dict:
    """Validate-then-persist. On success the key lands in .env AND
    os.environ, so it works with no restart. An invalid key is never
    written and never echoed back."""
    if cid not in CAPABILITIES:
        return {"valid": False, "error": "unknown service"}
    key = key.strip()
    if not key:
        return {"valid": False, "error": "No key provided."}
    ok, msg = probe_key(cid, key)
    if not ok:
        return {"valid": False, "error": msg}
    env_name = CAPABILITIES[cid]["env"]
    write_env_var(REPO_ROOT / ".env", env_name, key)
    os.environ[env_name] = key
    return {"valid": True, "unlocked": msg}
