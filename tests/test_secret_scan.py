"""The scan CI runs: the whole committed tree must be clean, always.

Also pins the scanner's load-bearing behaviours so the docs can't drift:
a real-shaped rbk_ key is caught, the .env.example placeholder passes, and
the personal-content class fails on a deny-listed pattern, runs from a
gitignored local file, and says loudly when it was skipped.

The script itself is a byte-for-byte copy of crossband's, the fleet's canonical
scanner. A pattern fix lands there first and is copied here; the last test in
this file hashes the copy against the canonical so a copy that differs is a red
build rather than a scanner that quietly misses what the others catch. That
one test reads a local crossband checkout when there is one and otherwise
downloads the canonical with a short timeout, skipping (visibly) when it
cannot; everything else stays keyless and offline.
"""

import hashlib
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCAN = REPO / "scripts" / "secret-scan.sh"


def _scan(*args: str, local_list: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Point class 3 somewhere explicit so tests behave the same on a machine
    # with a real .secret-scan-local and in CI without one.
    env["SECRET_SCAN_LOCAL"] = local_list if local_list else "/nonexistent"
    return subprocess.run(
        ["bash", str(SCAN), *args],
        capture_output=True, text=True, cwd=REPO, env=env,
    )


def test_tree_scan_is_clean():
    result = _scan("--tree")
    assert result.returncode == 0, f"committed tree has a leak:\n{result.stdout}"


def test_tree_scan_is_clean_against_local_denylist():
    """On a machine with a real deny-list, the tree must also be free of
    personal content. In CI (no file) this degrades to the plain tree scan."""
    real = REPO / ".secret-scan-local"
    result = _scan("--tree", local_list=str(real) if real.exists() else None)
    assert result.returncode == 0, f"personal content in the tree:\n{result.stdout}"


def test_scanner_catches_rbk_key_shape(tmp_path):
    hot = tmp_path / "leaky.txt"
    hot.write_text("REDBARK_API_KEY=rbk_live_" + "ab12" * 16 + "\n")
    result = _scan("--files", str(hot))
    assert result.returncode == 1, "a real-shaped rbk_live_ key must be caught"
    assert "CREDENTIAL" in result.stdout


def test_placeholder_passes():
    result = _scan("--files", str(REPO / ".env.example"))
    assert result.returncode == 0, "the documented placeholder must not trip the scan"


def test_local_denylist_catches_personal_content(tmp_path):
    deny = tmp_path / "deny"
    deny.write_text("# a name and a bank reference\njane citizen\n\\b9990001\\b\n")
    hot = tmp_path / "doc.txt"
    hot.write_text("Paid JANE CITIZEN, ref 9990001.\n")
    result = _scan("--files", str(hot), local_list=str(deny))
    assert result.returncode == 1, "deny-listed personal content must be caught"
    assert "PERSONAL CONTENT" in result.stdout


def test_missing_denylist_is_skipped_loudly(tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("nothing personal here\n")
    result = _scan("--files", str(ok))
    assert result.returncode == 0
    assert "SKIPPED" in result.stdout, "a skipped class must say so, not imply coverage"


# ── published files are never pre-exempt ─────────────────────────────────────
def test_requirements_txt_is_not_exempt():
    """requirements.txt is tracked and ships, so it must face every matcher.
    The exclusion existed for lockfile noise the file never contained. A
    published file on the exclude list is a silent pre-exemption for
    whatever lands in it later. Exclusions can come from the script's
    built-in list or from a repo's .secret-scan-exclude, so both are read."""
    assert ":(exclude)requirements.txt" not in SCAN.read_text()
    listing = REPO / ".secret-scan-exclude"
    if listing.exists():
        entries = [ln.split("#", 1)[0].strip() for ln in listing.read_text().splitlines()]
        assert "requirements.txt" not in entries


# ── drift: this file is a copy of crossband's canonical scanner ──────────────
#
# The fleet runs ONE scanner. crossband owns it; this repo carries it byte for
# byte, so a pattern fix lands there first and is copied here. The copy is
# hashed against the canonical so a copy that differs is a red build rather
# than a scanner that quietly misses what the others catch.

CANONICAL_URL = ("https://raw.githubusercontent.com/shawn-durrani/crossband/"
                 "main/scripts/secret-scan.sh")
SYNC_HINT = (
    "scripts/secret-scan.sh is a byte-for-byte copy of crossband's canonical "
    "scanner. Pattern fixes land in crossband first; do not patch this copy. "
    "Sync it with:\n"
    f"  curl -fsSL {CANONICAL_URL} -o scripts/secret-scan.sh\n"
    "or copy the file from a local crossband checkout, then commit."
)


def _canonical_scanner():
    """The canonical's bytes and where they came from, or (None, why not).
    In order: an explicit SECRET_SCAN_CANONICAL path (which must exist), a
    sibling or home-directory crossband checkout, else a download with a
    short timeout so the suite stays runnable offline."""
    explicit = os.environ.get("SECRET_SCAN_CANONICAL")
    if explicit:
        p = Path(explicit)
        assert p.is_file(), f"SECRET_SCAN_CANONICAL={explicit} is not a file"
        return p.read_bytes(), f"SECRET_SCAN_CANONICAL={explicit}"
    for p in (REPO.parent / "crossband" / "scripts" / "secret-scan.sh",
              Path.home() / "dev" / "crossband" / "scripts" / "secret-scan.sh"):
        if p.is_file():
            return p.read_bytes(), str(p)
    try:
        with urllib.request.urlopen(CANONICAL_URL, timeout=10) as resp:
            return resp.read(), CANONICAL_URL
    except Exception as exc:  # offline, a proxy, a GitHub hiccup
        return None, f"{CANONICAL_URL} ({exc.__class__.__name__}: {exc})"


def test_scanner_matches_canonical():
    canonical, source = _canonical_scanner()
    if canonical is None:
        pytest.skip(f"the canonical scanner is not reachable: {source}. "
                    "CI has network and checks this; offline it cannot.")
    mine = hashlib.sha256(SCAN.read_bytes()).hexdigest()
    theirs = hashlib.sha256(canonical).hexdigest()
    assert mine == theirs, (
        f"scripts/secret-scan.sh has drifted from the canonical at {source}\n"
        f"  local     sha256 {mine}\n  canonical sha256 {theirs}\n{SYNC_HINT}")
