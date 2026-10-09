import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

spec = json.load(open(sys.argv[1], encoding="utf-8"))
fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
kind = spec.get("kind", "line")
x = [str(v) for v in spec.get("x", [])]
series = spec.get("series", [])
if kind == "pie" and series:
    ax.pie(series[0]["values"], labels=x, autopct="%1.0f%%")
else:
    width = 0.8 / max(1, len(series))
    for i, s in enumerate(series):
        vals = [float(v) for v in s.get("values", [])]
        if kind == "bar":
            ax.bar([j + i * width for j in range(len(vals))], vals, width=width, label=str(s.get("name", "")))
            ax.set_xticks([j + width * (len(series) - 1) / 2 for j in range(len(x))], x)
        else:
            ax.plot(x[: len(vals)], vals, marker="o", label=str(s.get("name", "")))
    ax.set_xlabel(str(spec.get("x_label", "")))
    ax.set_ylabel(str(spec.get("y_label", "")))
    if len(series) > 1:
        ax.legend()
ax.set_title(str(spec.get("title", ""))[:120])
fig.tight_layout()
fig.savefig(sys.argv[2])
