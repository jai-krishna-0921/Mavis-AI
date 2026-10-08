"""Action claims in a chat reply are bound to what the turn did (track 1 T1.4, hotfix4 H4).

The model may end a turn saying it will act ("I'll block 2 to 3 pm, just tap Approve") without having
called any tool: nothing was created and no card exists. This module decides, from the turn's own record
(ReactResult: which tools ran, which cards were queued, which cards are waiting) and the vocabulary of the
tools that were offered, when such a reply needs a second look, and builds the one re-prompt that gives
the model its tools again. Code never edits the reply: the model either calls the tool or answers again.

Signals (measured, not phrase lists):
- UI claim: the reply tells the user to work a control (tap, a button) while the turn queued no card
  and none is waiting. Ordinary words such as "approved" ("HR approved your leave") are not card UI and
  never count.
- Missed action: the turn called no tool, and the user's message and the reply both share a term of an
  offered action tool's own vocabulary (its name and description, minus terms most action tools share
  and the approval mechanics every outward description repeats).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool

from mavis.domain.policy import RiskClass
from mavis.domain.terms import terms

# Working an on-screen control: in a chat, only our approval card has controls.
UI_TERMS = frozenset(terms("tap tapping tapped button buttons"))
_SHARED_BY = 3  # a term in this many offered action tools' vocabularies says nothing about any one of them
# How an outward tool's description says it waits for the user ("asked to approve first"): mechanics,
# not what the tool does, so never evidence that a reply is about that tool.
_MECHANICS = frozenset(terms("approve approved approves approval asked first user"))


def ui_claim(reply: str) -> bool:
    """The reply tells the user to work a control of the approval card."""
    return bool(terms(reply) & UI_TERMS)

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


def _action_vocab(offered: Sequence[BaseTool], risk_of, exclude: frozenset[str]) -> dict[str, set[str]]:
    """Each offered action tool's own terms: its name and description minus terms most action tools
    share and the approval mechanics outward descriptions repeat."""
    vocab = {tool.name: _vocab(tool) for tool in offered
             if risk_of(tool.name) not in (None, RiskClass.READ) and tool.name not in exclude}
    counts = Counter(t for words in vocab.values() for t in words)
    return {name: {t for t in words if counts[t] < _SHARED_BY} - _MECHANICS for name, words in vocab.items()}


def check(reply: str, user_text: str, offered: Sequence[BaseTool], *, tools_called: Sequence[str],
          card_shown: bool, waiting_tools: Sequence[str] = (), risk_of) -> ClaimCheck:
    """What, if anything, in `reply` is not backed by the turn. `risk_of(name)` gives a tool's declared
    risk (None for a tool outside the registry); `card_shown`: this turn queued or re-showed a card;
    `waiting_tools`: the tools of earlier cards still waiting on the user (talk about those is backed).

    A missed action: the turn called no tool at all, and both the user's message and the reply share a
    term of an offered action tool's own vocabulary (they asked for what it does, the reply talks about
    it, and nothing ran). Tuned on the live eval (fix round 1): a reply that only shares words with a tool
    the user never asked about, or a turn that looked something up first, is not re-prompted."""
    said = terms(reply)
    out = ClaimCheck(ui_claim=ui_claim(reply) and not card_shown and not waiting_tools)
    if card_shown or any(risk_of(name) not in (None, RiskClass.READ) for name in tools_called):
        return out  # the turn did act: its own receipt and card speak for it
    if tools_called:
        return out  # it looked something up and answered from it: only a card claim counts
    asked = terms(user_text)
    for name, own in _action_vocab(offered, risk_of, frozenset(waiting_tools)).items():
        if said & own and asked & own:
            out.tools.append(name)
    return out
