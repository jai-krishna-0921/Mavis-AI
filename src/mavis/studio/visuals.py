# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
"""Raster pieces a document needs and Word cannot draw itself: the gradient cover band and themed charts.

Both are PNG bytes made from the theme alone (colours, gradient, fonts when installed), so they match the
slides drawn natively. Charts follow the deck's rules: one message, labelled values, no chart junk.
"""

from __future__ import annotations

import io
import logging
import math

from mavis.studio.spec import Chart
from mavis.studio.themes import Theme

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)  # a missing theme font falls back


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def gradient_band(stops: list[str], angle: int, width: int = 1950, height: int = 420) -> bytes:
    """A linear gradient (2 or 3 stops, CSS-like angle) as PNG bytes."""
    import numpy as np
    from PIL import Image

    cols = np.array([_rgb(c) for c in stops], dtype=float)
    rad = math.radians(angle)
    ys, xs = np.mgrid[0:height, 0:width]
    proj = xs * math.sin(rad) - ys * math.cos(rad)
    t = (proj - proj.min()) / (proj.max() - proj.min())
    segments = len(cols) - 1
    i = np.minimum((t * segments).astype(int), segments - 1)
    f = (t * segments - i)[..., None]
    rgb = cols[i] + (cols[i + 1] - cols[i]) * f
    out = io.BytesIO()
    Image.fromarray(rgb.astype("uint8"), "RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _luma(color: str) -> float:
    r, g, b = _rgb(color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def _fmt(value: float, number_format: str) -> str:
    fmt = number_format or ""
    prefix = "".join(ch for ch in fmt.split("#")[0].split("0")[0] if not ch.isspace() and ch not in "\"'")
    if "%" in fmt:
        return f"{value * 100:.0f}%" if abs(value) <= 1 else f"{value:.0f}%"
    digits = 0 if float(value).is_integer() or abs(value) >= 100 else 1
    return f"{prefix}{value:,.{digits}f}"


def chart_png(chart: Chart, theme: Theme, width_in: float = 6.4) -> bytes:
    """One themed chart as PNG bytes (200 dpi): labelled values, no top/right frame, a light value grid."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    palette = theme.palette()
    if theme.gradient:
        palette = [theme.gradient[-1], theme.primary, theme.secondary, theme.accent]
    # a near-black primary (minimal, editorial) reads as ink, not data: lead with the colours instead
    palette = [c for c in palette if _luma(c) > 0.18] + [c for c in palette if _luma(c) <= 0.18]
    family = [theme.body_font, "DejaVu Sans"]
    with plt.rc_context({"font.family": "sans-serif", "font.sans-serif": family, "font.size": 9,
                         "axes.edgecolor": theme.muted, "axes.labelcolor": theme.text,
                         "xtick.color": theme.text, "ytick.color": theme.muted, "text.color": theme.text}):
        fig, ax = plt.subplots(figsize=(width_in, width_in * 0.5), dpi=200)
        cats = list(chart.categories)
        series = chart.series[:4]
        if chart.kind in ("pie", "doughnut"):
            values = series[0].values[:len(cats)]
            colors = (palette * 4)[:len(values)]
            wedges, *_ = ax.pie(values, colors=colors, startangle=90, counterclock=False,
                                wedgeprops={"width": 0.42 if chart.kind == "doughnut" else 1,
                                            "edgecolor": "white", "linewidth": 1.5},
                                autopct=lambda p: f"{p:.0f}%" if p >= 4 else "", pctdistance=0.78,
                                textprops={"color": "white", "fontsize": 8, "fontweight": "bold"})
            ax.legend(wedges, cats, loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False)
            ax.axis("equal")
        else:
            n = len(series)
            idx = list(range(len(cats)))
            width = 0.8 / n
            for k, s in enumerate(series):
                vals = (list(s.values) + [0.0] * len(cats))[:len(cats)]
                color = palette[k % len(palette)]
                if chart.kind == "line":
                    ax.plot(idx, vals, color=color, linewidth=2.4, marker="o", markersize=4, label=s.name)
                    ax.annotate(_fmt(vals[-1], chart.number_format), (idx[-1], vals[-1]),
                                textcoords="offset points", xytext=(4, 4), fontsize=8, color=color)
                    continue
                pos = [i - 0.4 + width * (k + 0.5) for i in idx]
                bars = (ax.barh if chart.kind == "bar" else ax.bar)(pos, vals, width * 0.92, color=color,
                                                                     label=s.name)
                for b, v in zip(bars, vals, strict=True):
                    label = _fmt(v, chart.number_format)
                    if chart.kind == "bar":
                        ax.text(b.get_width(), b.get_y() + b.get_height() / 2, " " + label, va="center",
                                fontsize=8)
                    else:
                        ax.text(b.get_x() + b.get_width() / 2, b.get_height(), label, ha="center",
                                va="bottom", fontsize=8)
            if chart.kind == "bar":
                ax.set_yticks(idx, cats)
                ax.invert_yaxis()
                ax.xaxis.grid(True, color="#E6E6E6", linewidth=0.8)
                ax.set_xticks([])
            else:
                ax.set_xticks(idx, cats, rotation=0 if len(cats) <= 6 else 30,
                              ha="center" if len(cats) <= 6 else "right")
                ax.yaxis.grid(True, color="#E6E6E6", linewidth=0.8)
                ax.tick_params(axis="y", length=0)
            ax.set_axisbelow(True)
            for side in ("top", "right", "left" if chart.kind != "bar" else "bottom"):
                ax.spines[side].set_visible(False)
            if n > 1:
                ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, 1.12), ncol=n)
        fig.tight_layout()
        out = io.BytesIO()
        fig.savefig(out, format="png", dpi=200, facecolor="white")
        plt.close(fig)
    return out.getvalue()
