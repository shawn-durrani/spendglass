"""Changelog fragments: two open PRs must never conflict on CHANGELOG.md (#44).

Every PR used to insert its entry at the same line, the top of Unreleased,
so any two open PRs conflicted there whatever else they touched. Entries now
ship as one file each under changelog.d/ and fold into CHANGELOG.md at
release, via scripts/fold_changelog.py. These tests pin the three things
that keep that true: Unreleased gains no new entries directly, every
fragment can actually fold, and the fold puts fragments where the reader
expects them.
"""
import hashlib
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = REPO / "changelog.d"

# The entries sitting under Unreleased when fragments arrived. They stay
# byte-identical until a release folds them away; any change means someone
# added an entry the old way, which is the conflict machine coming back.
FROZEN_SHA = "cb8f0c0b26d6d9f31af207e6ae596f808df9e309b3cb69c0fa84cf4939355d0b"


def _fold():
    spec = importlib.util.spec_from_file_location(
        "fold_changelog", REPO / "scripts" / "fold_changelog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unreleased_gains_no_new_entries_directly():
    """The guard. A user-visible change adds a file under changelog.d/, and
    CHANGELOG.md's Unreleased section stays exactly as the conversion froze
    it until a release empties it."""
    mod = _fold()
    _, _, entries, _ = mod.split_unreleased((REPO / "CHANGELOG.md").read_text())
    if not entries.strip():
        return  # a release folded everything away; nothing to guard
    assert hashlib.sha256(entries.encode()).hexdigest() == FROZEN_SHA, (
        "CHANGELOG.md's Unreleased section changed. New entries go in "
        "changelog.d/ as one file per change (see CONTRIBUTING.md); move "
        "the entry there and revert the CHANGELOG edit."
    )


def test_every_fragment_can_fold():
    """A fragment that cannot fold is a release-day surprise. Catch it in
    the PR that adds it instead."""
    mod = _fold()
    problems = {
        f.name: p
        for f in sorted(FRAGMENT_DIR.glob("*.md"))
        if (p := mod.fragment_problems(f.name, f.read_text()))
    }
    assert not problems, problems


def test_fold_puts_fragments_above_the_frozen_entries():
    """Newest first is the file's law. Fragments are newer than anything
    already under Unreleased, so they land above it in the new section."""
    mod = _fold()
    changelog = (
        "# Changelog\n\nHouse convention.\n\n"
        "## Unreleased\n\nPointer paragraph.\n\n"
        "- Old entry, already placed (#1).\n\n"
        "## v0.1.0 (2026-08-06)\n\n- Ancient (#0).\n"
    )
    out = mod.fold(changelog, ["- Newer (#3).\n", "- New (#2).\n"],
                   "v0.2.0", "2026-09-01")
    section = out.split("## v0.2.0 (2026-09-01)\n")[1].split("## v0.1.0")[0]
    order = [ln for ln in section.splitlines() if ln.startswith("- ")]
    assert order == ["- Newer (#3).", "- New (#2).",
                     "- Old entry, already placed (#1)."], order
    assert "\n\n\n" not in out


def test_fold_leaves_a_fresh_empty_unreleased_that_passes_the_guard():
    """After a release the guard's empty branch takes over, so the release
    never has to edit this test. Pin that the folded file really does read
    as empty to the same parser."""
    mod = _fold()
    changelog = (
        "# Changelog\n\n"
        "## Unreleased\n\nPointer paragraph.\n\n"
        "- Old entry (#1).\n\n"
        "## v0.1.0 (2026-08-06)\n\n- Ancient (#0).\n"
    )
    out = mod.fold(changelog, ["- New (#2).\n"], "v0.2.0", "2026-09-01")
    _, preamble, entries, _ = mod.split_unreleased(out)
    assert "Pointer paragraph." in preamble
    assert entries == ""


def test_fragment_rules_name_each_problem():
    """The problems list is what a contributor sees in CI; each rule speaks
    once and plainly."""
    mod = _fold()
    assert mod.fragment_problems("44-changelog-fragments.md",
                                 "- One entry (#44).\n  Indented tail.\n") == []
    assert mod.fragment_problems("Bad_Name.md", "- Fine (#1).\n")
    assert mod.fragment_problems("ok.md", "not an entry\n")
    assert mod.fragment_problems("ok.md", "- One (#1).\n- Two (#2).\n")
    assert mod.fragment_problems("ok.md", "- One (#1).\nno indent\n")
    assert mod.fragment_problems("ok.md", "- No newline (#1).")
