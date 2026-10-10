---
name: doc-design
description: How to write a document (report, proposal, brief, plan, minutes, letter) that reads well and looks professional. Use for any Word or Google Doc request.
metadata:
  renderer: doc
---

# Document design

You write a DocSpec. The renderer handles cover, fonts, colours and spacing.

## Structure
- `summary` first: 3 to 5 lines with the conclusion, the decision needed and the key numbers. Busy readers
  stop here.
- Sections in reading order, each heading a clear label ("Budget", "Risks and mitigations", "Next steps").
  Use level 2 headings only inside long sections.
- End with "Next steps" (owner, action, date) when the document leads to action.
- `cover: true` for reports, proposals and plans; `false` for letters, notes and minutes.

## Blocks
- `paragraph`: 2 to 5 sentences, one idea each.
- `bullets` for unordered points, `numbered` for steps or ranked items (3 to 8 items, short).
- `table` whenever there are 3+ items with the same attributes (owner, date, cost). Short cells.
- `callout` for the one thing the reader must not miss (a deadline, a risk, a decision).
- `quote` for a direct line from a person or source, with the source in the text.
- `kpis` for 2 to 4 headline numbers (`stats`: value short like "₹71L", "+22%", label a few words). Put it
  at the top of the first section of any report with numbers; never more than one per section.
- `chart` whenever numbers change over time or differ across groups (fill `chart`; `text` is the one-line
  takeaway printed under it, stating what the chart shows, not "Chart of X"). Column for a few categories
  or periods, line for 6+ periods, bar for long category names or rankings, doughnut only for a share of
  one whole with 2 to 5 parts. One series unless a comparison is the point; at most 3.
- Mix block kinds: a page of only paragraphs is hard to scan. A report with numbers needs at least one
  `kpis` or `chart`, and a table for anything with 3+ comparable rows.

## Writing
- Plain, specific language in the user's register. Active voice. No filler openings.
- Ground facts in the user's request and files; mark unknowns as "[to confirm]". Never invent data.
- No em dashes or en dashes.

## Look
The theme draws the cover (a gradient band on gradient styles), fonts, colours, tables and charts. Do not
describe design in the text. Long documents read better with a level 1 heading every page or so and a
callout or chart breaking up long runs of text.

## Rendering
The studio validates your spec and runs `scripts/render.py` (the doc renderer) on the server.
You never write layout code; the anti-slop and brand-personalisation skills apply too.
