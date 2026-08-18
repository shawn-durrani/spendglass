# Contributing

Thanks for looking. Honest framing first: Spendglass is solo-maintained,
built primarily for the maintainer's own use, and opened up because the
patterns might be useful to others. Issues and PRs are welcome; response
times vary.

## Development setup

```sh
git clone https://github.com/shawn-durrani/spendglass.git
cd spendglass
./start.sh                      # venv + deps + server on 127.0.0.1:8903
.venv/bin/python -m pytest -q   # the whole suite, no credentials needed
```

The suite must pass **with no API keys configured**: keyless degradation
is a design rule ([ARCHITECTURE.md](ARCHITECTURE.md)), and CI runs
keyless on purpose. If your change only works with a key present, it
needs a keyless fallback.

For a scratch instance that can't touch a real store, point
`SPENDGLASS_DB` somewhere disposable and use a throwaway
`SPENDGLASS_RECOVERY_SECRET` on a different `SPENDGLASS_UI_PORT`.
Scheduled sync stays inert on its own: an empty store at a non-default
`SPENDGLASS_DB` trips the scratch guard, so a repo `.env` full of real
credentials cannot pull real bank data into it. `SPENDGLASS_AUTOSYNC=0`
switches scheduled sync off outright; `=1` overrides the guard.

## Before you commit

Enable the pre-commit leak scan once per clone:

```sh
git config core.hooksPath .githooks
```

The scanner checks key shapes and infrastructure identifiers, plus an
optional personal deny-list: copy `secret-scan-local.example` to
`.secret-scan-local` (gitignored) and list patterns for your own names,
places, and account nicknames. **A green scan is not a publication
clearance**: fixtures and examples must be synthetic by construction,
not merely scanner-approved. `tests/conftest.py` shows the house style:
`Example Bank`, `acc-1`, shape-invalid keys.

## Pull requests

- Open or claim an issue first; everything ships issue → PR → merge,
  and small PRs review faster.
- Tests accompany behaviour changes; the suite stays keyless-green.
- User-visible changes get one line in `CHANGELOG.md` under Unreleased.
- Code style: match the file you're in. Comments state constraints the
  code can't show, not narration of what the next line does.
- CI (test + scan) must pass; `main` is protected.

## Writing documentation

Budgets, not taste. `tests/test_doc_style.py` enforces the hard limits;
the rest is review. The house reference is a 15.5-word average sentence
with 4% of sentences over 35 words.

- One claim per sentence. Average under 18 words, and keep sentences over
  35 words under 10% of a document.
- No em-dashes. Australian English. Plain English over jargon.
- Caveats earn their own sentence. Appending a limitation to every claim
  is how the important ones stop reading as important.
- Antithesis ("X, not Y", "rather than", "instead of") is a tool, not a
  cadence. If deleting the "not Y" half loses no information, delete it.
- Never announce your own honesty. "Stated plainly", "the honest reason":
  delete the phrase, keep the fact.
- Issue numbers and bug history go in the CHANGELOG and the issue.
  Reference prose says what is true now.
- Do not narrate a document's own structure or edit history. Nobody read
  the previous version.
- A table cell holds a value and a sentence, not a section.
- Headings every 30 to 50 lines of prose, so a section can be navigated.
- Every document in `docs/` is linked from `docs/README.md`, and a test
  enforces it.
- Say a thing once. Two copies of a rule is one copy that will go stale.

## What gets a warm welcome

Bug reports with reproductions, portability fixes, accessibility
improvements, and anything that makes the plain-English explainers
plainer. Big new features are worth an issue discussion before code;
the scope boundaries in ARCHITECTURE.md (notably: no investment-advice
derivation) are deliberate.

## Releasing

Ordinary semantic versions in the 0.x range: no stability promise yet.
`spendglass.__version__` is the single source.

Before a tag, every box:

- [ ] Suite green keyless: `.venv/bin/python -m pytest -q` with no API keys set
- [ ] `pip-audit -r requirements.txt --strict` clean
- [ ] `bash scripts/secret-scan.sh --tree` green. The bare command scans
      only staged lines, so at release time it would scan nothing and
      still report clean; `--tree` is the one that looks.
- [ ] No real personal data in code, tests, docs or fixtures
- [ ] Screenshots and any demo database come from a synthetic store only:
      real merchant names and amounts are personal data
- [ ] `__version__` bumped, CHANGELOG entry dated, fresh `## Unreleased`
      left above it
