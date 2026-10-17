from __future__ import annotations

import subprocess
import sys


def test_importing_the_job_handler_module_registers_every_pass():
    """The worker imports only `runner` (wiring._notion_sync_job); a fresh interpreter must then know the
    passes, with PASSES untouched."""
    code = (
        "from mavis.tools.integrations.notion_sync.runner import PASSES, notion_sync_job\n"
        "print(sorted((order, name) for order, name, _, _ in PASSES))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    registered = eval(out.strip())  # noqa: S307 - our own subprocess output
    assert {(30, "meetings"), (40, "todos")} <= set(registered)
