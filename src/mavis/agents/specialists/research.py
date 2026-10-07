from mavis.agents.specialists.base import Specialist
from mavis.llm.models import Tier

RESEARCH = Specialist(
    name="research",
    description="Finds current information on the web, cross-checks it and cites sources.",
    prompt=(
        "You are Mavis's research specialist. Use web_search with 2-4 focused queries, open the most "
        "relevant pages with web_extract, and cross-check key claims across sources. Answer concisely. "
        "Cite sources inline as [n] and end with a list `[n] Title: URL`. Say plainly when something "
        "could not be verified."
    ),
    tier=Tier.SMART,
    tool_names=("web_search", "web_extract"),
    max_steps=10,
    steps_setting="research_max_steps",  # configurable; on the limit it wraps up with what it found
)
