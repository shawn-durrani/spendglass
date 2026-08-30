#!/usr/bin/env python3
"""Fold changelog.d fragments into CHANGELOG.md for a release (#44).

Unreleased entries live as one file per change under changelog.d/, so two
open PRs never edit the same line of CHANGELOG.md. This script runs at
release: it moves every fragment into the new version's section, newest
first, above any entries already sitting under Unreleased, and leaves a
fresh empty Unreleased behind.

Usage:
    python scripts/fold_changelog.py v0.3.0 [--date 2026-09-01]

The date defaults to today. The script edits CHANGELOG.md and deletes the
fragments; review the diff and commit it as part of the release PR.
"""
import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = REPO / "changelog.d"
FRAGMENT_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\.md$")


def split_unreleased(text):
    """(before, preamble, entries, after) around the Unreleased section.

    The preamble is whatever prose sits under the heading before the first
    entry; the entries run from the first `- ` line to the next `## `
    heading. One parse shared with the tests, so the guard and the fold can
    never disagree about where an entry is."""
    m = re.search(r"^## Unreleased\n(.*?)(?=^## )", text, re.M | re.S)
    if not m:
        raise SystemExit("CHANGELOG.md has no Unreleased section")
    body = m.group(1)
    e = re.search(r"^- ", body, re.M)
    preamble, entries = (body, "") if not e else (body[: e.start()], body[e.start():])
    return text[: m.start(1)], preamble, entries, text[m.end(1):]


def fragment_problems(name, body):
    """Why this fragment cannot fold, or an empty list. An entry is one
    `- ` paragraph; continuation lines are blank or indented two spaces,
    exactly as they would sit in CHANGELOG.md."""
    problems = []
    if not FRAGMENT_NAME.match(name):
        problems.append("name must be lowercase-hyphen words ending .md, "
                        "e.g. 277-changelog-fragments.md")
    lines = body.splitlines()
    if not lines or not lines[0].startswith("- "):
        problems.append("the first line must start with '- '")
    for ln in lines[1:]:
        if ln.startswith("- "):
            problems.append("one entry per fragment; a second change gets "
                            "its own file")
            break
        if ln and not ln.startswith("  "):
            problems.append("continuation lines are indented two spaces")
            break
    if body and not body.endswith("\n"):
        problems.append("the file must end with a newline")
    return problems


def fold(text, fragment_bodies, version, date):
    """The folded CHANGELOG text. fragment_bodies arrive newest first and
    land above any entries already under Unreleased, which are already in
    their final order."""
    before, preamble, entries, after = split_unreleased(text)
    parts = [b.rstrip("\n") for b in fragment_bodies]
    if entries.strip():
        parts.append(entries.strip("\n"))
    section = f"## {version} ({date})\n\n" + "\n\n".join(parts) + "\n\n"
    return before + preamble + section + after


def _added_ts(path):
    """When git first saw this fragment, for newest-first ordering. A file
    git has never seen sorts newest, which is where a just-written entry
    belongs."""
    r = subprocess.run(
        ["git", "log", "--diff-filter=A", "--format=%ct", "-1", "--",
         str(path.relative_to(REPO))],
        cwd=REPO, capture_output=True, text=True)
    out = r.stdout.strip()
    return int(out) if r.returncode == 0 and out else float("inf")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("version", help="e.g. v0.3.0")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args(argv)

    fragments = sorted(FRAGMENT_DIR.glob("*.md"))
    if not fragments:
        raise SystemExit("changelog.d/ holds no fragments; nothing to fold")
    bad = {}
    for f in fragments:
        problems = fragment_problems(f.name, f.read_text())
        if problems:
            bad[f.name] = problems
    if bad:
        for name, problems in bad.items():
            print(f"{name}: " + "; ".join(problems), file=sys.stderr)
        raise SystemExit("fix the fragments above, then rerun")

    fragments.sort(key=lambda f: (_added_ts(f), f.name), reverse=True)
    changelog = REPO / "CHANGELOG.md"
    changelog.write_text(fold(changelog.read_text(),
                              [f.read_text() for f in fragments],
                              args.version, args.date))
    for f in fragments:
        f.unlink()
    print(f"folded {len(fragments)} fragment(s) into {args.version}; "
          "review the diff and commit")


if __name__ == "__main__":
    main()
