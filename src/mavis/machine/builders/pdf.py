import json
import sys

from fpdf import FPDF

spec = json.load(open(sys.argv[1], encoding="utf-8"))


def txt(s):
    return str(s).encode("latin-1", "replace").decode("latin-1")  # core fonts are latin-1; others become ?


def line(h, s):
    # multi_cell leaves the cursor at the right margin by default; go back to the left for the next line
    pdf.multi_cell(0, h, txt(s), new_x="LMARGIN", new_y="NEXT")


pdf = FPDF()
pdf.set_auto_page_break(auto=True, margin=15)
pdf.add_page()
pdf.set_font("Helvetica", "B", 18)
line(10, spec.get("title", ""))
for sec in spec.get("sections", []):
    pdf.set_font("Helvetica", "B", 14)
    line(8, sec.get("heading", ""))
    pdf.set_font("Helvetica", "", 11)
    for p in sec.get("paragraphs", []):
        line(6, p)
    for b in sec.get("bullets", []):
        line(6, f"- {b}")
    for row in sec.get("table") or []:
        line(6, " | ".join(str(v) for v in row))
pdf.output(sys.argv[2])
