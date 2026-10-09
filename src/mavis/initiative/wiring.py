"""Composition root for the initiative engine (one instance per process)."""

from __future__ import annotations

from dataclasses import dataclass

from mavis.bus.base import EventBus
from mavis.bus.leader import LeaderLock, make_leader
from mavis.config import get_settings
from mavis.domain.events import Provenance
from mavis.initiative.composer import Composer
from mavis.initiative.executor import InitiativeExecutor
from mavis.initiative.filters import Embed, EventFilter
from mavis.initiative.handler import InitiativeHandler, register
from mavis.initiative.quiet import QuietTracker
from mavis.initiative.reasoner import Reasoner
from mavis.initiative.routines import Routines
from mavis.loops.service import LoopService, loops_from_extraction
from mavis.memory import personal_layer
from mavis.policy.pings import PingPolicy
from mavis.timers.runner import TimerRunner
from mavis.timers.service import WakeupService


@dataclass
class Initiative:
    bus: EventBus
    loops: LoopService
    wakeups: WakeupService
    policy: PingPolicy
    composer: Composer
    reasoner: Reasoner
    quiet: QuietTracker
    executor: InitiativeExecutor
    routines: Routines
    handler: InitiativeHandler
    timer: TimerRunner

    async def loops_from_extraction(self, user_id: int, extraction, prov: Provenance) -> None:
        await loops_from_extraction(self.loops, user_id, extraction, prov)


def build_initiative(bus: EventBus, memory, *, leader: LeaderLock | None = None,
                     embed: Embed | None = None) -> Initiative:
    wakeups, loops, policy = WakeupService(), LoopService(bus), PingPolicy()
    composer, reasoner = Composer(memory), Reasoner(memory, policy)
    quiet = QuietTracker(wakeups)
    executor = InitiativeExecutor(bus, loops, wakeups, policy, composer, quiet)
    routines = Routines(loops, wakeups, executor)
    filt = EventFilter(embed, centrality=personal_layer.centrality)
    handler = InitiativeHandler(filt=filt, reasoner=reasoner, executor=executor, loops=loops,
                                wakeups=wakeups, routines=routines, quiet=quiet)
    timer = TimerRunner(bus, wakeups, leader or make_leader(), get_settings().timer_interval_s, loops=loops)
    return Initiative(bus, loops, wakeups, policy, composer, reasoner, quiet, executor, routines, handler,
                      timer)


_current: Initiative | None = None


def set_current(initiative: Initiative | None) -> None:
    global _current
    _current = initiative


def current() -> Initiative:
    """The process Initiative, built lazily. Handler registration is wire_initiative's job (startup)."""
    return _current if _current is not None else wire_initiative(register_handlers=False)


def wire_initiative(register_handlers: bool = True) -> Initiative:
    """Build (once) and hook the process Initiative; register its event handlers on every call.

    Registration is idempotent (register_event_handler dedupes), and must not be skipped when the
    instance is cached: tests clear the handler registry between runs.
    """
    global _current
    import mavis.bus
    import mavis.memory.service

    if _current is None:
        _current = build_initiative(mavis.bus.get_bus(), mavis.memory.service.get_memory())
    init = _current
    memory = mavis.memory.service.get_memory()
    # bound methods of the same instance compare equal, so this hooks each memory object once
    if init.loops_from_extraction not in memory.on_extraction:
        memory.on_extraction.append(init.loops_from_extraction)
    memory.set_loops_reader(init.loops)
    if register_handlers:
        register(init.handler)
    return init
