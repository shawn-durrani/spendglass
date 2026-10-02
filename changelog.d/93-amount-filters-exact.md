- Amount filters match to the cent (#93). The transactions route and
  the agents' `search_transactions` tool turned dollars into cents
  through floating point and dropped the fraction, so $0.29 became 28
  cents and $19.99 became 1998. A filter at a value like that could miss
  a charge at the boundary or take in one a cent past it. A filter value
  that isn't a number is now refused.
