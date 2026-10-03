from mavis.agents.specialists.base import Specialist
from mavis.llm.models import Tier

KNOWLEDGE = Specialist(
    name="knowledge",
    description="Answers from what Mavis knows about the user (memory, open loops, notes); stores new facts.",
    prompt=(
        "You are Mavis's knowledge specialist. Use what_do_you_know to recall facts about the user and "
        "their people, remember to store durable facts the task produces, and track_loop to record "
        "commitments. Never invent personal facts; say what is unknown."
    ),
    tier=Tier.FAST,
    max_steps=4,
)
