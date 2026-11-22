"""Draw the plots in docs/img from the files in numbers/.

    python bench/plots/make_plots.py          writes docs/img/<name>-light.svg and <name>-dark.svg
    python bench/plots/make_plots.py --check  also prints the text and mark contrast against each surface

It needs matplotlib (requirements-bench.txt) and no server, key or network. Every value is read from numbers/,
so a plot cannot disagree with the tables in docs/benchmarks.md.
"""
import sys

from matplotlib.lines import Line2D
from matplotlib.patches import FancyBboxPatch, Patch
from matplotlib.ticker import FuncFormatter

from tables import update_docs
from style import THEMES, contrast, fmt, load_numbers, new_axes, save, setup, style_axes


def latency_hops(v, theme):
    fig, t = new_axes(theme, 8.2, 3.1)
    ax = fig.add_axes([0.385, 0.17, 0.585, 0.76])
    style_axes(ax, t)
    n_all = int(v["window.minutes"])
    rows = [
        ("Kalshi timestamp to receipt", f"all {n_all:,} minutes", "lat.venue_to_recv.book"),
        ("Receipt to state updated", f"all {n_all:,} minutes", "lat.recv_to_hub"),
        ("Receipt to send, with the 100 ms throttle",
         f"{int(v['browser.minutes_with_samples.recv_to_send']):,} of {n_all:,} minutes, browser open", "lat.recv_to_send"),
        ("Send to render, as reported by the tab",
         f"{int(v['browser.minutes_with_samples.send_to_render']):,} of {n_all:,} minutes, tab reporting", "lat.send_to_render"),
    ]
    for i, (label, sub, key) in enumerate(rows):
        y = 3 - i
        p50, p99 = v[f"{key}.p50"], v[f"{key}.p99"]
        ax.plot([p50, p99], [y, y], color=t["line"], linewidth=1.6, solid_capstyle="round", zorder=2)
        ax.plot([p50], [y], "o", color=t["ramp_low"], markersize=9, markeredgecolor=t["surface"], markeredgewidth=1.5, zorder=3)
        ax.plot([p99], [y], "s", color=t["ramp_high"], markersize=8, markeredgecolor=t["surface"], markeredgewidth=1.5, zorder=3)
        ax.annotate(fmt(p50), (p50, y), xytext=(-8, 0), textcoords="offset points", ha="right", va="center", color=t["text"], fontsize=8.5)
        ax.annotate(fmt(p99), (p99, y), xytext=(8, 0), textcoords="offset points", ha="left", va="center", color=t["text"], fontsize=8.5)
        tr = ax.get_yaxis_transform()
        ax.text(-0.03, y + 0.12, label, transform=tr, ha="right", va="center", color=t["text"], fontsize=8.8)
        ax.text(-0.03, y - 0.2, sub, transform=tr, ha="right", va="center", color=t["text2"], fontsize=7.4)
    ax.set_xscale("log")
    ax.set_xlim(0.0015, 40000)
    ax.set_ylim(-0.6, 3.6)
    ax.set_yticks([])
    ticks = [0.01, 0.1, 1, 10, 100, 1000, 10000]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{x:,}".rstrip("0").rstrip(".") if x < 1 else f"{x:,.0f}" for x in ticks], color=t["text2"], fontsize=8)
    ax.minorticks_off()
    ax.set_xlabel("milliseconds, log scale", color=t["text2"], fontsize=8.5)
    handles = [
        Line2D([], [], marker="o", linestyle="", color=t["ramp_low"], markersize=8, markeredgecolor=t["surface"], label="median"),
        Line2D([], [], marker="s", linestyle="", color=t["ramp_high"], markersize=7, markeredgecolor=t["surface"], label="p99"),
    ]
    ax.legend(handles=handles, loc="upper right", frameon=False, labelcolor=t["text2"], fontsize=8.5, handletextpad=0.3)
    return save(fig, "latency-hops", theme)


def cpu_by_handler(v, theme):
    fig, t = new_axes(theme, 8.2, 3.3)
    ax = fig.add_axes([0.335, 0.16, 0.62, 0.8])
    style_axes(ax, t)
    named = v["cpu.named_handlers_share_of_process"] / 100
    share = lambda k: v[f"cpu.handler.{k}.share"] * named  # noqa: E731  (share of the whole process)
    small = sum(v[f"cpu.handler.{k}.share"] for k in
                ("orderbook_snapshot", "market_lifecycle_v2", "ok", "event_lifecycle", "subscribed", "unsubscribed")) * named
    rows = [
        ("Not attributed to a handler timer", 100 - v["cpu.named_handlers_share_of_process"], True),
        ("Order book messages", share("orderbook_delta"), False),
        ("Ticker messages", share("ticker"), False),
        ("Building browser payloads", share("payload_build"), False),
        ("Trade messages", share("trade"), False),
        ("Book summaries", share("book_summary"), False),
        ("Choosing which markets get books", share("promotion_check"), False),
        ("Other message types", small, False),
    ]
    for i, (label, val, grey) in enumerate(rows):
        y = len(rows) - 1 - i
        ax.barh(y, val, height=0.52, color=t["muted"] if grey else t["series"], zorder=3)
        text = "<0.1%" if val < 0.05 else f"{val:.1f}%"
        ax.text(val + 1.0, y, text, va="center", ha="left", color=t["text"], fontsize=8.5)
        ax.text(-0.02, y, label, transform=ax.get_yaxis_transform(), ha="right", va="center", color=t["text"], fontsize=8.8)
    ax.set_xlim(0, 75)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_yticks([])
    ax.set_xticks([0, 20, 40, 60])
    ax.set_xticklabels(["0%", "20%", "40%", "60%"], color=t["text2"], fontsize=8)
    ax.set_xlabel("share of the process's CPU time", color=t["text2"], fontsize=8.5)
    return save(fig, "cpu-by-handler", theme)


def perf_split(v, theme):
    fig, t = new_axes(theme, 8.2, 3.5)
    ax = fig.add_axes([0.335, 0.15, 0.62, 0.8])
    style_axes(ax, t)

    def two(name):
        return v[f"profile.perf.{name}.run1"], v[f"profile.perf.{name}.run2"]

    net, kern = two("kernel_net_share"), two("kernel_share")
    rows = [
        ("Python interpreter running Python code", two("interp_share")),
        ("C library (libc)", two("libc_share")),
        ("Kernel, TCP receive", net),
        ("Kernel, everything else", (kern[0] - net[0], kern[1] - net[1])),
        ("JSON decoding (orjson)", two("orjson_share")),
        ("TLS decryption", two("tls_share")),
        ("Decompression (zlib)", two("zlib_share")),
        ("DuckDB", two("duckdb_share")),
    ]
    for i, (label, (a, b)) in enumerate(rows):
        y = len(rows) - 1 - i
        ax.barh(y, a, height=0.5, color=t["series"], zorder=3)
        ax.plot([b], [y], "D", color=t["ramp_high"], markersize=7, markeredgecolor=t["surface"], markeredgewidth=1.4, zorder=4)
        ax.text(max(a, b) + 1.4, y, f"{a:.1f} and {b:.1f}", va="center", ha="left", color=t["text"], fontsize=8.5)
        ax.text(-0.02, y, label, transform=ax.get_yaxis_transform(), ha="right", va="center", color=t["text"], fontsize=8.8)
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.set_yticks([])
    ax.set_xticks([0, 20, 40, 60, 80])
    ax.set_xticklabels(["0%", "20%", "40%", "60%", "80%"], color=t["text2"], fontsize=8)
    ax.set_xlabel("share of CPU cycles", color=t["text2"], fontsize=8.5)
    handles = [
        Patch(facecolor=t["series"], label="run 1"),
        Line2D([], [], marker="D", linestyle="", color=t["ramp_high"], markersize=6.5, markeredgecolor=t["surface"], label="run 2"),
    ]
    ax.legend(handles=handles, loc="lower right", frameon=False, labelcolor=t["text2"], fontsize=8.5, handletextpad=0.4)
    return save(fig, "perf-split", theme)


def capacity(v, theme):
    fig, t = new_axes(theme, 8.2, 2.9)
    books = [200, 400, 500, 600, 700, 800, 900, 1000]
    panels = [
        ("CPU, average, % of one core", "cpu_avg", (0, 40), "{:.0f}"),
        ("Event loop lag, p99, ms", "lag_p99", (0, 60), "{:.0f}"),
        ("Messages per second", "msgs_s", (0, 7500), "{:,.0f}"),
    ]
    for k, (title, key, ylim, f) in enumerate(panels):
        ax = fig.add_axes([0.065 + k * 0.325, 0.2, 0.27, 0.62])
        style_axes(ax, t, grid_axis="y")
        ys = [v[f"capacity.{n}.{key}"] for n in books]
        ax.plot(books, ys, color=t["series"], linewidth=1.6, marker="o", markersize=6.5,
                markeredgecolor=t["surface"], markeredgewidth=1.2, zorder=3)
        for n, y, ha, dx in ((books[0], ys[0], "left", -6), (books[-1], ys[-1], "center", 0)):
            ax.annotate(f.format(y), (n, y), xytext=(dx, 8), textcoords="offset points", ha=ha, va="bottom", color=t["text"], fontsize=8.2)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax.set_ylim(*ylim)
        ax.set_xlim(150, 1050)
        ax.set_xticks([200, 400, 600, 800, 1000])
        ax.tick_params(axis="x", labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.set_title(title, loc="left", color=t["text2"], fontsize=8.5, pad=8)
        ax.set_xlabel("books held", color=t["text2"], fontsize=8.2)
    fig.text(0.065, 0.035, "Each step ran 9 to 26 minutes, except 200 books, which is a window of 1,070 minutes.",
             color=t["text2"], fontsize=7.6, ha="left")
    return save(fig, "capacity", theme)


def path_diagram(theme):
    fig, t = new_axes(theme, 9.2, 3.7)
    ax = fig.add_axes([0.01, 0.02, 0.98, 0.96])
    ax.set_facecolor(t["surface"])
    ax.axis("off")
    w, h, gap = 1.0, 0.86, 0.26
    step = w + gap
    top, low = 2.3, 0.72
    ax.set_xlim(-0.15, 8 * step - gap + 0.15)
    ax.set_ylim(-0.35, 3.75)

    def box(i, y, title, detail):
        x = i * step
        ax.add_patch(FancyBboxPatch((x, y - h / 2), w, h, boxstyle="round,pad=0,rounding_size=0.07",
                                    facecolor=t["surface"], edgecolor=t["line"], linewidth=1.1, zorder=2))
        lines = title.split("\n")
        n_detail = len(detail)
        top_y = y + (len(lines) * 0.13 + n_detail * 0.095) / 2 - 0.05
        for j, line in enumerate(lines):
            ax.text(x + w / 2, top_y - j * 0.15, line, ha="center", va="center", color=t["text"], fontsize=8.3, zorder=3)
        base = top_y - len(lines) * 0.15 - 0.02
        for j, line in enumerate(detail):
            ax.text(x + w / 2, base - j * 0.105, line, ha="center", va="center", color=t["text2"], fontsize=6.4, zorder=3)

    def arrow(x0, y0, x1, y1):
        ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                    arrowprops=dict(arrowstyle="-|>", color=t["text2"], linewidth=1.1, shrinkA=0, shrinkB=0, mutation_scale=8), zorder=1)

    row1 = [
        ("Kalshi", []),
        ("Read and\ndecode", []),
        ("Apply to\nthe book", ["mark changed"]),
        ("Push\ntrigger", ["every 20th", "update, one per", "100 ms at most"]),
        ("Build\npayload", ["changed", "markets"]),
        ("Queues", ["50 shared,", "20 per tab"]),
        ("Send", ["5 s timeout"]),
        ("Browser", ["market list"]),
    ]
    for i, (title, detail) in enumerate(row1):
        box(i, top, title, detail)
        if i:
            arrow((i - 1) * step + w, top, i * step, top)
    row2 = [
        (2, "Wake\nthe tab", ["watching", "this market"]),
        (3, "One frame", ["every 50 ms", "at most,", "full depth"]),
        (4, "Browser", ["market page"]),
    ]
    for i, title, detail in row2:
        box(i, low, title, detail)
    arrow(2 * step + w / 2, top - h / 2, 2 * step + w / 2, low + h / 2)
    arrow(2 * step + w, low, 3 * step, low)
    arrow(3 * step + w, low, 4 * step, low)
    ax.text(3 * step + w / 2, low - h / 2 - 0.2, "no timing stage on this path", ha="center", va="center",
            color=t["text2"], fontsize=7.4, style="italic")

    def bracket(x0, x1, y, label, two_lines=False):
        ax.plot([x0, x0, x1, x1], [y - 0.07, y, y, y - 0.07], color=t["series"], linewidth=1.6, solid_joinstyle="round", zorder=2)
        ax.text((x0 + x1) / 2, y + 0.12, label, ha="center", va="bottom", color=t["text2"], fontsize=7.8, linespacing=1.15)

    y1 = top + h / 2 + 0.12
    bracket(0.5 * w, step - 0.04, y1, "Kalshi timestamp\nto receipt")
    bracket(step + 0.05, 2 * step + w, y1, "receipt to state updated")
    bracket(5 * step + w / 2, 7 * step + w / 2, y1, "send to render")
    bracket(step + 0.04, 6 * step + w, y1 + 0.62, "receipt to send")
    return save(fig, "path", theme)


def main():
    setup()
    v = load_numbers()
    made = []
    for theme in THEMES:
        made += [latency_hops(v, theme), path_diagram(theme), cpu_by_handler(v, theme), perf_split(v, theme), capacity(v, theme)]
    for p in made:
        print("wrote", p.relative_to(p.parents[2]))
    update_docs(v)
    if "--check" in sys.argv:
        for name, t in THEMES.items():
            print(f"\n{name}: contrast against surface {t['surface']}")
            for key in ("text", "text2", "series", "ramp_low", "ramp_high", "muted", "line"):
                print(f"  {key:10s} {t[key]}  {contrast(t[key], t['surface']):.2f}:1")


if __name__ == "__main__":
    main()
