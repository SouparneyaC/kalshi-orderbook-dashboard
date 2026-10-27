"""
OrderBook: In-memory representation of a Kalshi prediction market order book.

Kalshi only sends BIDS for both sides of a binary market; asks are derived:
    YES ASK at price X  =  NO BID at price (1 - X)
    NO ASK at price X   =  YES BID at price (1 - X)

How the data is laid out:

  Prices are integers in TICKS of $0.0001, because Kalshi prices have 4 decimals
  (27% of real trades are below a whole cent). "0.3900" → 3900.
  Quantities are integers in HUNDREDTHS of a contract. "4835.32" → 483532.
  Integers are exact, like Decimal, but they are plain machine numbers, not objects
  to allocate, follow and compare digit by digit.

  Each side is two parallel arrays kept in ascending price order:
      px  = array('q', [3800, 3850, 3900])    ← prices, packed together in memory
      qty = array('q', [ 500,  120, 4300])    ← quantity at the same index
  array('q') stores raw 8-byte integers back to back (like a C++ std::vector<int64_t>),
  so walking a side reads consecutive memory: the cheap, sequential pattern from
  cpumemory §3.3.2, instead of hopping between hash-table slots and Decimal objects.

  Because the arrays are sorted, the best bid is simply the LAST element: no search.
  Running totals are updated on every change, and summary() is cached until the
  book changes, so readers never rescan the book.
"""

from array import array
from bisect import bisect_left
from decimal import Decimal

ONE_DOLLAR = 10_000     # ticks per $1.00
NEAR_TICKS = 500        # "near the top of the book" = within $0.05 of the best bid


def to_ticks(price: str) -> int:
    """'0.3900' → 3900. float() then round() is exact for 4-decimal prices."""
    return round(float(price) * ONE_DOLLAR)


def to_hundredths(qty: str) -> int:
    """'4835.32' → 483532."""
    return round(float(qty) * 100)


def _fixed(n: int, places: int) -> str:
    """Integer in units of 10^-places → text, exactly. _fixed(3900, 4) → '0.3900'."""
    sign = "-" if n < 0 else ""
    whole, frac = divmod(abs(n), 10 ** places)
    return f"{sign}{whole}.{frac:0{places}d}"


def _ratio(num: int, den: int, places: int) -> str:
    """num/den rounded half-to-even to `places` decimals, exactly (what Decimal formatting did)."""
    scale = 10 ** places
    q, r = divmod(num * scale, den)
    if r * 2 > den or (r * 2 == den and q % 2):
        q += 1
    return _fixed(q, places)


# Integer → text. A C-level float format is 2x faster than _fixed() and still exact here:
# the double nearest to n/10000 always rounds back to the right 4-decimal text
# (checked against _fixed() for 600k values up to 10^12).
def px_str(ticks: int) -> str:
    return f"{ticks / ONE_DOLLAR:.4f}"


def qty_str(hundredths: int) -> str:
    return f"{hundredths / 100:.2f}"


class _Side:
    """One side of the book (YES bids or NO bids): sorted prices + matching quantities."""

    __slots__ = ("px", "qty", "total")

    def __init__(self):
        self.px = array("q")
        self.qty = array("q")
        self.total = 0          # sum of qty, kept up to date on every change

    def load(self, pairs):
        """pairs: iterable of (price_str, qty_str). Replaces the whole side."""
        levels = {}
        for price, qty in pairs:
            levels[to_ticks(price)] = to_hundredths(qty)   # later duplicates win, like the old dict
        prices = sorted(levels)
        self.px = array("q", prices)
        self.qty = array("q", [levels[p] for p in prices])
        self.total = sum(self.qty)

    def add(self, p: int, d: int):
        """Kalshi deltas are ADDITIVE: quantity at price p changes by d. ≤ 0 removes the level."""
        px = self.px
        i = bisect_left(px, p)                  # binary search: ~5 probes for ~20 levels
        if i < len(px) and px[i] == p:
            new = self.qty[i] + d
            if new <= 0:
                self.total -= self.qty[i]
                del px[i]                       # shifts the tail left: a short sequential memmove
                del self.qty[i]
            else:
                self.qty[i] = new
                self.total += d
        elif d > 0:
            px.insert(i, p)
            self.qty.insert(i, d)
            self.total += d

    def best(self):
        """(price, qty) of the highest bid, or None. The arrays are sorted, so it is the last one."""
        if not self.px:
            return None
        return self.px[-1], self.qty[-1]

    def near(self, width: int) -> int:
        """Total quantity within `width` ticks of the best bid: one binary search + one short slice."""
        if not self.px:
            return 0
        i = bisect_left(self.px, self.px[-1] - width)
        return sum(self.qty[i:])

    def top(self, n: int):
        """Up to n levels, best (highest) first."""
        k = len(self.px)
        return [(self.px[i], self.qty[i]) for i in range(k - 1, max(k - 1 - n, -1), -1)]

    def as_dict(self) -> dict:
        return {Decimal(px_str(p)): Decimal(qty_str(q)) for p, q in zip(self.px, self.qty)}


class OrderBook:
    """
    Holds the state of a single Kalshi market's order book.

    yes: the YES bids (people willing to BUY YES)
    no:  the NO bids  (people willing to BUY NO)
    """

    __slots__ = ("ticker", "yes", "no", "_cached", "_depth", "ver")

    def __init__(self, ticker: str = ""):
        self.ticker = ticker
        self.yes = _Side()
        self.no = _Side()
        self._cached = None     # last summary(); set back to None by any change
        self._depth = None      # last depth(); same rule
        self.ver = 0            # +1 on every change, so a reader can tell "same book as last time"

    # --- Building / updating the book ---

    def load_snapshot(self, snapshot_msg: dict, ticker: str = ""):
        """
        Replace the book with a snapshot. Works with both:
        - REST API response: {"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}
        - WebSocket snapshot msg: {"yes_dollars_fp": [...], "no_dollars_fp": [...]}
        """
        if ticker:
            self.ticker = ticker
        ob = snapshot_msg["orderbook_fp"] if "orderbook_fp" in snapshot_msg else snapshot_msg
        self.yes.load(ob.get("yes_dollars") or ob.get("yes_dollars_fp", []))
        self.no.load(ob.get("no_dollars") or ob.get("no_dollars_fp", []))
        self._changed()

    def apply_delta(self, delta_msg: dict):
        """
        Apply one incremental update:
            {"price_dollars": "0.9600", "delta_fp": "-54.00", "side": "yes"}
        delta_fp is how much the quantity CHANGED (positive = added, negative = removed).
        Text is turned into integer ticks/hundredths exactly once, here.
        """
        side = self.yes if delta_msg["side"] == "yes" else self.no
        side.add(to_ticks(delta_msg["price_dollars"]), to_hundredths(delta_msg["delta_fp"]))
        self._changed()

    def clear(self):
        """Wipe the book (used when switching to a new market)."""
        self.ticker = ""
        self.yes = _Side()
        self.no = _Side()
        self._changed()

    def _changed(self):
        self._cached = None
        self._depth = None
        self.ver += 1

    def level_count(self) -> int:
        return len(self.yes.px) + len(self.no.px)

    # Read-only views in the old shape (price → qty as Decimal), for scripts and debugging.
    @property
    def yes_bids(self) -> dict:
        return self.yes.as_dict()

    @property
    def no_bids(self) -> dict:
        return self.no.as_dict()

    # --- Queries (Decimal results, for scripts; the hot path uses summary()) ---

    @staticmethod
    def _dec(level):
        return None if level is None else (Decimal(px_str(level[0])), Decimal(qty_str(level[1])))

    @staticmethod
    def _ask(level):
        return None if level is None else (ONE_DOLLAR - level[0], level[1])

    def best_yes_bid(self) -> tuple[Decimal, Decimal] | None:
        """Highest price someone will pay for YES. Returns (price, qty) or None."""
        return self._dec(self.yes.best())

    def best_no_bid(self) -> tuple[Decimal, Decimal] | None:
        """Highest price someone will pay for NO."""
        return self._dec(self.no.best())

    def best_yes_ask(self) -> tuple[Decimal, Decimal] | None:
        """Cheapest price to BUY YES = $1 − best NO bid."""
        return self._dec(self._ask(self.no.best()))

    def best_no_ask(self) -> tuple[Decimal, Decimal] | None:
        """Cheapest price to BUY NO = $1 − best YES bid."""
        return self._dec(self._ask(self.yes.best()))

    def yes_spread(self) -> Decimal | None:
        bid, no = self.yes.best(), self.no.best()
        return None if bid is None or no is None else Decimal(px_str(ONE_DOLLAR - no[0] - bid[0]))

    def no_spread(self) -> Decimal | None:
        bid, yes = self.no.best(), self.yes.best()
        return None if bid is None or yes is None else Decimal(px_str(ONE_DOLLAR - yes[0] - bid[0]))

    def implied_probability(self) -> Decimal | None:
        """Midpoint of best YES bid and best YES ask."""
        bid, no = self.yes.best(), self.no.best()
        if bid is None or no is None:
            return None
        return Decimal(bid[0] + ONE_DOLLAR - no[0]) / 2 / ONE_DOLLAR

    def yes_depth(self, within: Decimal | None = None) -> Decimal:
        q = self.yes.total if within is None else self.yes.near(to_ticks(str(within)))
        return Decimal(qty_str(q))

    def no_depth(self, within: Decimal | None = None) -> Decimal:
        q = self.no.total if within is None else self.no.near(to_ticks(str(within)))
        return Decimal(qty_str(q))

    def depth_imbalance(self, within: Decimal | None = None) -> Decimal | None:
        """Share of resting volume on the YES side. >0.5 means more YES pressure."""
        y, n = self.yes_depth(within), self.no_depth(within)
        return None if y + n == 0 else y / (y + n)

    def ladder(self, levels: int = 10) -> dict:
        """
        Top price levels in YES terms, for the dashboard's depth ladder.
        bids: YES bids, best (highest) first.
        asks: YES asks implied from NO bids (ask = 1 − NO bid), best (lowest) first.
        No sorting: the arrays are already in price order, we just read the top 10.
        """
        return {
            "bids": [[px_str(p), qty_str(q)] for p, q in self.yes.top(levels)],
            "asks": [[px_str(ONE_DOLLAR - p), qty_str(q)] for p, q in self.no.top(levels)],
        }

    def depth(self) -> dict:
        """
        EVERY price level in YES terms, for the market page's full ladder and depth chart.
        ladder() above stops at 10 levels so the home-page payload stays small; this one is only
        sent to a tab that opened this market. Same shape as ladder(): best price first on each side.
        Cached until the book changes, so several tabs on one market share one build.
        """
        d = self._depth
        if d is None:
            d = self._depth = self.ladder(len(self.yes.px) + len(self.no.px))
        return d

    def summary(self) -> dict:
        """
        Every number the dashboard shows for this book, as text.
        Computed once per change: if nothing changed since the last call, the same dict is returned.
        """
        c = self._cached
        if c is not None and c["ticker"] == self.ticker:
            return c

        bid = self.yes.best()
        no = self.no.best()
        ask = self._ask(no)
        y_tot, n_tot = self.yes.total, self.no.total
        y_near, n_near = self.yes.near(NEAR_TICKS), self.no.near(NEAR_TICKS)
        both = bid is not None and ask is not None

        c = self._cached = {
            "ticker": self.ticker,
            "best_yes_bid": px_str(bid[0]) if bid else None,
            "best_yes_bid_qty": qty_str(bid[1]) if bid else None,
            "best_yes_ask": px_str(ask[0]) if ask else None,
            "best_yes_ask_qty": qty_str(ask[1]) if ask else None,
            "yes_spread": px_str(ask[0] - bid[0]) if both else None,
            # (bid + ask) / 2 as a percentage with 1 decimal = (bid + ask) / 20 in tenths of a percent
            "implied_prob": _ratio(bid[0] + ask[0], 200, 1) + "%" if both else None,
            "yes_depth_total": qty_str(y_tot),
            "no_depth_total": qty_str(n_tot),
            "yes_depth_near": qty_str(y_near),
            "no_depth_near": qty_str(n_near),
            "depth_imbalance": _ratio(y_tot, y_tot + n_tot, 3) if y_tot + n_tot else None,
            "depth_imbalance_near": _ratio(y_near, y_near + n_near, 3) if y_near + n_near else None,
            "yes_levels": len(self.yes.px),
            "no_levels": len(self.no.px),
            "ladder": self.ladder(),
        }
        return c

    def print_summary(self):
        """Pretty-print the order book summary to terminal."""
        s = self.summary()
        print(f"\n{'=' * 55}")
        print(f"  Order Book: {s['ticker']}")
        print(f"{'=' * 55}")
        print(f"  Best YES Bid:    ${s['best_yes_bid']}  ({s['best_yes_bid_qty']} contracts)")
        print(f"  Best YES Ask:    ${s['best_yes_ask']}  ({s['best_yes_ask_qty']} contracts)")
        print(f"  Spread:          ${s['yes_spread']}")
        print(f"  Implied Prob:    {s['implied_prob']}")
        print(f"  {'─' * 50}")
        print(f"  Book Depth (all levels):")
        print(f"    YES: {s['yes_depth_total']} contracts ({s['yes_levels']} levels)")
        print(f"    NO:  {s['no_depth_total']} contracts ({s['no_levels']} levels)")
        print(f"    Imbalance: {s['depth_imbalance']} (>0.5 = more YES pressure)")
        print(f"  Book Depth (within $0.05 of best bid):")
        print(f"    YES: {s['yes_depth_near']} contracts")
        print(f"    NO:  {s['no_depth_near']} contracts")
        print(f"    Imbalance: {s['depth_imbalance_near']}")
        print(f"{'=' * 55}")
