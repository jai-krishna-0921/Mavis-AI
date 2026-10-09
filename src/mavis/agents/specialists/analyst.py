from mavis.agents.specialists.base import Specialist
from mavis.llm.models import Tier

ANALYST = Specialist(
    name="analyst",
    description="Runs Python on the user's files and data in a private machine: analysis, numbers, charts, "
    "spreadsheets, scripts it tests before sending.",
    prompt=(
        "You are Mavis's analyst. Work in the user's private machine (no internet inside). Files the user "
        "sent are in inbox/. Look before you compute: list and read the inputs first. Write code that saves "
        "deliverables under out/ (they are sent to the user as soon as they exist). Run it, read the exit "
        "code and output, fix and rerun when it fails. Check your numbers with a second, independent "
        "calculation when they matter. Use make_chart and make_xlsx for charts and tables. Answer with the "
        "key numbers in plain words; do not paste code unless asked."
    ),
    tier=Tier.SMART,
    tool_names=(
        "machine_run_python",
        "machine_run_shell",
        "machine_install",
        "machine_fetch",
        "files_list",
        "files_read",
        "files_write",
        "files_attach",
        "files_delete",
        "files_send",
        "make_chart",
        "make_xlsx",
    ),
    steps_setting="analyst_max_steps",
    timeout_s=600,
    machine=True,
)
