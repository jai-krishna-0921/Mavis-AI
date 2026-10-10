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
- Mix block kinds: a page of only paragraphs is hard to scan.

## Writing
- Plain, specific language in the user's register. Active voice. No filler openings.
- Ground facts in the user's request and files; mark unknowns as "[to confirm]". Never invent data.
- No em dashes or en dashes.

## Rendering
The studio validates your spec and runs `scripts/render.py` (the doc renderer) on the server.
You never write layout code; the anti-slop and brand-personalisation skills apply too.
