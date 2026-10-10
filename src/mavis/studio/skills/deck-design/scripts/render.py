"""Render a deck spec (JSON) into a file in a theme: what the studio runs for this skill.

    python src/mavis/studio/skills/<skill>/scripts/render.py SPEC.json OUT [--theme NAME]

Kept beside the skill so the skill folder is complete (guide plus script); the logic lives in
mavis.studio.render (one renderer per kind).
"""

from __future__ import annotations

import sys

from mavis.studio.render import main

if __name__ == "__main__":
    sys.exit(main("deck", sys.argv[1:]))
