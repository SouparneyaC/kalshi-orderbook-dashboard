"""The tables in docs/benchmarks.md, written from the same rows of numbers/ as the plots.

make_plots.py calls update_docs(). It replaces the text between <!-- table:NAME --> and <!-- /table:NAME -->.
"""
import re

from style import ROOT, fmt


def build(v):
    n_all = int(v["window.minutes"])
    out = {}

    stages = [
        ("Kalshi timestamp to receipt, order book updates", "lat.venue_to_recv.book", n_all),
        ("Kalshi timestamp to receipt, trades", "lat.venue_to_recv.trade", n_all),
        ("Receipt to state updated", "lat.recv_to_hub", n_all),
        ("Build the payload", "lat.build", int(v["browser.minutes_with_samples.build"])),
        ("Wait in the shared queue", "lat.queue_wait", int(v["browser.minutes_with_samples.queue_wait"])),
        ("Encode and hand to each tab", "lat.serialize_send", int(v["browser.minutes_with_samples.serialize_send"])),
        ("Receipt to send, with the 100 ms throttle", "lat.recv_to_send", int(v["browser.minutes_with_samples.recv_to_send"])),
        ("Send to render, as reported by the tab", "lat.send_to_render", int(v["browser.minutes_with_samples.send_to_render"])),
    ]
    rows = ["| Stage | Median (ms) | p99 (ms) | p99.9 (ms) | Minutes with samples | Rows |", "|---|---|---|---|---|---|"]
    for label, key, minutes in stages:
        rows.append(f"| {label} | {fmt(v[key + '.p50'])} | {fmt(v[key + '.p99'])} | {fmt(v[key + '.p999'])} | "
                    f"{minutes:,} of {n_all:,} | `{key}.*` |")
    out["latency"] = "\n".join(rows)

    named = v["cpu.named_handlers_share_of_process"] / 100
    handlers = [("Order book messages", "orderbook_delta"), ("Ticker messages", "ticker"),
                ("Building browser payloads", "payload_build"), ("Trade messages", "trade"),
                ("Book summaries", "book_summary"), ("Choosing which markets get books", "promotion_check")]
    rows = ["| Work | Share of the handler timers | Share of the process's CPU time | Row |", "|---|---|---|---|"]
    for label, key in handlers:
        share = v[f"cpu.handler.{key}.share"]
        rows.append(f"| {label} | {share:.1f}% | {share * named:.1f}% | `cpu.handler.{key}.share` |")
    rows.append(f"| Not attributed to a handler timer | | {100 - v['cpu.named_handlers_share_of_process']:.1f}% | "
                "100 minus `cpu.named_handlers_share_of_process` |")
    out["cpu"] = "\n".join(rows)

    def two(name):
        return v[f"profile.perf.{name}.run1"], v[f"profile.perf.{name}.run2"]

    net, kern = two("kernel_net_share"), two("kernel_share")
    split = [
        ("Python interpreter running Python code", two("interp_share"), "interp_share"),
        ("C library (libc)", two("libc_share"), "libc_share"),
        ("Kernel, TCP receive", net, "kernel_net_share"),
        ("Kernel, everything else", (kern[0] - net[0], kern[1] - net[1]), "kernel_share minus kernel_net_share"),
        ("JSON decoding (orjson)", two("orjson_share"), "orjson_share"),
        ("TLS decryption", two("tls_share"), "tls_share"),
        ("Decompression (zlib)", two("zlib_share"), "zlib_share"),
        ("DuckDB", two("duckdb_share"), "duckdb_share"),
    ]
    rows = ["| Where the cycles went | Run 1 | Run 2 | Rows (`profile.perf.*`) |", "|---|---|---|---|"]
    for label, (a, b), key in split:
        rows.append(f"| {label} | {a:.1f}% | {b:.1f}% | `{key}` |")
    out["perf"] = "\n".join(rows)

    rows = ["| Books held | Minutes judged | CPU, average (% of one core) | CPU, p95 | Event loop lag p99 (ms) "
            "| Messages per second | Steal, average | Sequence gaps |", "|---|---|---|---|---|---|---|---|"]
    for n in (200, 400, 500, 600, 700, 800, 900, 1000):
        def c(k, n=n):
            return v[f"capacity.{n}.{k}"]
        rows.append(f"| {n:,} | {int(c('minutes')):,} | {c('cpu_avg'):.1f} | {c('cpu_p95'):.1f} | {c('lag_p99'):.1f} | "
                    f"{c('msgs_s'):,.0f} | {c('steal_avg'):.2f}% | {int(c('gaps'))} |")
    out["capacity"] = "\n".join(rows)

    health = [
        ("Messages received", f"{int(v['msgs.total']):,}", "msgs.total"),
        ("Messages per second, average", f"{v['msgs.per_s_avg']:,.0f}", "msgs.per_s_avg"),
        ("Messages per second, busiest minute", f"{v['msgs.per_s_peak_minute']:,.0f}", "msgs.per_s_peak_minute"),
        ("Books held, average", f"{v['books.avg']:.0f}", "books.avg"),
        ("CPU, average (% of one core)", f"{v['cpu.avg']:.1f}", "cpu.avg"),
        ("Memory, average (MB)", f"{v['rss.avg']:.1f}", "rss.avg"),
        ("Memory, highest minute (MB)", f"{v['rss.max']:.1f}", "rss.max"),
        ("Steal, average (% of CPU time)", f"{v['steal.avg']:.2f}", "steal.avg"),
        ("Steal, highest minute (% of CPU time)", f"{v['steal.max']:.1f}", "steal.max"),
        ("Sequence gaps in order book messages", f"{int(v['seq.gaps'])}", "seq.gaps"),
        ("Restarts", f"{int(v['window.restarts'])}", "window.restarts"),
    ]
    rows = ["| Over the 48 hours | Value | Row |", "|---|---|---|"]
    rows += [f"| {a} | {b} | `{c}` |" for a, b, c in health]
    out["health"] = "\n".join(rows)
    return out


def update_docs(v):
    path = ROOT / "docs" / "benchmarks.md"
    text = path.read_text()
    for name, body in build(v).items():
        pattern = re.compile(rf"(<!-- table:{name} -->)\n.*?(<!-- /table:{name} -->)", re.S)
        if not pattern.search(text):
            raise SystemExit(f"docs/benchmarks.md has no table:{name} markers")
        text = pattern.sub(lambda m, b=body: m.group(1) + "\n" + b + "\n" + m.group(2), text)
    path.write_text(text)
    print("updated tables in docs/benchmarks.md")
