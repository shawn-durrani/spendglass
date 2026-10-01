# Contributing

Thanks for looking. One person maintains Spendglass, it's built first
for that person's own use, and it's open because the patterns might
help someone else. Issues and pull requests are welcome, and how long
a reply takes varies.

## Development setup

```sh
git clone https://github.com/shawn-durrani/spendglass.git
cd spendglass
./start.sh                      # venv + deps + server on 127.0.0.1:8903
.venv/bin/python -m pytest -q   # the whole suite, no credentials needed
```

`start.sh` keeps the server running in the foreground, so run the
tests from a second terminal.

The suite has to pass with no API keys set. Every deterministic feature
works without a key, as [ARCHITECTURE.md](ARCHITECTURE.md) sets out,
and CI runs without keys to keep it that way. If your change only works
with a key present, it needs a fallback for when there isn't one.

For a scratch instance that can't touch a real store, point
`SPENDGLASS_DB` at a store in a folder of its own. Use a throwaway
`SPENDGLASS_RECOVERY_SECRET` and a different `SPENDGLASS_UI_PORT`.
Scheduled sync leaves an empty store at a path like that alone, so a
repo `.env` full of real credentials won't pull real bank data into it
by itself. [docs/CONFIG.md](docs/CONFIG.md#sync) says what that guard
does and how `SPENDGLASS_AUTOSYNC` overrides it.

## Before you commit

Turn on the pre-commit leak scan once per clone:

```sh
git config core.hooksPath .githooks
```

The scanner checks for the shapes of keys and for infrastructure
identifiers. It can also check a personal deny-list. Copy
`secret-scan-local.example` to `.secret-scan-local`, which git ignores,
and list patterns for your own names, places and account nicknames.

A green scan doesn't clear anything for publication. Fixtures and
examples have to be synthetic from the start, and passing the scanner
isn't enough. `tests/conftest.py` shows the house style, with
`Example Bank`, `acc-1`, and a test key that's plainly fake and is
split in two in the source so the scanner never sees a whole one.

The scan script is a byte-for-byte copy of crossband's, which is the
fleet's one scanner. `tests/test_secret_scan.py` fails when the copy
differs. It compares against `SECRET_SCAN_CANONICAL` when you set it,
then a crossband checkout beside this one or in `~/dev`, then the file
on GitHub, and it skips when it can reach none of them.

Don't patch the script here. Land the fix in crossband, which also
holds the Redbark key shape. Then copy the file across and commit it,
from a local checkout or with this command:

```sh
curl -fsSL https://raw.githubusercontent.com/shawn-durrani/crossband/main/scripts/secret-scan.sh -o scripts/secret-scan.sh
```

## Pull requests

- Open or claim an issue first. Everything ships as issue, then pull
  request, then merge, and small pull requests get reviewed faster.
- A change in behaviour comes with tests, and the suite stays green
  without keys.
- A change someone using the app can see gets one new file under
  `changelog.d/`, and `CHANGELOG.md` stays as it is. Name the file in
  lowercase words joined by hyphens, starting with the issue number,
  like `<issue>-<slug>.md`. Write the finished entry in the changelog's
  voice, as one paragraph that starts with `- `. Indent its
  continuation lines two spaces, and end the file with a newline.
  Entries fold into the changelog at release, so two open pull requests
  never touch the same line.
- Match the style of the file you're in. A comment says what the code
  can't show, such as a constraint, and doesn't narrate the next line.
- CI runs the tests and the scan, and both have to pass. `main` is
  protected.

## Writing documentation

Write a page the way you'd explain the app to a smart friend who's
never seen it, and if you wouldn't say a sentence like that, rewrite it
until you would. Write in Australian English. Contractions are fine,
the reader is "you", and short words beat long ones. Keep one claim to
a sentence, with an average under 18 words and fewer than one sentence
in ten over 35. A caveat gets a sentence of its own. A paragraph is one
thought, and it opens with its point. When you bring in
something from outside the app, say what it is in a sentence and link
its own documentation. Don't announce a count before a list, and don't
end a paragraph on a line that sounds good. Say a thing once, in one
place, and link to it from anywhere else. README.md is the page to
measure against.

`tests/test_doc_style.py` checks the mechanical part. Every markdown
file in the repo is held to the same ceilings: no em-dash, no sentence
over 55 words, no table cell over 45 words, and a heading at least
every 50 lines of prose. A doc rewritten in the voice is listed in
`CONVERTED` near the top of that file, and those docs also keep to
these rules:

- no dashes and no semicolons
- one colon per sentence, and only to introduce a list, a command or a
  quoted value
- bracketed asides under eight words, and no sentence starting with one
- capitals only for acronyms
- none of the filler words the test names
- no sentence that announces a count before the list
- contrasts, such as "X, not Y", kept rare
- no history and no issue numbers
- no pointers to the page itself
- no sentence opening with "So" or "Because"

When you rewrite a doc, add its path to `CONVERTED` in the same pull
request, and the suite tells you what's left. Test files are different,
because a test names the issue it guards.

The same file keeps two indexes whole. Every document in `docs/` is
linked from `docs/README.md`, and `docs/CONFIG.md` names every
environment variable the code reads.

## What gets a warm welcome

Bug reports with a way to reproduce them, portability fixes,
accessibility improvements, and anything that makes the plain-English
explainers plainer. A big new feature is worth discussing in an issue
before you write code. The scope boundaries in
[ARCHITECTURE.md](ARCHITECTURE.md) are settled, such as no investment
advice.

## Releasing

Versions are ordinary semantic versions in the 0.x range, with no
promise of stability yet. `spendglass.__version__` is the one place the
version lives.

Before a tag, tick every box:

- [ ] The suite is green without keys: `.venv/bin/python -m pytest -q`
      with no API keys set.
- [ ] `pip-audit -r requirements.txt --strict` is clean. It isn't in
      `requirements.txt`, so install it first.
- [ ] `bash scripts/secret-scan.sh --tree` is green. The bare command
      scans only staged lines, so at release time it would scan nothing
      and still report clean. `--tree` is the one that looks.
- [ ] No real personal data is in the code, tests, docs or fixtures.
- [ ] Screenshots and any demo database come from a synthetic store
      only, because real merchant names and amounts are personal data.
- [ ] `python scripts/fold_changelog.py vX.Y.Z` has run, leaving
      `changelog.d/` empty, the new section dated, and Unreleased empty
      above it.
- [ ] `__version__` is bumped.
