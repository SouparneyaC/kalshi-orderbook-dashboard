"""
Market state: all mutable data structures for tracked markets.

Owns: tickers, market_titles, books, dirty_markets, market_recv_ms, market_venue_ms,
      trade_buffer, heavy_hitters, concentration, distinct_markets, stats, feed.
"""
import asyncio
import os
import heapq
import json
import ssl
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from itertools import islice
from operator import itemgetter

import certifi

from latency import LatencyMonitor, now_ms, perf_ns, ns_to_ms
from book_seq import BookSeqTracker
from orderbook import OrderBook
from streaming import MisraGries, SecondMomentEstimator, DistinctCounter, WindowedTradeBuffer

# --- Configuration ---
# Book-count experiment: BOOK_COUNT / HEAVY_HITTER_K env vars override the defaults (25 / 60).
# The Misra-Gries sketch can only nominate markets it holds counters for, so K must grow with N.
MAX_ORDERBOOK_MARKETS = int(os.environ.get("BOOK_COUNT", "25"))
# The Misra-Gries sketch now only feeds the Heavy Hitters panel (top 20), so it stays small.
HEAVY_HITTER_K = int(os.environ.get("HEAVY_HITTER_K", "60"))
MARKET_TTL_MS = 10 * 60 * 1000
MAX_TRACKED_MARKETS = 300_000
PAYLOAD_MAX_MARKETS = 200

# --- Shared state ---

class Market:
    """The hot fields of one market, copied out of Kalshi's ticker message.

    (Drepper §6.2.1, hot/cold split.) We used to keep Kalshi's whole message
    dict (~2 KB, ~20 fields, every number as text) as the market's state, and re-parse
    dollar_volume from text every time we ranked markets. Now each market is one small fixed
    record (__slots__: no per-object dict) holding only what the dashboard uses, and the number
    we sort by (dv) is parsed once, when the message arrives. The record is updated in place,
    so a ticker message allocates nothing that we keep.
    """
    __slots__ = ("price", "yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size",
                 "volume", "open_interest", "dollar_volume", "dv")


tickers: dict[str, Market] = {}
trade_buffer = WindowedTradeBuffer(maxlen=500)
heavy_hitters = MisraGries(k=HEAVY_HITTER_K)
# Exact recent dollars traded per market, faded like the sketch (x0.85 every 30 s). This picks the
# order books. A Misra-Gries sketch with K counters only reliably finds markets above ~1/(K+1) of the
# volume (about the top 50 at K=400); live, only 41 of its 200 picks were in the real top 200 by
# dollars. We already keep a record per market, so an exact counter is both correct and O(1) per trade.
recent_dollars: dict[str, float] = {}


def add_recent_dollars(ticker: str, dollars: float) -> None:
    recent_dollars[ticker] = recent_dollars.get(ticker, 0.0) + dollars


def top_recent_dollars(n: int) -> list[tuple[str, float]]:
    return heapq.nlargest(n, recent_dollars.items(), key=itemgetter(1))


# Books reserved for the top of the dashboard table (top markets by lifetime $ volume). Without
# this, books only went to the most active markets right now, and on 2026-10-25 only 3 of the top 50
# rows traders see first had a book. Default: a quarter of the books (50 of 200).
TOP_LIST_BOOKS = min(MAX_ORDERBOOK_MARKETS,
                     int(os.environ.get("TOP_LIST_BOOKS", str(MAX_ORDERBOOK_MARKETS // 4))))


def book_candidates(n: int) -> list[str]:
    """Markets that should have an order book: the top TOP_LIST_BOOKS rows of the table, then the
    most recent dollars traded for the rest."""
    chosen = [t for t, _ in heapq.nlargest(min(TOP_LIST_BOOKS, n), tickers.items(), key=_by_dv)]
    picked = set(chosen)
    for t, _ in heapq.nlargest(n, recent_dollars.items(), key=itemgetter(1)):
        if len(chosen) >= n:
            break
        if t not in picked:
            chosen.append(t)
            picked.add(t)
    return chosen
concentration = SecondMomentEstimator(num_estimators=8)
# Per-market trade history for the market page: the last MARKET_TRADES_MAX trades of each booked
# market (trade_buffer holds only the last 500 across ALL markets, so a quiet market would show
# almost nothing). Entries are the same dicts trade_buffer holds, so a trade is stored once.
MARKET_TRADES_MAX = 100
market_trades: dict[str, deque] = {}
distinct_markets = DistinctCounter(num_hashes=16)
books: dict[str, OrderBook] = {}
book_sub_sid: int | None = None
book_seq = BookSeqTracker()   # seq-gap detection on the book subscription (book_seq.py)
market_titles: dict[str, str] = {}
dirty_markets: set[str] = set()
# Ordered oldest → newest message (mark_market moves a market to the end), so pruning only
# has to look at the front.
market_recv_ms: OrderedDict[str, int] = OrderedDict()
market_venue_ms: dict[str, int] = {}

# The markets browsers are shown: top PAYLOAD_MAX_MARKETS by dollar volume (ticker -> dollar volume).
# Deltas only carry changes inside this set, plus markets entering/leaving it.
view: dict[str, float] = {}
VIEW_RESYNC_S = 10.0  # exact full-scan re-rank this often; in between, entries are judged per change
_last_view_resync = 0.0

# Human-readable names from Kalshi's public REST API, fetched only for markets in the view.
KALSHI_REST = "https://external-api.kalshi.com/trade-api/v2"
readable_titles: dict[str, str] = {}
_title_attempts: dict[str, float] = {}  # ticker -> monotonic time of last fetch attempt
TITLE_RETRY_S = 300.0
_ssl_ctx = ssl.create_default_context(cafile=certifi.where())

latency = LatencyMonitor()

feed = {
    "connected": False,
    "last_msg_ms": 0,
    "msgs": 0,
}

stats = {
    "ticker_updates": 0,
    "trade_count": 0,
    "book_deltas": 0,
    "book_snapshots": 0,
    "book_promotions": 0,
    "markets_seen": 0,
    "start_time": 0,
}

# Book-count experiment: event-loop time spent per kind of work, in ns (decode + handling per
# Kalshi message type, book summaries for payloads, payload builds, promotion checks).
cpu_ns: dict[str, int] = {}


def market_trades_for(ticker: str) -> deque:
    """The trade ring of a market, created (seeded from trade_buffer) on first use."""
    ring = market_trades.get(ticker)
    if ring is None:
        ring = market_trades[ticker] = deque((t for t in trade_buffer.trades if t["ticker"] == ticker),
                                             maxlen=MARKET_TRADES_MAX)
    return ring


def record_market_trade(ticker: str, trade: dict) -> None:
    """Called for every trade after it is added to trade_buffer; only booked markets keep a ring."""
    if ticker not in books:
        return
    ring = market_trades.get(ticker)
    if ring is None:
        market_trades_for(ticker)      # seeded from trade_buffer, which already holds `trade`
    else:
        ring.append(trade)


# --- Mark market helper ---

# Built once, not as a new f-string on every message
_VENUE_KEYS = {c: f"venue_to_recv.{c}" for c in ("ticker", "trade", "book")}


def mark_market(ticker: str, msg: dict, channel: str, recv_ms: int):
    """Remember when we last heard about this market, and how far behind Kalshi's stamp we got it."""
    market_recv_ms[ticker] = recv_ms
    market_recv_ms.move_to_end(ticker)
    venue_ms = msg.get("ts_ms")
    if venue_ms:
        market_venue_ms[ticker] = venue_ms
        latency.record(_VENUE_KEYS.get(channel) or f"venue_to_recv.{channel}", recv_ms - venue_ms)


def update_ticker(ticker: str, msg: dict):
    """Copy a ticker message's hot fields into the market's record (created on first sight)."""
    m = tickers.get(ticker)
    if m is None:
        m = tickers[ticker] = Market()
    m.price = msg.get("price_dollars")
    m.yes_bid = msg.get("yes_bid_dollars")
    m.yes_ask = msg.get("yes_ask_dollars")
    m.yes_bid_size = msg.get("yes_bid_size_fp")
    m.yes_ask_size = msg.get("yes_ask_size_fp")
    m.volume = msg.get("volume_fp")
    m.open_interest = msg.get("open_interest_fp")
    raw = msg.get("dollar_volume")
    m.dollar_volume = raw
    try:
        m.dv = float(raw or 0)
    except (TypeError, ValueError):
        m.dv = 0.0


# --- Build payload for browsers ---

def _build_market_entry(ticker: str) -> dict | None:
    """Build one market entry dict from current state. Returns None if ticker has no data."""
    m = tickers.get(ticker)
    if m is None:
        return None
    book = books.get(ticker)
    entry = {
        "ticker": ticker,
        "title": readable_titles.get(ticker) or market_titles.get(ticker, ticker),
        "price": m.price,
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "yes_bid_size": m.yes_bid_size,
        "yes_ask_size": m.yes_ask_size,
        "volume": m.volume,
        "open_interest": m.open_interest,
        "dollar_volume": m.dollar_volume,
        "has_book": book is not None,
        "recv_ts": market_recv_ms.get(ticker),
        "venue_ts": market_venue_ms.get(ticker),
    }
    if book is not None:
        t0 = perf_ns()
        entry["book"] = book.summary()   # cached inside the book until it changes
        cpu_ns["book_summary"] = cpu_ns.get("book_summary", 0) + perf_ns() - t0
    return entry


def _common_analytics() -> dict:
    """Build the analytics fields shared by both full and delta payloads."""
    now = time.time()
    uptime = now - stats["start_time"] if stats["start_time"] else 0
    top_movers = heavy_hitters.top(20)
    conc = concentration.concentration_ratio()
    return {
        "recent_trades": trade_buffer.recent(50),
        "heavy_hitters": [{"ticker": t, "score": round(s, 1)} for t, s in top_movers],
        "concentration": round(conc, 4) if conc is not None else None,
        "distinct_active": distinct_markets.estimate(),
        "stats": {
            "ticker_updates": stats["ticker_updates"],
            "trade_count": stats["trade_count"],
            "book_deltas": stats["book_deltas"],
            "markets_tracked": len(tickers),
            "books_active": len(books),
            "uptime_s": round(uptime),
        },
        "feed": {
            "venue": "kalshi",
            "connected": feed["connected"],
            "last_msg_ms": feed["last_msg_ms"],
            "msgs": feed["msgs"],
        },
        "latency": latency.snapshot(),
    }


def build_full_payload() -> dict:
    """Full state for a newly connected browser: the top PAYLOAD_MAX_MARKETS markets by dollar volume,
    plus every market that has an order book (traders need to see all books).

    Runs on the event loop, which owns all market state, so nothing
    can change underneath it (it used to run in a thread and could crash mid-iteration). And it
    builds entries only for the markets it sends: it used to build one for every tracked market
    (~90k, including book summaries), sort them all, and throw away all but 200.
    """
    # nlargest gives exactly sorted(..., reverse=True)[:N] (same tie order) without sorting everything
    top = [t for t, _ in heapq.nlargest(PAYLOAD_MAX_MARKETS, tickers.items(), key=_by_dv)]
    shown = set(top)
    top += [t for t in books if t in tickers and t not in shown]   # every booked market too
    market_list = [e for e in (_build_market_entry(t) for t in top) if e is not None]

    payload = {
        "type": "full_state",
        "markets": market_list,
        "market_count": len(tickers),
        "markets_sent": len(market_list),
    }
    payload.update(_common_analytics())
    return payload


def _dollar_volume(ticker: str) -> float:
    m = tickers.get(ticker)
    return m.dv if m is not None else 0.0


def _by_dv(item: tuple[str, Market]) -> float:
    return item[1].dv


def _resync_view() -> tuple[set[str], set[str]]:
    """Exact re-rank over every market. Returns (added, removed).

    Reads one pre-parsed float per market and keeps only the top N with a heap
    (heapq.nlargest = sorted(..., reverse=True)[:N], same tie order) instead of float()-parsing
    text for every market and fully sorting ~90k of them.
    """
    top = heapq.nlargest(PAYLOAD_MAX_MARKETS, tickers.items(), key=_by_dv)
    new_view = {t: m.dv for t, m in top}
    for t in books:                     # booked markets are always shown, whatever their $ volume
        m = tickers.get(t)
        if m is not None:
            new_view[t] = m.dv
    added = new_view.keys() - view.keys()
    removed = view.keys() - new_view.keys()
    view.clear()
    view.update(new_view)
    return set(added), set(removed)


def build_delta_payload() -> dict:
    """Fast path: changes to the top-N view only, plus markets entering or leaving it."""
    global _last_view_resync
    changed_tickers = set(dirty_markets)
    dirty_markets.clear()

    added: set[str] = set()
    removed: set[str] = set()
    now = time.monotonic()
    if now - _last_view_resync >= VIEW_RESYNC_S:
        _last_view_resync = now
        added, removed = _resync_view()

    for t in [t for t in view if t not in tickers]:  # pruned or settled
        del view[t]
        removed.add(t)

    threshold = min(view.values()) if len(view) >= PAYLOAD_MAX_MARKETS else float("-inf")
    for t in changed_tickers:
        m = tickers.get(t)
        if m is None:
            continue
        dv = m.dv
        if t in view:
            view[t] = dv
        elif dv > threshold:
            view[t] = dv
            added.add(t)
    for t in books:                     # pin every booked market into the view (after ranking)
        if t not in view:
            m = tickers.get(t)
            if m is not None:
                view[t] = m.dv
                added.add(t)
    if len(view) > PAYLOAD_MAX_MARKETS:
        # Same eviction as before (lowest $ volume first, same tie order); booked markets are exempt.
        for t, _ in heapq.nsmallest(len(view) - PAYLOAD_MAX_MARKETS, view.items(), key=itemgetter(1)):
            if t not in books:
                del view[t]
                removed.add(t)
    added &= view.keys()
    removed -= view.keys()

    changed_markets = []
    for ticker in (changed_tickers & view.keys()) | added:
        entry = _build_market_entry(ticker)
        if entry is not None:
            changed_markets.append(entry)

    payload = {
        "type": "delta",
        "markets_changed": changed_markets,
        "markets_removed": sorted(removed),
        "market_count": len(tickers),
        "markets_sent": len(view),
    }
    payload.update(_common_analytics())
    return payload


# --- Periodic loops ---

async def analytics_decay_loop():
    """Decay streaming algorithm counters every 30s for smooth fading."""
    while True:
        await asyncio.sleep(30)
        heavy_hitters.decay(factor=0.85, threshold=1.0)
        for t in [t for t, v in recent_dollars.items() if v * 0.85 < 1.0]:
            del recent_dollars[t]
        for t in recent_dollars:
            recent_dollars[t] *= 0.85
        concentration.decay(factor=0.85)
        print(f"  [DECAY] Streaming counters decayed (heavy_hitters: {len(heavy_hitters.counters)} items)")


def prune_stale_markets(now: int | None = None) -> int:
    """Forget markets with no message for MARKET_TTL_MS, unless we hold their order book.
    Returns how many entries were forgotten."""
    cutoff = (now if now is not None else now_ms()) - MARKET_TTL_MS
    # market_recv_ms is ordered oldest-first, so the stale markets are all at the
    # front. Walk from the front and stop at the first fresh one: we touch only what we remove
    # (plus the few stale markets kept because we hold their book), not all ~90k markets.
    stale = []
    for t, ms in market_recv_ms.items():
        if ms >= cutoff:
            break
        if t not in books:
            stale.append(t)
    for t in stale:
        tickers.pop(t, None)
        market_titles.pop(t, None)
        market_recv_ms.pop(t, None)
        market_venue_ms.pop(t, None)
        dirty_markets.discard(t)
        readable_titles.pop(t, None)
        _title_attempts.pop(t, None)

    # Clean orphaned titles: lifecycle writes titles for markets that never get
    # a ticker/trade message, so they never appear in market_recv_ms.
    # (A plain loop: the old version built two ~90k-entry sets every minute just to subtract them.)
    orphan_titles = [t for t in market_titles if t not in market_recv_ms]
    for t in orphan_titles:
        market_titles.pop(t, None)

    # Hard cap: if we still track too many markets, drop the oldest by recv time
    if len(tickers) > MAX_TRACKED_MARKETS:
        to_drop = len(tickers) - MAX_TRACKED_MARKETS
        for t, _ in list(islice(market_recv_ms.items(), to_drop)):   # oldest first, no sort needed
            if t not in books:
                tickers.pop(t, None)
                market_titles.pop(t, None)
                market_recv_ms.pop(t, None)
                market_venue_ms.pop(t, None)
                dirty_markets.discard(t)

    for t in [t for t in market_trades if t not in books]:   # book demoted or market settled
        del market_trades[t]

    pruned = len(stale) + len(orphan_titles)
    if pruned:
        print(f"  [PRUNE] Forgot {len(stale)} stale + {len(orphan_titles)} orphan titles ({len(tickers)} tickers, {len(market_titles)} titles left)")
    return pruned


async def prune_stale_markets_loop():
    while True:
        await asyncio.sleep(60)
        prune_stale_markets()


def _readable_label(m: dict) -> str | None:
    """'BTC price up in next 15 mins? · Target Price: $81,304.14'. Subtitle only when it adds something."""
    title = (m.get("title") or "").strip()
    sub = (m.get("yes_sub_title") or "").strip()
    if not title:
        return sub or None
    if sub and sub.lower() not in ("yes", "no") and sub.lower() not in title.lower() and title.lower() not in sub.lower():
        return f"{title} · {sub}"
    return title


def _fetch_titles(batch: list[str]) -> dict[str, str]:
    """Blocking: one public GET /markets?tickers=... for up to 100 tickers."""
    url = f"{KALSHI_REST}/markets?" + urllib.parse.urlencode({"tickers": ",".join(batch), "limit": len(batch)})
    with urllib.request.urlopen(url, timeout=10, context=_ssl_ctx) as resp:
        markets = json.load(resp).get("markets", [])
    out = {}
    for m in markets:
        label = _readable_label(m)
        if m.get("ticker") and label:
            out[m["ticker"]] = label
    return out


async def title_fetch_loop():
    """Look up readable titles for markets on screen that don't have one yet."""
    while True:
        await asyncio.sleep(2)
        now = time.monotonic()
        missing = [t for t in view
                   if t not in readable_titles and now - _title_attempts.get(t, 0) >= TITLE_RETRY_S]
        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            for t in batch:
                _title_attempts[t] = now
            try:
                found = await asyncio.to_thread(_fetch_titles, batch)
            except Exception as e:
                print(f"  [TITLES] Fetch failed for {len(batch)} markets: {e}")
                continue
            for t, label in found.items():
                if t in tickers:
                    readable_titles[t] = label
                    dirty_markets.add(t)  # resend the row with its new title
            if found:
                print(f"  [TITLES] Named {len(found)} of {len(batch)} markets")


async def latency_log_loop():
    """Print key latency percentiles every 60s."""
    keys = ["venue_to_recv.trade", "venue_to_recv.ticker", "recv_to_send",
            "send_to_render", "loop_lag", "db_flush", "gc_pause"]
    while True:
        await asyncio.sleep(60)
        stages = latency.snapshot(force=True)["stages"]
        parts = []
        for k in keys:
            s = stages.get(k)
            if s:
                parts.append(f"{k} p50={s['p50']} p99={s['p99']}")
        if parts:
            print("  [LATENCY ms] " + " | ".join(parts))
