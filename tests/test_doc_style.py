"""Documentation style budgets, enforced (#31, #77).

The docs drifted into a house style nobody chose: 73-word average sentences,
caveats stapled to every claim, bug history in reference prose. None of it
was catchable by review because none of it was measurable, so it accumulated
a paragraph at a time.

Two layers. Every markdown file in the repo is held to the hard ceilings: no
em-dash, no 55-word sentence, no 45-word table cell, a heading every 50 lines
of prose. A doc rewritten in the fleet's voice (CONTRIBUTING.md, Writing
documentation) is listed in CONVERTED and is also held to the rules that
guide marks (CI). The checks are crossband's, so the fleet's docs answer to
one set of rules. Taste is not automatable and is not attempted here: a doc
can pass every check and still fail the guide's one-line test.

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


def numbered_blocks(text):
    """Every block a reader reads, as (first line, kind, text). The kind is
    "prose" for a paragraph or a list item and "quote" for a quoted block.
    No fenced code, tables, headings or indented blocks, and no HTML
    comments, which nobody reads.

    Blocking matters. A bullet list rarely ends its items with a full stop, so
    joining the whole document into one string turns a fifteen-item dependency
    list into a single 98-word 'sentence' and reports a file that is fine.
    Lines inside one block are joined, so a sentence wrapped across source
    lines is still measured whole. The line reported is the block's first."""
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"),
                  text, flags=re.S)
    blocks, current, in_fence = [], [], False
    start = kind = None

    def flush():
        nonlocal current
        if current:
            blocks.append((start, kind, " ".join(current)))
        current = []

    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        this_kind = "quote" if stripped.startswith(">") else "prose"
        if this_kind == "quote":
            stripped = stripped.lstrip(">").strip()
        if current and this_kind != kind:
            flush()
        skip = (not stripped or stripped.startswith(("|", "#"))
                or line.startswith(("    ", "\t")))
        if skip or LIST_ITEM.match(stripped):
            flush()
            if skip:
                continue
            stripped = LIST_ITEM.sub("", stripped).strip()
        if not current:
            start, kind = n, this_kind
        current.append(stripped)
    flush()
    return blocks


def prose_blocks(text):
    """Body prose, split into the units a reader actually reads: one paragraph
    or one list item each. Quoted blocks are left out; the converted-doc
    rules take them from numbered_blocks() directly."""
    return [block for _, kind, block in numbered_blocks(text) if kind == "prose"]


def sentences_of(block):
    """Split one block on sentence-final punctuation, with one closing quote
    or bracket allowed after it. Emphasis markers are stripped first:
    `**...ends here.** Next one` puts the asterisks between the full stop and
    the space, which otherwise hides two sentences inside one measurement.
    Abbreviations and decimals are protected for the same reason, in the
    other direction."""
    guarded = re.sub(r"[*_]{1,3}", "", block)
    for abbr in ("e.g.", "i.e.", "etc.", "cf.", "vs.", "Dr.", "Mr.", "Ms.",
                 "St.", "approx.", "Fig.", "no."):
        guarded = guarded.replace(abbr, abbr.replace(".", "\x00"))
    guarded = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + "\x00" + m.group(2),
                     guarded)
    parts = re.split(r"(?:(?<=[.!?])|(?<=[.!?][\"')\]”]))\s+", guarded)
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


# ---------------------------------------------------------------------------
# The fleet's voice (CONTRIBUTING.md, Writing documentation), on the docs
# rewritten in it.
#
# A doc joins CONVERTED in the PR that rewrites it, and from then on every
# rule below holds it. Everything above runs on every markdown file.
# ---------------------------------------------------------------------------

CONVERTED = {
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "docs/CONFIG.md",
    "docs/MCP.md",
    "docs/MERCHANTS.md",
    "docs/OPERATIONS.md",
}

# Capitals are for acronyms. An all-capital word of three or more letters that
# is not one of these reads as shouting. Add one when a converted doc needs it;
# a name in a code span or a file name like README.md is never read as prose.
CAPS_ALLOWED = {"API", "CLI", "DNS", "GPT", "HTTP", "HTTPS", "ISO", "JSON",
                "CDR", "MCP", "MIT", "PDF", "SDK", "SQL", "STT", "URL", "WAV"}

# A colon may introduce a list, a command or a quoted value. Approximated as:
# what follows starts with a backtick or a quote, holds an inline list (two or
# more commas), or the sentence ends within this many words.
COLON_TAIL_WORDS = 12
# A bracketed aside is a name, a value or a pointer, under this many words.
ASIDE_WORDS = 8
# One contrast ("rather than", "instead of", "not just", "X, not Y") per this
# many sentences, and any doc may have two.
SENTENCES_PER_CONTRAST = 50

DASH = re.compile(r"[—–]|\s-\s")
ASIDE = re.compile(r"\(([^()]*)\)")
CAPS_WORD = re.compile(r"^([A-Z]{3,})(?:'s|s)?$")
FILLER = re.compile(
    r"\b(actually|genuinely|really|simply|literally|truly|honest|honestly|"
    r"deliberately|on purpose|by design|a conscious choice)\b", re.I)
LOOSE_EXACTLY = re.compile(r"\bexactly\b(?!\s*(?:\d|`))", re.I)
CONTRAST = re.compile(r"\b(?:rather than|instead of|not just)\b|, not\b", re.I)
HISTORY = re.compile(r"\b(used to|no longer|previously|any more)\b|#\d+", re.I)
SELF_REFERENCE = re.compile(
    r"\b(this page|this document|above|below|the next section)\b", re.I)
OPENER = re.compile(r"^(So|Because|Since|Given|Otherwise)\b")
# "Two rules still hold", "Three things catch what the lists miss": a count
# announced before the things themselves. Say the things.
COUNT = re.compile(
    r"^(?:One|Two|Three|Four|Five|Six|Seven|Several|A few|A couple of) "
    r"(?:more |other |last |final |small |quick )?"
    r"(?:things?|rules?|facts?|reasons?|ways?|points?|cases?|notes?|"
    r"caveats?|steps?|details?)\b", re.I)


def converted_docs():
    paths = [REPO / rel for rel in sorted(CONVERTED)]
    missing = [str(p.relative_to(REPO)) for p in paths if not p.exists()]
    assert not missing, f"CONVERTED names files that do not exist: {missing}"
    return paths


def reader_text(block):
    """A block as a reader reads it. A code span, a link target, a bare URL
    and a span in double quotes each become one token, so a word inside them
    is copied or mentioned, not used: the guide names every word it bans
    inside quotes, and a log line quoted whole may hold anything."""
    block = re.sub(r"`[^`]*`", "`code`", block)
    block = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", block)
    block = re.sub(r"https?://\S+", "url", block)
    block = re.sub(r'"[^"]*"|“[^”]*”', '"quoted"', block)
    return block


def converted_sentences(text):
    """(first line, kind, sentence) for every sentence a reader reads, quoted
    examples included: the guide's examples are held to its rules too."""
    out = []
    for line, kind, block in numbered_blocks(text):
        for sentence in sentences_of(reader_text(block)):
            sentence = sentence.strip()
            if sentence:
                out.append((line, kind, sentence))
    return out


def dash_offences(text):
    return [(n, "a dash in prose", s)
            for n, _, s in converted_sentences(text) if DASH.search(s)]


def semicolon_offences(text):
    return [(n, "a semicolon", s)
            for n, _, s in converted_sentences(text) if ";" in s]


def colon_offences(text):
    """One colon at most, and it introduces something (see COLON_TAIL_WORDS).
    A colon between two digits is a time or a ratio, not punctuation."""
    out = []
    for n, _, s in converted_sentences(text):
        parts = re.sub(r"(?<=\d):(?=\d)", "", s).split(":")
        if len(parts) > 2:
            out.append((n, "two colons in one sentence", s))
        elif len(parts) == 2:
            tail = parts[1].strip()
            introduces = (tail.startswith(("`", '"', "'", "“", "‘"))
                          or tail.count(",") >= 2
                          or len(tail.split()) <= COLON_TAIL_WORDS)
            if not introduces:
                out.append((n, "a colon followed by a clause, where the guide "
                            "allows a list, a command or a quoted value", s))
    return out


def bracket_offences(text):
    out = []
    for n, _, s in converted_sentences(text):
        if s.startswith("("):
            out.append((n, "a sentence that starts with a bracket", s))
        for aside in ASIDE.findall(s):
            words = len(aside.split())
            if words >= ASIDE_WORDS:
                out.append((n, f"a bracketed aside of {words} words", s))
    return out


def caps_offences(text):
    out = []
    for n, _, s in converted_sentences(text):
        for token in s.split():
            match = CAPS_WORD.match(
                token.strip("()[]{}.,;:!?\"'“”‘’*_"))
            if match and match.group(1) not in CAPS_ALLOWED:
                out.append((n, f"{match.group(1)} in capitals", s))
    return out


def filler_offences(text):
    out = []
    for n, _, s in converted_sentences(text):
        for m in FILLER.finditer(s):
            out.append((n, f'"{m.group(0)}"', s))
        for m in LOOSE_EXACTLY.finditer(s):
            out.append((n, '"exactly" before something that is neither a '
                        "number nor a value", s))
    return out


def contrast_offences(text):
    """Counted over the doc's own paragraphs and list items. A quoted example
    was counted once already, in the doc it was taken from."""
    sentences = [(n, s) for n, kind, s in converted_sentences(text)
                 if kind == "prose"]
    hits = [(n, m.group(0), s) for n, s in sentences
            for m in CONTRAST.finditer(s)]
    allowed = max(2, len(sentences) // SENTENCES_PER_CONTRAST)
    if len(hits) <= allowed:
        return []
    return [(n, f'contrast {i} of {len(hits)}, where {allowed} are allowed '
             f'in {len(sentences)} sentences: "{found.strip()}"', s)
            for i, (n, found, s) in enumerate(hits, 1)]


def history_offences(text):
    """Reference prose says what is true today. A quoted example is exempt:
    the guide quotes a changelog entry, which says what changed, and a log
    line that says a token stopped matching."""
    return [(n, f'"{m.group(0)}"', s)
            for n, kind, s in converted_sentences(text) if kind == "prose"
            for m in HISTORY.finditer(s)]


def self_reference_offences(text):
    return [(n, f'"{m.group(0)}"', s)
            for n, _, s in converted_sentences(text)
            for m in SELF_REFERENCE.finditer(s)]


def opener_offences(text):
    return [(n, f'a sentence opening with "{m.group(1)}"', s)
            for n, _, s in converted_sentences(text)
            for m in [OPENER.match(s)] if m]


def count_offences(text):
    return [(n, f'a count announced before the things: "{m.group(0)}"', s)
            for n, _, s in converted_sentences(text)
            for m in [COUNT.match(s)] if m]


def _offences(checker):
    found = []
    for path in converted_docs():
        for line, why, sentence in checker(path.read_text(errors="ignore")):
            found.append(
                f"{path.relative_to(REPO)}:{line}: {why}: {sentence[:100]}")
    return found


def _assert_clean(checker, rule):
    found = _offences(checker)
    assert not found, (
        f'{rule} (CONTRIBUTING.md, "Writing documentation"):\n  '
        + "\n  ".join(found))


def test_converted_docs_have_no_dashes():
    """No em-dash, no en-dash, no hyphen with a space either side. A hyphen
    only joins the halves of one word."""
    _assert_clean(dash_offences, "no dashes in prose")


def test_converted_docs_have_no_semicolons():
    _assert_clean(semicolon_offences, "no semicolons")


def test_converted_docs_use_a_colon_to_introduce_something():
    _assert_clean(colon_offences, "one colon per sentence, introducing a list, "
                  "a command or a quoted value")


def test_converted_docs_keep_brackets_short():
    _assert_clean(bracket_offences, f"a bracketed aside is under {ASIDE_WORDS} "
                  "words, and no sentence starts with one")


def test_converted_docs_keep_capitals_for_acronyms():
    _assert_clean(caps_offences, "capitals are for acronyms; add a real one to "
                  "CAPS_ALLOWED")


def test_converted_docs_avoid_the_filler_words():
    _assert_clean(filler_offences, "the words the guide bans")


def test_converted_docs_keep_contrasts_rare():
    _assert_clean(contrast_offences, "contrasts only where the reader would "
                  "assume the other thing")


def test_converted_docs_say_what_is_true_today():
    _assert_clean(history_offences, "no history or issue numbers in reference "
                  "prose; it belongs in the changelog and the issue")


def test_converted_docs_do_not_talk_about_themselves():
    _assert_clean(self_reference_offences, "no pointers to the doc itself; "
                  "link the heading")


def test_converted_docs_lead_with_the_claim():
    _assert_clean(opener_offences, "the reason or the caveat gets its own "
                  "sentence, after the claim")


def test_converted_docs_do_not_announce_a_count():
    _assert_clean(count_offences, "say the things, without announcing how "
                  "many are coming")


# Each checker on a string that breaks its rule, so a checker that silently
# stops matching fails here before it passes a real doc.

BAD_DOC = """\
# A doc that breaks every rule

The app is fast — it caches. It never waits – ever. It is quick - always.
It caches; it also waits.

The watcher waits: it asks the app whether it is busy and then it waits for
the answer before it does anything else at all.
Two colons: one here: and one there.

(This sentence starts with a bracket.) The app runs (in a way that nobody who
had seen it before would have expected) fine.

NEVER do this. It actually works, honestly, by design, and exactly like that.

So the app waits. Because it can. Since always. Given time. Otherwise not.

It used to be slow. It is no longer slow. Previously it was slow, and it is not
slow any more. See #342.

See the section below, and this page above. The next section says more.

A rather than B, C instead of D, E not just F, and G, not H.

Two things still hold. Three more rules follow.
"""

CLEAN_DOC = """\
# A doc that keeps every rule

Run `a - b; c` and read [docs/CONFIG.md](docs/CONFIG.md). The guide bans
"actually", so it is only mentioned here, in quotes. The API answers on
http://127.0.0.1:8903 in under a second, at 12:30 or any other time.

> Quoted examples count too. This one says "no longer" inside quotes.

<!-- an HTML comment is not prose, so "really" here is never read -->

- Symptom: the app hangs, the log fills, and nothing answers.
- Fix: set `SPENDGLASS_UI_PORT` to a free port and run `./start.sh`.
"""


def test_the_voice_checks_catch_a_bad_fixture():
    def whys(checker):
        return [why for _, why, _ in checker(BAD_DOC)]

    assert len(whys(dash_offences)) == 3
    assert whys(semicolon_offences) == ["a semicolon"]
    colons = whys(colon_offences)
    assert len(colons) == 2 and "two colons" in colons[1], colons
    brackets = whys(bracket_offences)
    assert brackets == ["a sentence that starts with a bracket",
                        "a bracketed aside of 13 words"], brackets
    assert whys(caps_offences) == ["NEVER in capitals"]
    fillers = whys(filler_offences)
    assert fillers[:3] == ['"actually"', '"honestly"', '"by design"'], fillers
    assert fillers[3].startswith('"exactly"'), fillers
    assert [w.split('"')[1] for w in whys(opener_offences)] == [
        "So", "Because", "Since", "Given", "Otherwise"]
    assert whys(history_offences) == [
        '"used to"', '"no longer"', '"Previously"', '"any more"', '"#342"']
    assert whys(self_reference_offences) == [
        '"below"', '"this page"', '"above"', '"The next section"']
    assert [w.split('"')[1] for w in whys(count_offences)] == [
        "Two things", "Three more rules"]
    contrasts = whys(contrast_offences)
    assert len(contrasts) == 4 and "where 2 are allowed" in contrasts[0], contrasts
    # Every offence names the block's first line, so the reader can find it.
    for checker in (dash_offences, colon_offences, contrast_offences):
        assert all(isinstance(line, int) and line > 1
                   for line, _, _ in checker(BAD_DOC))


def test_the_voice_checks_pass_a_clean_fixture():
    for checker in (dash_offences, semicolon_offences, colon_offences,
                    bracket_offences, caps_offences, filler_offences,
                    contrast_offences, history_offences,
                    self_reference_offences, opener_offences,
                    count_offences):
        assert checker(CLEAN_DOC) == [], checker.__name__


def test_quoted_examples_are_held_to_the_sentence_rules_only():
    """A quoted example must read like the doc around it, so the sentence
    rules see it. The two document rules (what is true today, how often it
    contrasts) read the doc's own paragraphs."""
    quoted = ("> The app — it waits; and it used to wait, rather than run.\n"
              "> It no longer does, and NEVER will.\n")
    assert dash_offences(quoted) and semicolon_offences(quoted)
    assert caps_offences(quoted) == [(1, "NEVER in capitals",
                                      "It no longer does, and NEVER will.")]
    assert history_offences(quoted) == []
    assert contrast_offences(quoted) == []


def test_contrasts_are_allowed_at_the_ceiling():
    two = "A rather than B. C instead of D. " + "Fine. " * 10
    assert contrast_offences(two) == []
    three = two + "E, not F."
    assert len(contrast_offences(three)) == 3
    long_doc = "Fine. " * 150 + "A rather than B. C instead of D. E, not F."
    assert contrast_offences(long_doc) == []


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


def test_config_reference_names_every_variable():
    """docs/CONFIG.md says it holds every setting. Each environment variable
    the app, its launcher or its supervisor installer reads must appear
    there, or the page quietly lags the code (#77 found REDBARK_API_URL,
    SPENDGLASS_AUTOSYNC and OPENAI_API_KEY missing)."""
    sources = sorted((REPO / "spendglass").glob("*.py")) + [
        REPO / "start.sh", REPO / "ops" / "install-supervisor.sh"]
    pattern = re.compile(
        r"\b(SPENDGLASS_[A-Z_]+|REDBARK_[A-Z_]+|ANTHROPIC_API_KEY|OPENAI_API_KEY)\b")
    read = {m for path in sources for m in pattern.findall(path.read_text())}
    assert "SPENDGLASS_DB" in read and "REDBARK_API_KEY" in read, read
    config_doc = (REPO / "docs" / "CONFIG.md").read_text()
    missing = sorted(v for v in read if f"`{v}" not in config_doc)
    assert not missing, f"variables the code reads but docs/CONFIG.md never names: {missing}"
