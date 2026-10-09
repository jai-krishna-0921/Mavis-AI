from __future__ import annotations

from importlib import resources

BUILDER_IMPORTS: dict[str, dict[str, str]] = {
    "chart": {"matplotlib": "matplotlib"},
    "xlsx": {"openpyxl": "openpyxl"},
    "docx": {"docx": "python-docx"},
    "pdf": {"fpdf": "fpdf2"},
    "pptx": {"pptx": "python-pptx"},
    "extract": {"pypdf": "pypdf", "docx": "python-docx", "openpyxl": "openpyxl"},
}


def load(name: str) -> str:
    if name not in BUILDER_IMPORTS:
        raise KeyError(name)
    return resources.files(__package__).joinpath(f"{name}.py").read_text(encoding="utf-8")
