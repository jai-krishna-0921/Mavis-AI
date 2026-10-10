---
name: anti-slop
description: The rules that keep an artifact from looking or reading like generic AI output. Always applies, to every deck, document and sheet.
---

# No AI slop

A reader should not be able to tell a model made this. Check every line against these rules.

## Words
- Banned: elevate, seamless, unleash, unlock, empower, leverage (as a verb), synergy, game-changer,
  next-gen, cutting-edge, revolutionize, delve, tapestry, landscape (for a market), robust, holistic,
  "in today's fast-paced world", "it's important to note", "in conclusion", "let's dive in".
- No emojis, no exclamation marks in titles, no ALL CAPS shouting.
- No meta labels as titles: "Section 01", "Introduction", "Overview", "Agenda" (unless the user asked for
  an agenda), "Key Takeaways" on every slide. Titles say the point.
- No filler subtitles ("A comprehensive overview of..."). If a subtitle adds nothing, leave it empty.
- No placeholder people or companies: no John Doe, Jane Smith, Acme, Company X, Lorem ipsum. Use the
  real names you were given, or a clearly marked gap like "[owner]".
- Specific beats vague: "Cut ticket backlog from 1,200 to 300 by March" not "Improve efficiency".
- No em dashes or en dashes.

## Structure
- Variety: never the same slide layout twice in a row; documents mix paragraphs, lists, tables and a
  callout.
- Restraint: 2 to 4 cards or stats, not 8. One accent idea per slide.
- Titles fit in two lines. If a title runs longer, it is two ideas: split the slide.
- Symmetry is not a virtue: a stat slide with 3 real numbers beats 4 where one is padding.
- No decorative content: every chart, table and number carries information the reader needs.

## Self-check before you answer
1. Read only the titles in order: do they tell the whole story?
2. Search your text for the banned words and replace them.
3. Is any number, name or date not from the user or their files? Remove it or mark it "[to confirm]".
