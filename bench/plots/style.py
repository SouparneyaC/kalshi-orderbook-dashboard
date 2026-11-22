"""Colours, fonts and small helpers shared by the plot scripts.

The palette is one blue hue plus neutrals, in a light and a dark version. Each plot is drawn twice so that
README.md can show the right one with a <picture> element. Text uses the text colours and never the series colour.
"""
import csv
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
NUMBERS = ROOT / "numbers"
OUT = ROOT / "docs" / "img"

THEMES = {
    "light": dict(
        surface="#fcfcfb", text="#0b0b0b", text2="#52514e", grid="#e6e5e1", line="#b9b8b2",
        series="#2a78d6", ramp_low="#86b6ef", ramp_high="#184f95", muted="#898a84",
    ),
    "dark": dict(
        surface="#1a1a19", text="#f0efec", text2="#c3c2b7", grid="#383835", line="#5b5a56",
        series="#3987e5", ramp_low="#5598e7", ramp_high="#cde2fb", muted="#8c8b85",
    ),
}


def load_numbers():
    """Every row of numbers/*.csv with a numeric value, by id."""
    values = {}
    for path in sorted(NUMBERS.glob("numbers*.csv")):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    values[row["id"]] = float(row["value"])
                except (KeyError, ValueError):
                    continue
    return values


def fmt(v):
    """A value in milliseconds or percent, with the digits a reader needs."""
    if v < 1:
        return f"{v:.3g}"
    if v < 10:
        return f"{v:.1f}"
    return f"{v:,.0f}"


def setup():
    plt.rcParams.update({
        "svg.fonttype": "path",          # text becomes shapes, so it looks the same on every machine
        "svg.hashsalt": "kalshi-orderbook",
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.linewidth": 0.8,
    })


def new_axes(theme, width, height, **kw):
    t = THEMES[theme]
    fig = plt.figure(figsize=(width, height), facecolor=t["surface"])
    return fig, t


def style_axes(ax, t, grid_axis="x"):
    ax.set_facecolor(t["surface"])
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(t["line"])
    ax.tick_params(colors=t["text2"], length=3, width=0.8)
    ax.grid(axis=grid_axis, color=t["grid"], linewidth=0.8)
    ax.set_axisbelow(True)


def save(fig, name, theme):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}-{theme}.svg"
    fig.savefig(path, format="svg", facecolor=fig.get_facecolor(), metadata={"Date": None, "Creator": None})
    preview = os.environ.get("PLOT_PREVIEW_DIR")          # optional: also write a PNG to look at
    if preview:
        Path(preview).mkdir(parents=True, exist_ok=True)
        fig.savefig(Path(preview) / f"{name}-{theme}.png", dpi=170, facecolor=fig.get_facecolor())
    plt.close(fig)
    return path


def luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)
