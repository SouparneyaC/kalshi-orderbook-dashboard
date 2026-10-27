"""
Run a fixed fake stream through the real code and print everything the outside world sees,
normalized, as JSON: browser payloads, order book summaries, recent trades, and the exact rows
that would be written to DuckDB.

Must run with PYTHONHASHSEED=0 (test_golden.py does this) so set ordering is repeatable.
"""
import json
import os
import random
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")
random.seed(7)

from app.kalshi import kalshi_feed    # noqa: E402
from app.core import market_state   # noqa: E402
from app.storage import persistence    # noqa: E402
from tests.sim_stream import SimStream  # noqa: E402

# Values that depend on wall-clock time or on hash functions we are allowed to change
DROP = {"latency", "server_ts", "uptime_s", "distinct_active"}


def norm(x):
    if isinstance(x, dict):
        return {k: norm(v) for k, v in sorted(x.items()) if k not in DROP}
    if isinstance(x, (list, tuple)):
        return [norm(v) for v in x]
    if isinstance(x, float):
        return float(f"{x:.9g}")
    if isinstance(x, str):
        try:
            d = Decimal(x)
            if d.is_finite():
                return f"num:{d.normalize()}"   # "0.3900" == "0.39", "0" == "0.00"
        except InvalidOperation:
            pass
    return x


def norm_payload(p):
    p = dict(p)
    if "markets_changed" in p:
        p["markets_changed"] = sorted(p["markets_changed"], key=lambda m: m["ticker"])
    return norm(p)


def db_rows():
    """The rows the DB writer would insert, in order."""
    if hasattr(persistence, "rows_for_insert"):
        return persistence.rows_for_insert(kalshi_feed.pending_trades)
    return [(t.get("ts"), t.get("ticker"),
             float(t["price"]) if t.get("price") else None,
             float(t["count"]) if t.get("count") else None,
             t.get("side"), t.get("book_side"), t.get("recv_ts"), t.get("trade_id"))
            for t in kalshi_feed.pending_trades]


def main():
    sim = SimStream(n_markets=20_000, seed=3)
    out = {"deltas": [], "resyncs": []}
    market_state._last_view_resync = float("inf")
    last_ms = 0
    for i, (recv_ms, m) in enumerate(sim.messages(60_000)):
        kalshi_feed.handle_message(kalshi_feed.decode(json.dumps(m)), 0, recv_ms)
        last_ms = recv_ms
        if i and i % 997 == 0:
            if i % 9_970 == 0:
                market_state._last_view_resync = float("-inf")
            out["deltas"].append(norm_payload(market_state.build_delta_payload()))
            market_state._last_view_resync = float("inf")
    out["books"] = {t: norm(b.summary()) for t, b in sorted(market_state.books.items())}
    out["full"] = norm_payload(market_state.build_full_payload())
    out["recent_trades"] = norm(market_state.trade_buffer.recent(50))
    out["db_rows"] = norm(db_rows())
    out["heavy_hitters"] = norm(market_state.heavy_hitters.top(20))
    # prune: nothing is stale at the end of the stream; 11 minutes later everything without a book is
    out["prune_now"] = market_state.prune_stale_markets(now=last_ms)
    out["prune_later"] = market_state.prune_stale_markets(now=last_ms + 11 * 60_000)
    out["after_prune_markets"] = len(market_state.tickers)
    json.dump(out, sys.stdout, sort_keys=True)


if __name__ == "__main__":
    import builtins
    builtins.print = lambda *a, **k: None   # silence [PRUNE] logs; only the JSON goes to stdout
    main()
