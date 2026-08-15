# Merchant identity, themes, and what counts as spend

## Working out who a merchant is

Bank descriptors are plumbing: `SQ *COFFEE CO`, reference numbers, FX tails.
The identity pipeline turns them into names.

1. A deterministic normaliser cleans and keys each descriptor.
2. An optional web-search agent proposes identities.
3. Confident proposals auto-apply.
4. The rest land in a review queue.

One decision keys on the merchant, so a single click labels every matching
transaction. Provenance decorators show how each label was established: ✦
agent, ✓ human, ? pending.

## What the miners cost

They use *your* Anthropic key.

| pass | cost |
|---|---|
| Lookup agent | The expensive one: a capable model plus billed web searches per unclear merchant. |
| Sweep and classifier | Cheap. A small model via the Batch API. |
| Propagation | One small non-batch request per batch of approvals you submit, re-guessing related pending proposals. On by default; switch it off in the admin panel. |
| Everything else | Nothing. Deterministic SQL. |

Discovery is front-loaded. Once coverage is built, only new merchants trigger
lookups.

## What counts as spend

Spend views answer one question: what did I consume?

Excluded by default, though never hidden, since a toggle shows everything:
matched internal transfers, loan principal, and committed family-style
transfers.

Loan *interest* counts as spend, because that money is gone forever.

## Themes

A theme is one number for something that spans categories. A renovation is
trades plus hardware plus architects. Pets are supplies plus vet plus
insurance.

New stores start with no themes. Create your own in the admin panel, blank or
from a template: Renovation, Pets, Travel, Kids, AI, Health & Fitness.

Themes never change a transaction. They are a read-time lens.
