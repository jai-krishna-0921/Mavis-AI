"""One entry point for the three renderers, plus the small CLI the studio skill shells out to."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from mavis.studio.render_docx import render_doc
from mavis.studio.render_pptx import render_deck
from mavis.studio.render_xlsx import render_sheet
from mavis.studio.spec import SPECS
from mavis.studio.themes import Theme, theme_for


def render(kind: str, spec: BaseModel, theme: Theme, path: Path, **kw) -> Path:
    """Render a validated spec of this kind (deck, doc, sheet) to `path`."""
    if kind == "deck":
        return render_deck(spec, theme, path, **kw)  # type: ignore[arg-type]
    if kind == "doc":
        return render_doc(spec, theme, path, **kw)  # type: ignore[arg-type]
    if kind == "sheet":
        return render_sheet(spec, theme, path)  # type: ignore[arg-type]
    raise ValueError(f"unknown kind: {kind}")


def main(kind: str, argv: list[str]) -> int:
    """CLI: SPEC_JSON_PATH OUT_PATH [--theme NAME]. Prints the output path; 1 on error."""
    args = list(argv)
    name = "minimal"
    if "--theme" in args:
        i = args.index("--theme")
        if i + 1 >= len(args):
            print("--theme needs a name", file=sys.stderr)
            return 1
        name = args[i + 1]
        del args[i : i + 2]
    if len(args) != 2 or kind not in SPECS:
        print("usage: SPEC_JSON_PATH OUT_PATH [--theme NAME]", file=sys.stderr)
        return 1
    try:
        spec = SPECS[kind].model_validate_json(Path(args[0]).read_text(encoding="utf-8"))
        out = render(kind, spec, theme_for(name or "minimal"), Path(args[1]))
    except Exception as exc:  # noqa: BLE001 - a short message beats a traceback for the caller
        print(f"render failed: {str(exc)[:300]}", file=sys.stderr)
        return 1
    print(out)
    return 0
