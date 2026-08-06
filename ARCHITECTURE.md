# Architecture & design principles

Distilled from the project's decision log: the rules the code actually
follows, with the reason each one exists. If a change violates one of
these, that's a conversation, not an accident.

## The shape

```
client.py   — the only code that talks to the Redbark API or holds its key
store.py    — SQLite (WAL) under data/; idempotent upserts; system of record
sync.py     — scheduled pull: client → store, consent status tracked loudly
────────────────────────────────────────────────────────────────────────────
enrich / transfers / themes / subscriptions / trends — derivation, in SQL
ui.py       — loopback web UI + spending page (reads store; writes labels)
mcp_server.py — read-only tools over the store, never the network
```

One privilege split defines the repo: **the process holding the bank key
is not the process serving requests.** Sync runs as a short-lived
subprocess; the long-running UI and MCP servers never see the key.
Subprocess boundaries enforce what code review can only request.

## Money & data integrity

- **Money is a string and an integer, never a float.** Store the raw
  decimal exactly as the source sent it *and* integer cents; aggregate in
  SQL over cents. "A float did money math" is unrepresentable, not merely
  discouraged.
- **Derived data must be re-derivable.** Everything below the raw rows can
  be deleted and rebuilt; derived tables rebuild wholesale, never patch in
  place. Improving a normaliser is therefore safe: it re-keys the store,
  and a migration carries user decisions across.
- **Idempotent upserts on source ids**, with a deliberate overlap window
  on re-fetch, so a pending transaction settles to posted under the same
  id instead of duplicating.
- **Refuse a newer schema; never mangle it.** A database written by newer
  code is a stop, not a migration attempt.
- **An honest `None` beats a confident wrong answer.** No dumping-ground
  categories; what resists classification stays unclassified.

## Boundaries & security

- **Read-only structurally, not by promise.** The MCP server opens the
  database `mode=ro` — a write raises at the storage layer regardless of
  who asks. The tool-name list is pinned by a test; the tool surface *is*
  the security surface.
- **Constrain the tool rather than trusting the caller.** Agents get the
  narrowest tool that does the job; anything destructive is the human's
  to run.
- **Local-only, on purpose.** Loopback binds plus a Host-header allowlist;
  the chart library is vendored so pages load nothing remote at runtime.
- **Validate-then-persist for provider credentials.** Probe the provider
  before writing; write to a 0600 file; apply in-process; never log, echo,
  or return the value. Config stores env-var *names* — values live only in
  `.env`.
- **Persist sessions as digests, not tokens.** Restarts don't log anyone
  out, the file on disk never holds a usable credential, and revocation
  still works.
- **Declare the territory you deliberately won't enter.** No derivation
  over holdings or trades: "opportunities to improve your portfolio" is
  investment advice, and a stated scope boundary is a design decision,
  not an omission.

## Freshness & honesty

- **Freshness is first-class state.** Every MCP response carries `as_of`,
  `stale`, and warnings, because the failure mode — sync stops, agents
  keep answering confidently from old data — is silent by default. Design
  it out rather than documenting around it.
- **When upstream won't tell you, estimate loudly.** The API has no
  consent-expiry field, so the worst-case deadline is computed, labeled an
  estimate, and warned about from 90 days out.
- **Keyless degradation as a house rule.** With no model key, every
  deterministic pass still runs, and the whole test suite passes with no
  credentials present. Optional intelligence never becomes a hard
  dependency.
- **Wear provenance on the sleeve.** Machine-proposed, human-confirmed,
  and awaiting-review are visibly distinct; inference may improve pending
  proposals after a human decision, but never auto-approves.

## Identity & classification

- **Normalise deterministically before spending a model call.** Processor
  prefixes, statement prefixes, embedded reference numbers, phone-shaped
  tokens, and date/FX tails are stripped first. Per-transaction reference
  numbers are poison: they mint a phantom entity per row.
- **Classify the entity, not the event.** Thousands of transactions
  collapse to hundreds of merchant keys; label each once. Batch tier for
  anything latency-insensitive; structured outputs with the valid enum
  injected; batch-resume state persisted so an interruption doesn't
  re-bill.
- **Prefer name equality to an entity table at small scale.** Several keys
  resolving to one display name; the fix for a miss is a single inline
  edit, not a config file.
- **Prove the pair, or leave it alone.** Only demonstrable two-leg
  transfers are linked; equal amounts alone never pair — a refund must not
  marry a café bill.
- **Read-time lenses never mutate the record.** Themes evaluate in SQL at
  query time; one question's answer can't destroy another's.
- **Baseline against the subject's own trailing history, never a target.**
  Budgets change behaviour; self-history tells the truth.

## UX rules

- **One editing rule everywhere.** Leaving a changed field commits it;
  Enter commits; Esc is the only discard; untouched fields never fire.
  Saves are in-place — no page refresh, no scroll jump.
- **The friction budget is the design constraint.** Above a confidence
  threshold, apply automatically (visibly, reversibly); below it, queue.
  Never make the user confirm the obvious.
- **Headline the insight, not the axis.** Current period in accent,
  comparisons in grey; every drill-down terminates in the actual records;
  every chart carries a plain-English "what am I looking at?" explainer.
- **Make taste checkable.** WCAG contrast is computed from the live theme
  tokens and fails below AA — encoding the actual bug class (a theme that
  flips surfaces while inheriting meaning-carrying colours).

## Operations

- **Scheduling belongs in the app, not the OS.** A launchd/systemd unit
  fixes one machine and silently doesn't exist for the next clone. The
  long-running process schedules its own syncs and backups; scheduled and
  manual runs share one lock and one status slot so they can't disagree.
- **Backups are the app's job too.** Consistent online snapshots with
  rotation and change-detection; a stalled backup is visible in the UI,
  never silent.
- **One implementation of a check for both the local hook and CI**, so
  the two can never drift. The secret scanner also states exactly what a
  green result does and doesn't mean.
