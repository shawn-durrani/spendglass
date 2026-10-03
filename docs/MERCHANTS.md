# Merchant identity, themes, and what counts as spend

## Working out who a merchant is

A bank's description of a purchase is mostly plumbing, such as
`SQ *COFFEE CO`, reference numbers and foreign-currency tails.
Spendglass turns those descriptions into the names of real businesses.

1. A fixed set of cleaning rules strips the plumbing and gives each
   merchant a key. The key is the bank's merchant name, or the
   description when there isn't one, cleaned and in lower case.
2. Two optional passes propose a name. The identity sweep names a
   merchant from its description alone. The lookup agent searches the
   web for the merchants that are still unclear.
3. A proposal whose confidence reaches the auto-approve threshold, 0.8
   by default, is applied straight away.
4. The rest wait for you in the review queue.

Both passes skip a merchant that already has a proposal, so whichever
runs first names it. A merchant counts as unclear to the lookup agent
when its description is noisy, or when the category classifier was
unsure about it or hasn't seen it yet.

Each decision belongs to the merchant key, so one click labels every
matching transaction. A mark beside each merchant name shows where the
name came from:

- ✦ means a pass applied it on its own.
- ✓ means you approved or corrected it.
- ? means it's waiting for review, and clicking it opens the queue.
- ⇄ means the row is one leg of a transfer between your own accounts.

## What the miners cost

The passes that name and sort merchants are called the miners, and
they use your Anthropic key.

| pass | cost |
|---|---|
| Lookup agent | The expensive one. A capable model, plus billed web searches for each unclear merchant, up to the limit you set in the admin panel. |
| Identity sweep and category classifier | Cheap. A small model through the Batch API, which costs half as much. |
| Propagation | One small request each time you submit review decisions that approve something. It re-guesses the pending proposals and never approves one itself. It's on by default, and "learn from my decisions" in the admin panel switches it off. |
| Everything else | Nothing. It's fixed code running on your computer. |

No paid pass runs by itself. Scheduled sync only runs the free
enrichment. The lookup agent, the sweep and the classifier run when
you press their buttons in the admin panel, or from the command line.
Each merchant is looked up once, so the cost comes up front. Once most merchants have
names, a run only pays for new ones. A change to the cleaning rules
can give a merchant a new key, and that merchant then needs naming
again.

## What counts as spend

The spend views answer one question: what did I consume?

They leave out both legs of a transfer between your own accounts, and
every row whose category is a transfer in, a transfer out or a loan
payment. A transfer that isn't matched stays out until you give it
another category. A regular contribution to a shared family account
stays out, and it shows in your committed floor. A payment to a tradie
by bank transfer counts once you file it under the work it paid for.

Loan interest and fees count as spend, because that money is gone. A
loan payment counts when its merchant is labelled Mortgage Interest,
Card Interest or Loan Fees. Loan principal stays out.

Nothing is hidden. On the spending page, "include transfers & loan
payments" brings every left-out row back into the charts and the
drill-down. The toggle only changes the spending page, and the
trends, the theme totals and the subscriptions ignore it.

## Themes

A theme is one number for something that spans categories. A
renovation is trades plus hardware plus architects. Pets are supplies
plus vet plus insurance.

A new store starts with no themes. You create your own in the admin
panel, blank or from a template, and the templates are Renovation,
Pets, Travel, Kids, AI, Health & Fitness, and Sport & Fitness.

Sport & Fitness is kept separate from Health & Fitness so a chemist
run or a doctor's visit never lands in a sport total. It starts with
the gym and sport/recreation subcategories plus a handful of merchant
rules for bike shops and bike parks, which usually get filed as
Shopping or Transportation and would otherwise never reach a
sport-flavoured subcategory at all. The two themes share the gym and
sport subcategories, so a gym debit shows up in both totals if you
keep both. Themes are overlapping lenses, not a partition, and
nothing is counted twice inside any one theme. It has no rule for a personal
trainer, because the app never guesses which business that is. Add
one yourself from the theme editor once a lookup names the merchant,
or straight away if you already know its name on your statement.

Themes never change a transaction. They group transactions when you
look, and the transactions stay as they are.
