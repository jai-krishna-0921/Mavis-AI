"""Action claims in a chat reply are bound to what the turn did (track 1 T1.4, hotfix4 H4).

The model may end a turn saying it will act ("I'll block 2 to 3 pm, just tap Approve") without having
called any tool: nothing was created and no card exists. This module decides, from the turn's own record
(ReactResult: which tools ran, which cards were queued) and the vocabulary of the tools that were offered,
when such a reply needs a second look, and builds the one re-prompt that gives the model its tools again.
It never rewrites what the model meant: the model either calls the tool or answers again. The only code
edit is the approval-UI rule: a reply cannot point at a card (tap, button, approve) when no card exists.

Signals (measured, not phrase lists):
- UI claim: the reply uses the approval card's own vocabulary while the turn queued no card and none is
  waiting on the user.
- Missed action: no action tool ran this turn, and the reply shares content terms with an offered action
  tool's own vocabulary (its name and description, minus terms most offered tools share): at least two
  terms, or one when the user's message shares a term with that tool too (they asked for what it does).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from mavis.domain.policy import RiskClass
from mavis.domain.terms import terms

# The approval card's own vocabulary (its buttons and what they do): only a card makes these true.
UI_TERMS = frozenset(terms("tap tapping tapped button buttons approve approved approval approving"))
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]*\s*|\n")
_SHARED_BY = 3  # a term in this many offered action tools' vocabularies says nothing about any one of them
UI_FALLBACK = "I haven't set anything up for that yet. Want me to?"

REPROMPT = (
    "[System note, not from the user] Your reply talks about doing something, but this turn "
    "called no tool for it, so nothing was done{ui}. If you meant to act, call the tool now. If you were "
    "only offering, asking or answering, reply again with your message, without saying anything was done "
    "or is waiting for approval. Do not do anything the user did not ask for."
)
NO_CARD = ", and no approval card exists: never ask them to tap or approve anything"


@dataclass
class ClaimCheck:
    ui_claim: bool = False
    tools: list[str] = field(default_factory=list)  # offered action tools the reply talks about (logs)

    @property
    def reprompt(self) -> bool:
        return self.ui_claim or bool(self.tools)

    def note(self) -> HumanMessage:
        # no tool names: a near miss in the vocabulary must not suggest an action to the model
        return HumanMessage(REPROMPT.format(ui=NO_CARD if self.ui_claim else ""))


def _vocab(tool: BaseTool) -> set[str]:
    return {t for t in terms(f"{tool.name.replace('_', ' ')} {tool.description}") if not t.isdigit()}


def _action_vocab(offered: Sequence[BaseTool], risk_of, exclude: frozenset[str]
                  ) -> dict[str, tuple[set[str], set[str]]]:
    """Each offered action tool's (own terms, distinctive terms). Own: its name and description minus
    terms most action tools share. Distinctive: also minus words a read tool uses ("email", "calendar":
    talking about those may be answering, not acting)."""
    vocab: dict[str, set[str]] = {}
    reads: set[str] = set()
    for tool in offered:
        risk = risk_of(tool.name)
        if risk is RiskClass.READ:
            reads |= _vocab(tool)
        elif risk is not None and tool.name not in exclude:
            vocab[tool.name] = _vocab(tool)
    counts = Counter(t for words in vocab.values() for t in words)
    out = {}
    for name, words in vocab.items():
        own = {t for t in words if counts[t] < _SHARED_BY}
        out[name] = (own, own - reads)
    return out


def check(reply: str, user_text: str, offered: Sequence[BaseTool], *, tools_called: Sequence[str],
          card_shown: bool, waiting_tools: Sequence[str] = (), risk_of) -> ClaimCheck:
    """What, if anything, in `reply` is not backed by the turn. `risk_of(name)` gives a tool's declared
    risk (None for a tool outside the registry); `card_shown`: this turn queued or re-showed a card;
    `waiting_tools`: the tools of earlier cards still waiting on the user (talk about those is backed).

    A missed action: no action tool ran, and for some offered action tool the reply shares two of its
    distinctive terms, or one that the user's message shares too; or, in a turn that called no tool at
    all, the reply and the user's message both share one of its own terms (they asked for what it does,
    the reply talks about it, and nothing was even looked up)."""
    said = terms(reply)
    out = ClaimCheck(ui_claim=bool(said & UI_TERMS) and not card_shown and not waiting_tools)
    if card_shown or any(risk_of(name) not in (None, RiskClass.READ) for name in tools_called):
        return out  # the turn did act: its own receipt and card speak for it
    asked = terms(user_text)
    for name, (own, distinct) in _action_vocab(offered, risk_of, frozenset(waiting_tools)).items():
        shared = said & distinct
        if (len(shared) >= 2 or (shared and asked & distinct)
                or (not tools_called and said & own and asked & own)):
            out.tools.append(name)
    return out


def strip_ui_claims(reply: str) -> str:
    """Drop the sentences that point at an approval card (only used when no card exists)."""
    kept = [s for s in _SENTENCE.findall(reply) if not terms(s) & UI_TERMS]
    text = re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()
    return text or UI_FALLBACK
