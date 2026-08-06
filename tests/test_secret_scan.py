"""The scan CI runs: the whole committed tree must be clean, always.

Also pins the scanner's load-bearing behaviors so the docs can't drift:
a real-shaped rbk_ key is caught, the .env.example placeholder passes, and
the personal-content class fails on a deny-listed pattern, runs from a
gitignored local file, and says loudly when it was skipped.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _scan(*args: str, local_list: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Point class 3 somewhere explicit so tests behave the same on a machine
    # with a real .secret-scan-local and in CI without one.
    env["SECRET_SCAN_LOCAL"] = local_list if local_list else "/nonexistent"
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "secret-scan.sh"), *args],
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
