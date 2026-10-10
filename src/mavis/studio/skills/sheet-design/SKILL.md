---
name: sheet-design
description: How to lay out a spreadsheet (tracker, budget, plan, list, comparison) so it is clean, typed and ready to use. Use for any Excel or Google Sheet request.
metadata:
  renderer: sheet
---

# Spreadsheet design

You write a SheetSpec. The renderer styles headers, bands rows, freezes the header, fits widths, applies
number formats and adds totals and charts.

## Columns
- One header row; the first column identifies the row (name, item, date).
- Give every column the right `kind`: `number`, `currency` (set `currency`, e.g. "₹"), `percent`
  (values as fractions: 0.25 for 25%), `date` (ISO "2026-10-12"), or `text`.
- Set `total: true` on amounts that should be summed. Never type a total row yourself.
- 3 to 10 columns is typical; status and owner columns make trackers useful.

## Rows
- Real data from the user or their files. If they asked for a template, add 2 to 4 clearly sample rows
  and say so in a "Notes" column or the tab name ("Sample").
- Numbers as numbers, not strings with units. Keep text cells short.

## Tabs and charts
- One tab per table. A summary tab is worth it only when there are several detail tabs.
- Add a `chart` when the user wants to see a trend or comparison: categories from the first column,
  one to three numeric series, `column` for comparisons, `line` for time, `pie` only for parts of a whole
  with 6 or fewer slices.

## Rendering
The studio validates your spec and runs `scripts/render.py` (the sheet renderer) on the server.
You never write layout code; the anti-slop and brand-personalisation skills apply too.
