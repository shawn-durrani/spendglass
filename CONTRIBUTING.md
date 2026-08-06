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

## What gets a warm welcome

Bug reports with reproductions, portability fixes, accessibility
improvements, and anything that makes the plain-English explainers
plainer. Big new features are worth an issue discussion before code;
the scope boundaries in ARCHITECTURE.md (notably: no investment-advice
derivation) are deliberate.
