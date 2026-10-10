"""The deploy's rsync excludes never drop code the app needs.

An unanchored --exclude 'data/' matched src/mavis/access/data/ as well as the repo's data/ folder, so the
timezone and currency tables never reached production and every onboarding and reset failed reading them
(evals 2026-10-10). A pattern without a leading slash matches at any depth.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "aws" / "deploy.sh"


def _excludes() -> list[str]:
    return re.findall(r"--exclude '([^']+)'", DEPLOY.read_text(encoding="utf-8"))


def _shipped_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "src", "scripts", "pyproject.toml", "uv.lock"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line and "__pycache__" not in line]


def test_no_exclude_drops_a_tracked_runtime_file():
    dropped = []
    for path in _shipped_files():
        parts = path.split("/")
        for pattern in _excludes():
            if pattern.startswith("/"):
                anchored = pattern.strip("/")
                if path == anchored or path.startswith(anchored + "/"):
                    dropped.append((path, pattern))
            elif pattern.endswith("/"):
                if pattern.rstrip("/") in parts[:-1]:
                    dropped.append((path, pattern))
            elif any(Path(p).match(pattern) for p in parts):
                if not pattern.startswith("*."):  # *.pem and the like are never runtime files
                    dropped.append((path, pattern))
    assert not dropped, f"rsync would not ship: {dropped[:5]}"


def test_runtime_data_tables_are_tracked():
    files = set(_shipped_files())
    assert "src/mavis/access/data/zone_country.tsv" in files
    assert "src/mavis/access/data/cities.tsv.gz" in files
