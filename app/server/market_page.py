"""
Market page backend: everything the /market/{ticker} tab needs, kept apart from the home-page path.

Three parts:
  MarketClient  one open market tab. Gets its own WebSocket (/ws/market/{ticker}) and its own
                sender task, so the home-page payload (200 markets) is never built for it and a
                slow market tab can't delay anything else. Sends the FULL book (every level; the
                home page only gets 10), the market's quote, and its own trade tape.
  notify()      called by kalshi_feed after every message about a market. Costs one dict lookup
                when nobody watches that market, which is almost always.
  get_info()    market metadata (rules, expiry, category, ...) from Kalshi's public REST API,
                cached for INFO_TTL_S so a tab reload or ten tabs cost one request.
"""
import asyncio
import json
import time
import urllib.parse
import urllib.request

from app.core.latency import now_ms
from app.server import broadcast
from app.core import market_state

# --- Live tabs ---
watchers: dict[str, set] = {}          # ticker -> MarketClients looking at it
MAX_MARKET_TABS = 50                   # refuse more than this many market tabs in total
MIN_PUSH_INTERVAL_S = 0.05             # at most 20 frames/s per tab: a burst becomes one frame
KEEPALIVE_S = 1.0                      # resend the quote at least this often so the age label stays honest
TAPE_ROWS = 50                         # trades sent per frame


def notify(ticker: str) -> None:
    """Wake every tab watching `ticker`. Called from the Kalshi message handlers."""
    w = watchers.get(ticker)
    if w:
        for client in w:
            client.wake.set()


def tab_count() -> int:
    return sum(len(w) for w in watchers.values())


class MarketClient:
    def __init__(self, ws, ticker: str):
        self.ws = ws
        self.ticker = ticker
        self.wake = asyncio.Event()
        self.closed = False
        self._book_key = None      # (id(book), book.ver) of the last book we sent
        self._tape_tail = None     # newest trade dict of the last tape we sent

    def register(self) -> None:
        watchers.setdefault(self.ticker, set()).add(self)
        self.wake.set()            # first frame goes out immediately

    def unregister(self) -> None:
        w = watchers.get(self.ticker)
        if w is not None:
            w.discard(self)
            if not w:
                del watchers[self.ticker]

    def frame(self) -> dict:
        """What this tab needs right now. `book` and `trades` are left out when unchanged."""
        ticker = self.ticker
        m = market_state.tickers.get(ticker)
        book = market_state.books.get(ticker)
        out = {
            "type": "market",
            "ticker": ticker,
            "title": market_state.readable_titles.get(ticker) or market_state.market_titles.get(ticker, ticker),
            "has_book": book is not None,
            "feed": {"connected": market_state.feed["connected"], "last_msg_ms": market_state.feed["last_msg_ms"]},
            "recv_ts": market_state.market_recv_ms.get(ticker),
            "server_ts": now_ms(),
        }
        if m is not None:
            out["quote"] = {
                "price": m.price, "yes_bid": m.yes_bid, "yes_ask": m.yes_ask,
                "yes_bid_size": m.yes_bid_size, "yes_ask_size": m.yes_ask_size,
                "volume": m.volume, "open_interest": m.open_interest, "dollar_volume": m.dollar_volume,
            }
        if book is not None:
            key = (id(book), book.ver)
            if key != self._book_key:
                self._book_key = key
                s = book.summary()
                out["book"] = {**{k: v for k, v in s.items() if k != "ladder"}, "depth": book.depth()}
            ring = market_state.market_trades_for(ticker)
            tail = ring[-1] if ring else None
            if tail is not self._tape_tail:
                self._tape_tail = tail
                rows = list(ring)[-TAPE_ROWS:]
                out["trades"] = [{"ts": t.get("ts") or t.get("recv_ts"), "side": t.get("side"),
                                  "price": t.get("price"), "count": t.get("count")} for t in rows]
        else:
            self._book_key = None
        return out

    async def run(self) -> None:
        """Sender: one frame per wake-up, at most one per MIN_PUSH_INTERVAL_S, never queued."""
        last = 0.0
        try:
            while not self.closed:
                try:
                    await asyncio.wait_for(self.wake.wait(), KEEPALIVE_S)
                except asyncio.TimeoutError:
                    pass
                wait = MIN_PUSH_INTERVAL_S - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)   # events landing meanwhile are covered by this frame
                self.wake.clear()
                last = time.monotonic()
                await asyncio.wait_for(self.ws.send_text(broadcast.encode(self.frame())), broadcast.SEND_TIMEOUT_S)
        except Exception:
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.unregister()
        try:
            await asyncio.wait_for(self.ws.close(), 1.0)
        except Exception:
            pass


# --- Market metadata (Kalshi public REST) ---

INFO_TTL_S = 60.0
INFO_CACHE_MAX = 500
_info_cache: dict[str, tuple[float, dict]] = {}


def _get_json(path: str) -> dict:
    url = f"{market_state.KALSHI_REST}{path}"
    with urllib.request.urlopen(url, timeout=10, context=market_state._ssl_ctx) as resp:
        return json.load(resp)


def _trim_info(market: dict, event: dict | None) -> dict:
    """The fields the info panel shows, and nothing else."""
    event = event or {}
    return {
        "ticker": market.get("ticker"),
        "title": market.get("title"),
        "subtitle": market.get("yes_sub_title"),
        "status": market.get("status"),
        "category": event.get("category"),
        "event_title": event.get("title"),
        "event_subtitle": event.get("sub_title"),
        "open_time": market.get("open_time"),
        "close_time": market.get("close_time"),
        "expected_expiration_time": market.get("expected_expiration_time"),
        "rules_primary": market.get("rules_primary"),
        "rules_secondary": market.get("rules_secondary"),
        "settlement_sources": event.get("settlement_sources") or [],
        "volume_24h": market.get("volume_24h_fp"),
        "legs": market.get("mve_selected_legs") or [],
    }


def _fetch_info(ticker: str) -> dict:
    """Blocking: GET /markets/{ticker}, then its event for the category. The event is best-effort
    (parlay events are not always published), the market is not."""
    q = urllib.parse.quote(ticker, safe="")
    market = _get_json(f"/markets/{q}")["market"]
    event = None
    et = market.get("event_ticker")
    if et:
        try:
            event = _get_json(f"/events/{urllib.parse.quote(et, safe='')}").get("event")
        except Exception:
            event = None
    return _trim_info(market, event)


async def get_info(ticker: str) -> dict:
    now = time.monotonic()
    hit = _info_cache.get(ticker)
    if hit is not None and now - hit[0] < INFO_TTL_S:
        return hit[1]
    info = await asyncio.to_thread(_fetch_info, ticker)
    if len(_info_cache) >= INFO_CACHE_MAX:
        for t in [t for t, (at, _) in _info_cache.items() if now - at >= INFO_TTL_S] or list(_info_cache)[:50]:
            _info_cache.pop(t, None)
    _info_cache[ticker] = (now, info)
    return info
