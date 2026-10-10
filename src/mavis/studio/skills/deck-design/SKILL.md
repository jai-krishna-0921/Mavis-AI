---
name: deck-design
description: How to plan and write a presentation that looks designed, not dumped. Use for any slide deck, pitch, kickoff, review or training deck.
metadata:
  renderer: deck
---

# Deck design

You write a DeckSpec. The renderer draws it in the user's theme, so your job is the story, the layout
choice per slide and tight copy. Never describe colours or fonts in the slides.

## Story first
- Open with a `title` slide, close with a `closing` slide (next steps, owner, date, or a crisp ask).
- Between them, one idea per slide. A slide title states the point ("Pilot cut handling time by 30%"),
  not the topic ("Pilot results").
- 6 to 12 slides is right for most requests. Use `section` slides only when there are 3 or more parts.
- Ground every number, name and date in what the user or their files said. If a figure is unknown, leave
  it out or write a clearly marked placeholder like "[budget to confirm]". Never invent data.

## Pick the layout from the content
| Content | Layout |
|---|---|
| 2 to 4 key figures | `stats` (value short: "18 lakh", "3x", "92%") |
| Before/after, option A vs B, problem vs solution | `two_column` |
| Phases, plan, roadmap, process | `timeline` (3 to 6 steps, `when` short) |
| Numbers over time or across groups | `chart` (fill `chart.categories`, `chart.series`) with a `takeaway` |
| Small comparison grid | `table` (max 6 rows x 5 columns, short cells) |
| A customer line, principle or vision | `quote` |
| A short list of points | `bullets` |
- Never use the same layout on two slides in a row. A deck of only `bullets` is a failure.
- Every deck of 6 or more slides should use at least 4 different layouts, and at least one of
  `stats`, `chart`, `timeline` or `two_column`.

## Copy rules
- Bullets: 2 to 5 per slide, each under 12 words, no full stops, start with a strong word.
- Subtitles: one line. Takeaways: one sentence that says what the chart means.
- No walls of text: anything longer belongs in `notes` (speaker notes, 2 to 5 sentences per slide).
- Write in the user's language and register. No em dashes or en dashes; use commas, colons or "to".

## Before you finish
- Check the arc: why, what, how, proof, next. Cut any slide that does not move it.
- Check numbers add up (stats and charts agree with each other and with the sources).

## Rendering
The studio validates your spec and runs `scripts/render.py` (the deck renderer) on the server.
You never write layout code; the anti-slop and brand-personalisation skills apply too.
