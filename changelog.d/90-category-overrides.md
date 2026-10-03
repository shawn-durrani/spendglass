- Four categories were lying (#90). Loan and credit-card interest showed up
  as bank fees, bottle shops (Liquorland, BWS, Dan Murphy's) showed up as
  eating out, Anthropic/AI API charges and Spriggy kids' top-ups both
  showed up as shopping. A new deterministic, user-editable rules table
  fixes all four at read time: the raw bank category is never touched,
  and deleting a rule reverts it instantly. Interest now has its own
  category, and it counts as fixed spend, not variable, in the fixed-vs-variable
  and merchant-focus views.
