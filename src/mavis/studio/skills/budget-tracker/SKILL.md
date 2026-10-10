---
name: budget-tracker
description: Budgets, expense trackers, cost breakdowns and simple financial tables. Load for any sheet or table about money.
---

# Budget tracker

- Columns: item or category, amount (currency kind with the user's currency), and when useful planned vs
  actual vs variance, date, owner, notes. Totals via `total: true`, never typed.
- Variance is a formula's job: include planned and actual; leave variance to the reader or describe it,
  do not hard-code computed numbers that could go stale.
- Group by category; one tab per period only when the user tracks months separately.
- A `column` chart of spend by category or a `line` chart over time when there are 4+ data points.
- Indian users: lakh and crore in labels when they use them; amounts stay plain numbers.
