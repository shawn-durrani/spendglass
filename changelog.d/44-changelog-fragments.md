- Two open PRs no longer conflict on the changelog (#44). Each change
  now ships its entry as one file under `changelog.d/`, and a release
  folds them into `CHANGELOG.md` newest first, above the entries already
  sitting under Unreleased. A test fails any PR that edits Unreleased
  directly and names the new home.
