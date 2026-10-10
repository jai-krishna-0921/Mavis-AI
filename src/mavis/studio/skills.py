"""The studio's skills: folders under mavis/studio/skills/, each a SKILL.md (Agent Skills format: YAML
frontmatter with name and description, then the guide) plus optional scripts/.

The studio loads the skill for the artifact kind (frontmatter metadata.renderer) and the skills that always
apply (anti-slop, brand-personalisation) into the sub-agent's prompt. New skills are new folders; nothing
else changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).parent / "skills"
ALWAYS = ("anti-slop", "brand-personalisation")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    renderer: str  # "deck" | "doc" | "sheet" | "" for guidance-only skills
    body: str


def _parse(path: Path) -> Skill | None:
    m = _FRONT.match(path.read_text(encoding="utf-8"))
    if m is None:
        return None
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and value.strip():
            meta[key.strip()] = value.strip()
    if not meta.get("name") or meta["name"] != path.parent.name:
        return None  # the format requires the name to match its folder
    return Skill(meta["name"], meta.get("description", ""), meta.get("renderer", ""), m.group(2).strip())


@cache
def all_skills() -> dict[str, Skill]:
    found = (_parse(p) for p in sorted(ROOT.glob("*/SKILL.md")))
    return {s.name: s for s in found if s is not None}


def for_kind(kind: str) -> list[Skill]:
    """The skill that renders `kind`, then the always-on skills."""
    skills = all_skills()
    main = [s for s in skills.values() if s.renderer == kind]
    return main + [skills[n] for n in ALWAYS if n in skills]


def prompt_block(kind: str) -> str:
    return "\n\n".join(f'<skill name="{s.name}">\n{s.body}\n</skill>' for s in for_kind(kind))
