"""MemoryService: the single entry point for recall (hot path) and learn (background)."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog

from mavis.config import get_settings
from mavis.domain import timeutil
from mavis.domain.events import Provenance, Trust
from mavis.domain.memory import Extraction, RecallContext
from mavis.memory import recall as recall_mod
from mavis.memory.dates import apply_relative_day
from mavis.memory.embeddings import Embedder, get_embedder
from mavis.memory.extractor import extract
from mavis.memory.graph import GraphStore, is_third_party, make_graph
from mavis.memory.names import is_user
from mavis.memory.recall import LoopsReader
from mavis.memory.resolver import resolve
from mavis.memory.spotter import SpotterCache
from mavis.memory.vector import QdrantVectorStore, VectorStore
from mavis.store.repo import loops as loops_repo
from mavis.store.repo import profile as profile_repo
from mavis.store.repo import users

log = structlog.get_logger()

ExtractionHook = Callable[[int, Extraction, Provenance], Awaitable[None]]
MIN_EPISODE_WORDS = 4
RECALL_TOTAL_TIMEOUT_S = 1.5
INIT_RETRY_COOLDOWN_S = 30.0
USER_PREFIX = "User: "
LEGACY_ASSISTANT_PREFIX = "Mavis: "  # LEARN text written before T3: the reply as a "Mavis: " line
CONTEXT_OPEN, CONTEXT_CLOSE = "<assistant_context>", "</assistant_context>"
CONTEXT_NOTE = "Your previous reply: context only. Do not extract items from it."


def _split_learn_text(text: str) -> tuple[str, str]:
    """(the user's words, the assistant context) of a LEARN text. The user's words are every "User: "
    segment; the context is the fenced previous reply and any legacy "Mavis: " lines. Text without any
    marker is all the user's."""
    if USER_PREFIX not in text and CONTEXT_OPEN not in text and not text.startswith(LEGACY_ASSISTANT_PREFIX):
        return text.strip(), ""
    segments: list[list[str]] = []
    context: list[str] = []
    in_context = in_user = False
    for line in text.split("\n"):
        if in_context:
            in_context = line.strip() != CONTEXT_CLOSE
            if in_context:
                context.append(line)
            continue
        if line.startswith(CONTEXT_OPEN):
            in_context, in_user = True, False
        elif line.startswith(USER_PREFIX):
            segments.append([line[len(USER_PREFIX):]])
            in_user = True
        elif line.startswith(LEGACY_ASSISTANT_PREFIX):
            in_user = False
            context.append(line[len(LEGACY_ASSISTANT_PREFIX):])
        elif in_user:
            segments[-1].append(line)
        else:
            context.append(line)
    user = "\n".join("\n".join(seg).strip() for seg in segments).strip()
    return user, "\n".join(context).strip()


def user_words_of(text: str) -> str:
    """Every "User: " segment of a LEARN text, joined: the only source of loops and events (T3). The fenced
    assistant context and legacy "Mavis: " lines are skipped. Text without any marker is all the user's."""
    return _split_learn_text(text)[0]


def assistant_context_of(text: str) -> str:
    """The assistant's words in a LEARN text (the fenced previous reply, legacy "Mavis: " lines): what the
    user may be echoing. Empty when the text is all the user's."""
    return _split_learn_text(text)[1]


_WORD = re.compile(r"[^\W_]+")  # any letters or digits: names are not only ASCII
_GROUNDING_STEM = 4  # "claims" grounds "claim", "renewal" grounds "renew"


def _grounded(title: str, entities: list[str], said: str, context: str | None) -> bool:
    """The item names something the user actually said: a person it involves, or an identifying word of
    its title (exact, or sharing a stem of at least _GROUNDING_STEM letters). Function words, times and
    dates are not identifying (loops_repo.title_tokens).

    The leading word needs care. An item title often leads with its action ("Call Ravi about the lease"),
    and when the assistant's reply proposed that action the user echoing it ("ok, I'll call him") does
    not say which item they mean. So the first word grounds the item only when it is the user's own: it
    does not appear in the assistant context. A noun-first title ("Dentist appointment Friday") is then
    grounded by "dentist" unless the reply said it first. With no context given (None) the first word
    cannot be shown to be the user's own and does not count; a one-word title is identified by its word."""
    if any(e.strip() and _named(e, said) for e in entities):
        return True
    tokens = loops_repo.title_tokens(title)
    if not tokens:
        return False
    rest = tokens[1:]
    first_is_own = context is not None and not _words_match(tokens[:1], context)
    if not rest or first_is_own:
        return _words_match(tokens, said)
    return _words_match(rest, said)


def _words_match(tokens: list[str], said: str) -> bool:
    words = set(_WORD.findall(said.casefold()))
    return any(t == w or (min(len(t), len(w)) >= _GROUNDING_STEM and (t.startswith(w) or w.startswith(t)))
               for t in tokens for w in words)


def _named(name: str, said: str) -> bool:
    """The user's words name this entity: the user themself, the full name, or one of its name words
    ("Ravi" names "Ravi Menon"). Whole words only: "Ravi" is not named by "ravine" or "Ravishankar"."""
    if is_user(name):
        return True
    words = _WORD.findall(said.casefold())
    parts = _WORD.findall(name.casefold())
    if not parts:
        return False
    n = len(parts)
    if any(words[i:i + n] == parts for i in range(len(words) - n + 1)):
        return True
    return any(len(p) >= 2 and p in words for p in parts)


# Words that say whose fact it is, not what it is: never evidence that the user said the content.
_PERSPECTIVE = frozenset(
    "user users user's i me my mine myself we us our you your yours he him his she her hers they them their "
    "is are was were be been am has have had does did s".split()
)


def _supported(content: str, said: str, skip: set[str]) -> bool:
    """Strict grounding of stored content: more than half of its content words (not perspective words,
    not the words in `skip`: the subject's name and the user's own name) appear in the user's own words,
    exactly or by stem. A single shared word does not launder a sentence the user never said."""
    tokens = [t for t in loops_repo.title_tokens(content) if t not in _PERSPECTIVE and t not in skip]
    if not tokens:
        return False
    hit = sum(1 for t in tokens if _words_match([t], said))
    return hit * 2 > len(tokens)


def _name_words(*names: str) -> set[str]:
    return {w for n in names for w in _WORD.findall(n.casefold())}


def grounded_in_user(
    x: Extraction, said: str, context: str | None = None, *, strict: bool = False,
    own_names: tuple[str, ...] = (),
) -> Extraction:
    """Keep only what the user's own words support (T3, I5); `context` is the assistant's previous reply
    (see _grounded for how it decides the leading word). Anything lifted from the assistant context
    is dropped: loops and events, entities the user never named, relations whose every non-user side
    the user did not name, and profile updates whose value the user did not say.

    `strict` (the turn read third-party output): naming is not support. A stored relation (its statement
    and object) and a profile value must also be said by the user in substance (_supported), so a reply
    that carries an email's claim about "Alice" cannot be learned because the user said "thanks Alice"."""
    loops = [lp for lp in x.loops if _grounded(lp.title, lp.entities, said, context)]
    events = [ev for ev in x.events if _grounded(ev.title, ev.with_people, said, context)]
    entities = [e for e in x.entities if _named(e.name, said)]
    relations = [r for r in x.relations if _named(r.subject, said) and _named(r.object, said)]
    me = _name_words(*own_names)
    if strict:
        relations = [r for r in relations
                     if _supported(f"{r.statement} {r.object}", said, _name_words(r.subject) | me)]
        profile = [u for u in x.profile_updates if _supported(u.value, said, me)]
    else:
        profile = [u for u in x.profile_updates if _words_match(loops_repo.title_tokens(u.value), said)]
    dropped = {"loops": len(x.loops) - len(loops), "events": len(x.events) - len(events),
               "entities": len(x.entities) - len(entities), "relations": len(x.relations) - len(relations),
               "profile_updates": len(x.profile_updates) - len(profile)}
    if any(dropped.values()):
        log.info("memory.ungrounded_items_dropped", **dropped)
    return x.model_copy(update={"loops": loops, "events": events, "entities": entities,
                                "relations": relations, "profile_updates": profile})


def user_message_of(text: str) -> str:
    """The user's own words in a conversation turn: text after the last line starting 'User: '."""
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(USER_PREFIX):
            return "\n".join([lines[i][len(USER_PREFIX):], *lines[i + 1:]]).strip()
    return text


class MemoryService:
    def __init__(
        self, graph: GraphStore, vector: VectorStore, embedder: Embedder, loops: LoopsReader | None = None
    ) -> None:
        self.graph = graph
        self.vector = vector
        self.embedder = embedder
        self.loops = loops
        self.on_extraction: list[ExtractionHook] = []
        self._spotters = SpotterCache(graph)
        self._ready = False
        self._init_lock = asyncio.Lock()
        self._init_failed_at: float | None = None

    async def init(self) -> None:
        """Idempotent store initialisation; every public coroutine calls it lazily."""
        if self._ready:
            return
        async with self._init_lock:
            if not self._ready:
                await self.graph.init()
                await self.vector.init()
                self._ready = True

    async def warm(self) -> None:
        """Pay the one-off costs (store init, embedding model load) before the first reply."""
        await self.init()
        await self.embedder.embed(["warm-up"])

    def set_loops_reader(self, reader: LoopsReader | None) -> None:
        self.loops = reader

    def invalidate(self, user_id: int) -> None:
        self._spotters.invalidate(user_id)

    # --- hot path ------------------------------------------------------------------

    async def recall(self, user_id: int, text: str) -> RecallContext:
        """Never blocks a reply for long: the whole thing (incl. lazy init) is bounded."""
        try:
            return await asyncio.wait_for(self._recall(user_id, text), RECALL_TOTAL_TIMEOUT_S)
        except TimeoutError:
            if not self._ready:
                self._init_failed_at = time.monotonic()
            log.warning("memory.recall_timeout", timeout_s=RECALL_TOTAL_TIMEOUT_S)
            return RecallContext()

    async def _recall(self, user_id: int, text: str) -> RecallContext:
        if (
            not self._ready
            and self._init_failed_at is not None
            and time.monotonic() - self._init_failed_at < INIT_RETRY_COOLDOWN_S
        ):
            return RecallContext()
        try:
            try:
                await self.init()
            except Exception:
                self._init_failed_at = time.monotonic()
                raise
            user = await users.get(user_id)
            card = await profile_repo.get(user_id)
            profile, tz = card.render(), user.timezone
        except Exception:
            log.warning("memory.recall_setup_failed", exc_info=True)
            return RecallContext()
        return await recall_mod.recall(
            user_id,
            text,
            profile=profile,
            tz=tz,
            spotters=self._spotters,
            graph=self.graph,
            vector=self.vector,
            loops=self.loops,
        )

    # --- background ----------------------------------------------------------------

    async def learn(
        self, user_id: int, text: str, source_ref: str = "", trust: Trust = Trust.USER,
        conversation: bool = True, anchor_at: datetime | None = None, strict: bool = False,
    ) -> Extraction:
        """Extract and persist. LLMError from extraction propagates; the LEARN job drops it (best effort).

        `trust` and `conversation` are the origin's provenance; hooks receive them unchanged.
        `strict`: the turn saw third-party output, so every item must be named in the user's own words (the
        same grounding as when an assistant reply is included), even when no reply is.
        `anchor_at` is when the text was written (the turn, the email): relative times in it ("7 PM",
        "tomorrow") resolve against that, not against when this job happens to run.

        Idempotent on retry: graph writes are MERGEs, vector ids are uuid5 of the text.
        """
        await self.init()
        user = await users.get(user_id)
        card = await profile_repo.get(user_id)
        anchor = anchor_at or timeutil.now()
        extraction = await extract(
            text, user_name=user.name or card.name, tz=user.timezone, now=anchor, trust=trust,
            source=source_ref,
        )
        if not card.tracks_mood:
            extraction = extraction.model_copy(update={"mood": None})
        if trust is Trust.USER:  # the model sometimes misreads "by Tuesday": fix the plain cases in code
            extraction = apply_relative_day(extraction, user_message_of(text), anchor, user.timezone)
        said, context = _split_learn_text(text)
        # The assistant's own words are never a memory source (only fenced context for the extractor):
        # what is stored is the user's side of a chat turn, whatever its trust.
        own_words = said if conversation else text
        # a reply was included as context (T3), or the turn read third-party output (strict)
        if conversation and (strict or said != text.strip()):
            extraction = grounded_in_user(extraction, said, context, strict=strict,
                                          own_names=tuple(n for n in (user.name, card.name) if n))

        resolution = await resolve(extraction, await self.graph.entities(user_id), self.embedder)
        trusted = trust is Trust.USER
        if trusted:
            # Third-party text must not seed graph entities: an email sender listed as an alias would
            # otherwise count as a "known sender" in the trusted triage signals.
            for entity in resolution.entities:
                await self.graph.upsert_entity(user_id, entity)
        facts = [r.statement for r in resolution.relations]
        if trusted:
            for rel in resolution.relations:
                await self.graph.upsert_relation(user_id, rel, source_ref=source_ref, at=anchor)
            await self.vector.add(user_id, facts, kind="fact", source_ref=source_ref, at=anchor)
            episode = user_message_of(text)
        else:
            # Third-party text: derived facts are signals (wrapped as untrusted in recall); no graph
            # relations, no profile or mood changes.
            await self.vector.add(user_id, facts, kind="signal", source_ref=source_ref, at=anchor)
            episode = own_words
        if len(episode.split()) >= MIN_EPISODE_WORDS:
            await self.vector.add(
                user_id, [episode[:500]], kind="episode" if trusted else "signal", source_ref=source_ref,
                at=anchor,
            )

        if trusted and extraction.profile_updates:
            await profile_repo.save(user_id, card.apply(extraction.profile_updates, at=anchor))
        self.invalidate(user_id)

        final = extraction.model_copy(
            update={"entities": resolution.entities, "relations": resolution.relations}
        )
        if not trusted:
            final = final.model_copy(update={"mood": None})
        # the anchor travels with the provenance: loop titles resolve against when the text was written (T2)
        prov = Provenance(source_ref=source_ref, trust=trust, conversation=conversation, anchor_at=anchor)
        for hook in self.on_extraction:
            try:
                await hook(user_id, final, prov)
            except Exception:
                log.error("memory.hook_failed", hook=repr(hook), exc_info=True)
        return final

    # --- user control ------------------------------------------------------------

    async def describe_user(self, user_id: int) -> str:
        await self.init()
        card = await profile_repo.get(user_id)
        facts = [d["statement"] for d in await self.graph.dump(user_id)
                 if not is_third_party(d.get("source_ref"))][-15:]
        parts = []
        if rendered := card.render():
            parts.append(rendered)
        if facts:
            parts.append("Things I've picked up:\n" + "\n".join(f"- {f}" for f in facts))
        return "\n\n".join(parts) or "I don't know much about you yet."

    async def forget(self, user_id: int, needle: str) -> int:
        if not needle.strip():
            return 0
        await self.init()
        removed = await self.graph.forget(user_id, needle) + await self.vector.forget(user_id, needle)
        card, changed = (await profile_repo.get(user_id)).remove_matching(needle)
        if changed:
            await profile_repo.save(user_id, card)
            removed += 1
        self.invalidate(user_id)
        return removed


_service: MemoryService | None = None


def set_memory(svc: MemoryService | None) -> None:
    global _service
    _service = svc


def get_memory() -> MemoryService:
    """Synchronous lazy singleton. Stores initialise on first use (see MemoryService.init)."""
    global _service
    if _service is None:
        s = get_settings()
        embedder = get_embedder()
        if s.qdrant_url:
            vector = QdrantVectorStore(embedder, url=s.qdrant_url)
        else:  # embedded: path=None would silently target localhost:6333
            vector = QdrantVectorStore(embedder, path=str(s.data_dir / "qdrant"))
        _service = MemoryService(make_graph(), vector, embedder)
    return _service
