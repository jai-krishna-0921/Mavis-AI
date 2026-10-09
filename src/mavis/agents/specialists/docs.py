from mavis.agents.specialists.base import Specialist
from mavis.llm.models import Tier

DOCS = Specialist(
    name="docs",
    description="Makes files: slide decks (PPTX), Word documents, PDFs and spreadsheets, from an outline it "
    "writes.",
    prompt=(
        "You are Mavis's document maker. Write a clear outline first, then call exactly one builder "
        "(make_pptx, make_docx, make_pdf or make_xlsx) with it. Keep slide bullets short (8 per slide at "
        "most). The file is sent to the user when it is built. Answer in one or two sentences about what "
        "you made."
    ),
    tier=Tier.SMART,
    tool_names=(
        "make_pptx",
        "make_docx",
        "make_pdf",
        "make_xlsx",
        "make_chart",
        "files_list",
        "files_read",
        "files_send",
    ),
    max_steps=12,
    timeout_s=600,
    machine=True,
)
