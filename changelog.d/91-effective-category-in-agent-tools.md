- The `search_transactions` and `spending_summary` agent tools were still
  filtering and grouping by the bank's raw category, the very tools that
  showed #90's lying figures. They now filter and group by the corrected
  category and subcategory, with the bank's own code still one field away
  (`raw_category` on a transaction row, or `group_by="raw_category"`).
  Every trend, theme and subscription tool now also ensures the override
  rules and merchant tables exist on first use, so a fresh store answers
  them correctly from a standalone MCP server, not only from one the admin
  UI has already touched.
