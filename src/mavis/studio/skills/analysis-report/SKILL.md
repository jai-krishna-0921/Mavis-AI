---
name: analysis-report
description: Deep analysis reports (market, competitor, financial, performance, research, due diligence, any "analyse X and report"). Load when the reader needs findings backed by evidence and a recommendation, not just information.
---

# Analysis report

An analysis report answers a question with evidence and ends in a decision. Work like an analyst: state the
answer first, then prove it.

## Before writing
- Find the question behind the request ("analyse our Q3" usually means "are we on track, and what should we
  change?"). Put it in the subtitle.
- Use only the material given (the brief, earlier steps' research, source files). Every number needs a
  source in it; when one is missing, say "[to confirm]". Never invent figures, sources or quotes.
- Do the arithmetic yourself: growth rates, shares, differences, per-unit figures, rankings. Analysis is
  what the numbers mean together, not a list of them.

## Structure (DocSpec, `cover: true`)
1. `summary`: the answer in 3 to 5 lines: the conclusion, the 2 or 3 numbers that prove it, the
   recommendation and the decision needed.
2. "Headline metrics": one `kpis` block (3 or 4 numbers), then a sentence on what they mean together.
3. Findings: one section per finding, the heading states the finding ("Referrals drive 41% of new
   revenue", not "Channels"). Each has evidence (a `chart` or `table`), a paragraph on why it matters, and
   its implication. 3 to 5 findings; order by importance.
4. "Risks and unknowns": what could make the conclusion wrong, and what data would settle it.
5. "Recommendations": a `numbered` list, each an action with an owner and a date where known; the first
   is the most important. Add a `callout` with the one decision and its deadline.
6. "Method and sources": how the analysis was done and the sources used (titles, dates, links if given).
   Keep it short.

## Evidence rules
- One chart per finding at most, chosen for the comparison (see doc-design). Its `text` says the takeaway.
- Tables for comparisons of 3+ items on the same attributes (competitors, options, segments). Keep cells
  short; put the comparison that decides it in the first data column.
- Compare against something: last period, a target, a competitor, a benchmark. A number alone says little.
- Separate fact from judgement: "Revenue grew 22% (Q3 report)" vs "This suggests referrals scale".

## Style
Always a gradient style, whatever the brand-personalisation defaults say: `executive` for leadership and
investors, `ocean` for research, data and personal or financial analysis, `corporate` for a client. Never
`minimal` or `bold` for an analysis report (unless the user chose a style).
Decks from the same analysis follow deck-design: one finding per slide, `stats` for the headline
metrics, a `chart` per finding, `closing` with the recommendation.
