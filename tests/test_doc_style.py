"""Documentation style budgets, enforced (#31).

The docs drifted into a house style nobody chose: 73-word average sentences,
caveats stapled to every claim, bug history in reference prose. None of it
was catchable by review because none of it was measurable, so it accumulated
a paragraph at a time.

These are the hard limits only. The full rules are in CONTRIBUTING.md under
"Writing documentation"; taste is not automatable and is not attempted here.
What this stops is the shape that made the docs unreadable: a sentence that
never ends, a table cell that swallowed a section, a file with no way in.

Prose measurement excludes fenced code blocks, tables and headings, so a
long SQL line or a wide table never trips a prose budget.
"""
from pathlib import Path
import re

REPO = Path(__file__).resolve().parents[1]

# Vendored, generated, or not ours.
SKIP_DIRS = {".venv", "venv", "node_modules", ".git", ".pytest_cache",
             "__pycache__", "data", "dist", "build", "site-packages"}

MAX_SENTENCE_WORDS = 55
MAX_CELL_WORDS = 45
HEADING_EVERY = 50
HEADING_EXEMPT_UNDER = 150


def tracked_docs():
    """Every markdown file that is ours, longest first for readable failures."""
    out = []
    for path in REPO.rglob("*.md"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")


def prose_blocks(text):
    """Body prose, split into the units a reader actually reads: one paragraph
    or one list item each. No fenced code, tables, headings, or indented
    blocks.

    Blocking matters. A bullet list rarely ends its items with a full stop, so
    joining the whole document into one string turns a fifteen-item dependency
    list into a single 98-word 'sentence' and reports a file that is fine.
    Lines inside one block are joined, so a sentence wrapped across source
    lines is still measured whole."""
    blocks, current, in_fence = [], [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        skip = (not stripped or stripped.startswith(("|", "#", ">"))
                or line.startswith(("    ", "\t")))
        if skip or LIST_ITEM.match(line):
            if current:
                blocks.append(" ".join(current))
                current = []
            if skip:
                continue
            stripped = LIST_ITEM.sub("", line).strip()
        current.append(stripped)
    if current:
        blocks.append(" ".join(current))
    return blocks


def sentences_of(block):
    """Split one block on sentence-final punctuation. Emphasis markers are
    stripped first: `**...ends here.** Next one` puts the asterisks between
    the full stop and the space, which otherwise hides two sentences inside
    one measurement. Abbreviations and decimals are protected for the same
    reason, in the other direction."""
    guarded = re.sub(r"[*_]{1,3}", "", block)
    for abbr in ("e.g.", "i.e.", "etc.", "cf.", "vs.", "Dr.", "Mr.", "Ms.",
                 "St.", "approx.", "Fig.", "no."):
        guarded = guarded.replace(abbr, abbr.replace(".", "\x00"))
    guarded = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + "\x00" + m.group(2),
                     guarded)
    parts = re.split(r"(?<=[.!?])\s+", guarded)
    return [p.replace("\x00", ".") for p in parts]


def word_count(text):
    """Words a reader actually parses. Inline code spans and link targets are
    one token each: a bare URL is not ten words of prose."""
    text = re.sub(r"`[^`]*`", " CODE ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " URL ", text)
    return len(text.split())


def test_no_em_dashes():
    """House style, and the one rule that is purely mechanical. A stray
    em-dash in a sample payload is worse than one in prose: it gets copied
    into other people's code."""
    offenders = []
    for path in tracked_docs():
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if "—" in line:
                offenders.append(f"{path.relative_to(REPO)}:{n}")
    assert not offenders, (
        "em-dash is not house style; use a comma, a colon, or two sentences:\n  "
        + "\n  ".join(offenders))


def test_no_runaway_sentences():
    """A sentence past this length has stopped being one claim. The record
    before this test existed was 1,004 words."""
    offenders = []
    for path in tracked_docs():
        for block in prose_blocks(path.read_text(errors="ignore")):
            for sentence in sentences_of(block):
                n = word_count(sentence)
                if n > MAX_SENTENCE_WORDS:
                    offenders.append(
                        f"{path.relative_to(REPO)}: {n} words - {sentence[:90]}...")
    assert not offenders, (
        f"sentences over {MAX_SENTENCE_WORDS} words ({len(offenders)}); "
        "split them:\n  " + "\n  ".join(offenders[:20]))


def test_no_essays_in_table_cells():
    """A reference table is scanned, not read. Past this length the cell has
    become a section and belongs in prose with a link."""
    offenders = []
    for path in tracked_docs():
        in_fence = False
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not line.strip().startswith("|"):
                continue
            for cell in line.split("|"):
                words = word_count(cell)
                if words > MAX_CELL_WORDS:
                    offenders.append(
                        f"{path.relative_to(REPO)}:{n}: {words} words in one cell")
    assert not offenders, (
        f"table cells over {MAX_CELL_WORDS} words; move the detail into prose "
        "and link it:\n  " + "\n  ".join(offenders))


def test_long_docs_stay_navigable():
    """An unbroken wall has no way in. The worst case before this test was a
    335-line section under a single heading.

    CHANGELOG.md is exempt, and the exemption is about genre rather than
    convenience: a changelog's headings are its releases, so the gap between
    two of them is however much shipped in between. Forcing one every 50 lines
    would mean inventing headings that describe nothing. Its entries are still
    held to the sentence budget, which is where changelog prose actually goes
    wrong."""
    offenders = []
    for path in tracked_docs():
        if path.name == "CHANGELOG.md":
            continue
        lines = path.read_text(errors="ignore").splitlines()
        if len(lines) < HEADING_EXEMPT_UNDER:
            continue

        # Only PROSE lines count toward a gap. A fenced block, a table and a
        # list are all navigable already: a numbered procedure is indexed by
        # its own numbers, and a table by its rows. Counting them would push
        # a heading into the middle of a 1..6 list, which renumbers it from 1
        # and makes the document worse to satisfy the guard.
        marks, in_fence, prose = [(0, 0)], False, 0
        for n, line in enumerate(lines, 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence and re.match(r"#{2,6}\s", line):
                marks.append((n, prose))
                continue
            stripped = line.strip()
            skip = (in_fence or not stripped or stripped.startswith(("|", ">"))
                    or LIST_ITEM.match(line) or line.startswith(("    ", "\t")))
            if not skip:
                prose += 1
        marks.append((len(lines), prose))
        widest, where = 0, 0
        for (_, pa), (nb, pb) in zip(marks, marks[1:]):
            if pb - pa > widest:
                widest, where = pb - pa, nb
        if widest > HEADING_EVERY:
            offenders.append(
                f"{path.relative_to(REPO)}: {widest} lines of unbroken prose "
                f"ending near line {where}")
    assert not offenders, (
        f"add a heading at least every {HEADING_EVERY} lines:\n  "
        + "\n  ".join(offenders))


def test_docs_index_stays_complete():
    """docs/README.md must link every document in docs/.

    An index with holes is worse than none, because it teaches readers that
    the unlisted files do not exist. Copied from crossband, where the index
    is the entry point both humans and AI sessions are pointed at.

    The href is checked, not the link text: a first draft of this accepted the
    filename anywhere on the page and passed on link TEXT while the href was
    broken."""
    index = (REPO / "docs" / "README.md").read_text()
    for path in sorted((REPO / "docs").glob("*.md")):
        if path.name == "README.md":
            continue
        assert f"({path.name}" in index, (
            f"docs/{path.name} is not linked from docs/README.md; index it")

    for entry in ("README.md", "CLAUDE.md"):
        text = (REPO / entry).read_text()
        assert "docs/README.md" in text, (
            f"{entry} must link the docs index, or it is invisible to the "
            "audience it was written for")
