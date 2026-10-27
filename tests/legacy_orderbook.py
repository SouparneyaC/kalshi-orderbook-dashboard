"""
OrderBook: In-memory representation of a Kalshi prediction market order book.

This class takes Kalshi's raw API response and turns it into a queryable
data structure. It handles the binary market quirk where only bids are
returned and asks must be derived.

Data structure choice: sorted dict (SortedDict-like behavior via regular dict).
We use a plain dict keyed by Decimal price → Decimal quantity. Since Kalshi
returns data sorted and we rebuild from snapshots, we maintain order by
rebuilding when needed. For the delta phase (Phase 3), we'll add/remove
individual levels.

Why Decimal instead of float:
  float(0.1) + float(0.2) = 0.30000000000000004
  Decimal("0.1") + Decimal("0.2") = Decimal("0.3")
  Financial data demands exact arithmetic. Strings in, Decimals internally.
"""

# ---------------------------------------------------------------------------
# BEGINNER'S GUIDE TO READING THIS FILE
# ---------------------------------------------------------------------------
# A few Python ideas show up over and over below. Learn these and the rest
# becomes much easier to read:
#
# 1. DICTIONARY ("dict"): a lookup table of key → value pairs.
#       prices = {0.39: 4835, 0.38: 1945}
#    Here the key is a price and the value is how many contracts are
#    waiting at that price. prices[0.39] gives back 4835.
#
# 2. "self": inside a class, "self" means "this particular order book".
#    self.yes_bids is "the YES bids belonging to this order book".
#
# 3. "def name(...)": defines a function (a reusable recipe). "return"
#    is the answer the recipe hands back to whoever called it.
#
# 4. "None": Python's way of saying "nothing / no answer". We return None
#    when a question can't be answered (e.g. best bid of an empty book).
#
# 5. Type hints like "-> Decimal | None" are just labels saying what the
#    function returns ("a Decimal, or None"). Python doesn't enforce them;
#    they're notes for humans.
#
# 6. Tuple: a small fixed bundle of values, written (a, b). We often return
#    (price, quantity) together as a tuple. tuple[0] is the first item
#    (price), tuple[1] the second (quantity).
# ---------------------------------------------------------------------------

# Bring in tools from Python's built-in library:
# - Decimal: exact decimal numbers (no float rounding errors with money).
# - dataclass / field: a shortcut that writes the boring setup code of a
#   class for us (see below).
from decimal import Decimal
from dataclasses import dataclass, field


# "@dataclass" is a decorator: it tells Python "auto-generate the setup code
# for this class from the variables listed at the top". Without it we'd have
# to write an __init__ function by hand.
@dataclass
class OrderBook:
    """
    Holds the state of a single Kalshi market's order book.

    Attributes:
        ticker: The market ticker (e.g., "KXBTC15M-26OCT091715-45")
        yes_bids: dict mapping price (Decimal) → quantity (Decimal)
                  These are people willing to BUY YES contracts.
        no_bids:  dict mapping price (Decimal) → quantity (Decimal)
                  These are people willing to BUY NO contracts.

    All asks are derived — never stored directly — because in binary
    prediction markets:
        YES ASK at price X  =  NO BID at price (1 - X)
        NO ASK at price X   =  YES BID at price (1 - X)
    """
    # The three pieces of data every OrderBook holds.
    # ticker starts as an empty string "" until we know which market it is.
    ticker: str = ""
    # "field(default_factory=dict)" means "give each new OrderBook its own
    # brand-new empty dictionary". (If we wrote "= {}" instead, every
    # OrderBook would accidentally share ONE dictionary — a classic Python
    # trap, so dataclasses forbid it.)
    # MEM-01 · cpumemory §3.3.2 p.20-24 (random walk), §6.2.1 p.49-55 (layout), §7.3 p.82-85 (small allocs)
    #   NOW: dict[Decimal, Decimal] — each level is a hash slot pointing at two ~104 B Decimal objects
    #        scattered on the heap. ~295 B/level for ~8 B of data (~37x); every scan is a pointer-chasing walk.
    #   SHOULD BE: two fixed arrays indexed by price in cents (Kalshi prices are 1-99¢), ints for qty:
    #        self.yes = [0] * 100   # yes[39] = resting qty at 39¢ (qty in hundredths if _fp has decimals)
    #        self.no  = [0] * 100
    #        One contiguous block per side → sequential walks, ~800 B/book in C++, all 25 books fit in L1d.
    yes_bids: dict = field(default_factory=dict)
    no_bids: dict = field(default_factory=dict)

    # =======================================================================
    # PART 1: PUTTING DATA INTO THE BOOK
    # =======================================================================

    def load_snapshot(self, snapshot_msg: dict, ticker: str = ""):
        """
        Populate the order book from a snapshot.

        Works with both:
        - REST API response: {"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}
        - WebSocket snapshot msg: {"yes_dollars": [...], "no_dollars": [...]}

        This completely replaces any existing state.

        Args:
            snapshot_msg: The snapshot data (either REST response or WS msg body)
            ticker: The market ticker string
        """
        # If a ticker was passed in (i.e. it's not the empty string ""),
        # remember it. An empty string counts as "false" in an if-check,
        # so this line only runs when we actually got a ticker.
        if ticker:
            self.ticker = ticker

        # Handle three possible shapes:
        # 1. REST API:      {"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}
        # 2. WS snapshot:   {"yes_dollars_fp": [...], "no_dollars_fp": [...]}
        # 3. Already unwrapped
        #
        # The REST API wraps the data inside an extra "orderbook_fp" layer,
        # like a letter inside an envelope. If the envelope is there, open it
        # and use what's inside. Otherwise the data is already "unwrapped".
        # Either way, "ob" ends up pointing at the part we care about.
        if "orderbook_fp" in snapshot_msg:
            ob = snapshot_msg["orderbook_fp"]
        else:
            ob = snapshot_msg

        # Try both key naming conventions (REST vs WebSocket)
        #
        # .get("name") looks up a key but returns None (instead of crashing)
        # if the key doesn't exist. "A or B" means "use A if it has something
        # in it, otherwise use B". So: try the REST name first; if missing,
        # try the WebSocket name; if that's missing too, use an empty list [].
        yes_data = ob.get("yes_dollars") or ob.get("yes_dollars_fp", [])
        no_data = ob.get("no_dollars") or ob.get("no_dollars_fp", [])

        # Convert string pairs into Decimal-keyed dict
        # Input: [["0.3900", "4835.32"], ["0.3800", "1945.10"], ...]
        # Output: {Decimal("0.3900"): Decimal("4835.32"), ...}
        #
        # This is a "dict comprehension" — a one-line way to build a
        # dictionary with a loop. Read it as:
        #   "For each [price, qty] pair in yes_data, add an entry
        #    price → qty to a new dictionary (converting both from text
        #    to exact Decimal numbers)."
        # "for price, qty in yes_data" unpacks each two-item pair into two
        # named variables in one go.
        # Because we build a NEW dictionary and assign it, any old data is
        # thrown away — that's what "completely replaces existing state" means.
        self.yes_bids = {
            Decimal(price): Decimal(qty)
            for price, qty in yes_data
        }
        self.no_bids = {
            Decimal(price): Decimal(qty)
            for price, qty in no_data
        }

    def apply_delta(self, delta_msg: dict):
        """
        Apply an incremental update to the order book.

        WebSocket delta format (one level change per message):
        {
            "market_ticker": "KXBTC15M-...",
            "price_dollars": "0.9600",
            "delta_fp": "-54.00",      ← ADDITIVE change, not absolute
            "side": "yes"              ← which side changed
        }

        delta_fp is how much the quantity CHANGED:
        - Positive: contracts were added at this price
        - Negative: contracts were removed from this price
        - If the result is zero or negative, remove the level entirely

        Args:
            delta_msg: The "msg" body of a WebSocket orderbook_delta message.
        """
        # Pull the three facts we need out of the message and convert the
        # text numbers into exact Decimals.
        # MEM-02 · cpumemory §7.3 p.82-85 (every small allocation costs), §6.2.1 (do work once)
        #   NOW: two new Decimal objects per delta, then the book is left "dumb" — every reader
        #        (best bid, depth, ladder) has to rediscover the state by scanning.
        #   SHOULD BE: parse to integer cents once, and update the derived values right here:
        #        p = round(float(delta_msg["price_dollars"]) * 100); d = round(float(delta_msg["delta_fp"]) * 100)
        #        side[p] += d; side_total += d
        #        if d > 0 and p > best: best = p
        #        elif side[p] <= 0 and p == best: best = next nonzero slot below (≤99 sequential steps)
        #        self.dirty = True
        price = Decimal(delta_msg["price_dollars"])
        delta = Decimal(delta_msg["delta_fp"])
        side = delta_msg["side"]

        # Pick which dictionary to change. This is a one-line if/else:
        #   "book = yes_bids if the side is 'yes', otherwise book = no_bids".
        # Note: "book" isn't a copy — it's another name for the SAME
        # dictionary, so changing "book" changes self.yes_bids/no_bids too.
        book = self.yes_bids if side == "yes" else self.no_bids

        # How many contracts are at this price right now?
        # .get(price, 0) returns 0 if this price isn't in the book yet
        # (e.g. the very first order at a brand-new price).
        current = book.get(price, Decimal("0"))
        # Add the change (a negative delta subtracts).
        new_qty = current + delta

        # If nothing is left at this price, delete the price level entirely
        # (.pop removes a key; the "None" means "don't crash if it's already
        # gone"). Otherwise, store the new quantity.
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    def clear(self):
        """Wipe the book — used when switching to a new market."""
        # Reset everything back to empty. .clear() empties a dictionary.
        self.ticker = ""
        self.yes_bids.clear()
        self.no_bids.clear()

    # =======================================================================
    # PART 2: ASKING THE BOOK QUESTIONS
    # =======================================================================

    # --- Core queries ---

    def best_yes_bid(self) -> tuple[Decimal, Decimal] | None:
        """Highest price someone will pay for YES. Returns (price, qty) or None."""
        # An empty dictionary counts as "false", so "not self.yes_bids"
        # means "if there are no YES bids at all". Nothing to report → None.
        if not self.yes_bids:
            return None
        # max() on a dictionary looks at the KEYS (the prices) and returns
        # the largest one — i.e. the highest bid.
        # MEM-03 · cpumemory §3.3.2 Table 3.2 p.24 (random walk L2 miss 50% vs 1.78% sequential)
        #   NOW: max() walks every level through hash-table pointers; summary() ends up calling this
        #        and best_no_bid() ~8 times per book per payload.
        #   SHOULD BE: return the best index kept up to date in apply_delta (MEM-02): O(1), no memory walk.
        price = max(self.yes_bids)
        # Return the price AND how many contracts are waiting there.
        return (price, self.yes_bids[price])

    def best_no_bid(self) -> tuple[Decimal, Decimal] | None:
        """Highest price someone will pay for NO. Returns (price, qty) or None."""
        # Exactly the same logic as best_yes_bid, but for the NO side.
        if not self.no_bids:
            return None
        price = max(self.no_bids)
        return (price, self.no_bids[price])

    def best_yes_ask(self) -> tuple[Decimal, Decimal] | None:
        """
        Cheapest price to BUY a YES contract (implied from NO bids).

        If best NO bid is $0.60, then best YES ask is $0.40.
        Someone willing to sell you YES at $0.40 is the same person
        willing to buy NO at $0.60.
        """
        # Reuse the function above instead of repeating its logic.
        no = self.best_no_bid()
        # If there are no NO bids, there's no implied YES ask either.
        if no is None:
            return None
        # no[0] is the NO bid price, no[1] its quantity.
        # YES ask price = $1.00 minus the NO bid price; same quantity.
        return (Decimal("1.00") - no[0], no[1])

    def best_no_ask(self) -> tuple[Decimal, Decimal] | None:
        """Cheapest price to BUY a NO contract (implied from YES bids)."""
        # Mirror image of best_yes_ask: NO ask = $1.00 minus best YES bid.
        yes = self.best_yes_bid()
        if yes is None:
            return None
        return (Decimal("1.00") - yes[0], yes[1])

    # --- Metrics ---
    # These all follow the same pattern:
    #   1. Ask the simpler functions above for the numbers we need.
    #   2. If any of them came back None (empty book), give up and return None.
    #   3. Otherwise do a bit of arithmetic and return the result.

    def yes_spread(self) -> Decimal | None:
        """Gap between best YES ask and best YES bid. Tighter = more liquid."""
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid is None or ask is None:
            return None
        # [0] grabs just the price out of each (price, qty) pair.
        return ask[0] - bid[0]

    def no_spread(self) -> Decimal | None:
        """Gap between best NO ask and best NO bid."""
        bid = self.best_no_bid()
        ask = self.best_no_ask()
        if bid is None or ask is None:
            return None
        return ask[0] - bid[0]

    def implied_probability(self) -> Decimal | None:
        """
        Market's implied probability that YES happens.
        Midpoint of best YES bid and best YES ask.
        """
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid is None or ask is None:
            return None
        # Average of the two prices = halfway point. E.g. bid 0.38 and
        # ask 0.40 → 0.39, i.e. the market thinks ~39% chance of YES.
        return (bid[0] + ask[0]) / 2

    # MEM-04 · cpumemory §6.2.1 p.49-51 (each pass over the data reloads it; Table 6.2)
    #   NOW: yes_depth/no_depth each do a full pass (plus a max() pass when `within` is set);
    #        summary() and depth_imbalance() call them 8 times → ~8 extra full walks per book.
    #   SHOULD BE: running totals maintained in apply_delta (side_total += d); "near" depth =
    #        sum(side[best-5 : best+1]) — 6 adjacent slots of one array, a single cache line or two.
    def yes_depth(self, within: Decimal | None = None) -> Decimal:
        """
        Total YES bid volume, optionally within a price range of the best bid.

        Args:
            within: If set, only count levels within this dollar amount of
                    the best bid. E.g., within=Decimal("0.05") counts levels
                    from best bid down to best bid - $0.05.
                    If None, counts all levels.
        """
        # "within: ... = None" in the def line means within is OPTIONAL.
        # Call yes_depth() → within is None (count everything).
        # Call yes_depth(Decimal("0.05")) → only count near the top.

        # Case 1: empty book → zero contracts. (We return 0 rather than
        # None here because "no volume" is a perfectly sensible answer.)
        if not self.yes_bids:
            return Decimal("0")

        # Case 2: no range given → add up EVERY quantity in the book.
        # .values() gives just the quantities (ignoring the prices),
        # and sum() adds them together.
        if within is None:
            return sum(self.yes_bids.values())

        # Case 3: only count price levels close to the best bid.
        # First find the best (highest) bid price.
        best = max(self.yes_bids)
        # Then walk through every (price, qty) pair in the book and add up
        # the qty ONLY for prices that are close enough to the best.
        # .items() gives each entry as a (price, qty) pair.
        #
        # "best - price" = how far below the best bid this level sits.
        # Example with best = 0.39 and within = 0.05:
        #   price 0.39 → distance 0.00 → counted
        #   price 0.36 → distance 0.03 → counted
        #   price 0.34 → distance 0.05 → counted (<= includes the edge)
        #   price 0.30 → distance 0.09 → skipped
        #
        # The whole thing reads as plain English:
        #   "sum the qty, for each price/qty in the book, if it's within range"
        return sum(
            qty for price, qty in self.yes_bids.items()
            if best - price <= within
        )

    def no_depth(self, within: Decimal | None = None) -> Decimal:
        """Total NO bid volume, optionally within a price range of the best bid."""
        # Identical logic to yes_depth, just using the NO side.
        if not self.no_bids:
            return Decimal("0")
        if within is None:
            return sum(self.no_bids.values())
        best = max(self.no_bids)
        return sum(
            qty for price, qty in self.no_bids.items()
            if best - price <= within
        )

    def depth_imbalance(self, within: Decimal | None = None) -> Decimal | None:
        """
        Ratio of YES depth to total depth. >0.5 means more YES pressure.

        This is a simple sentiment indicator:
        - 0.7 means 70% of resting volume is on the YES side
        - 0.3 means bears dominate (more NO bids)
        - 0.5 is balanced

        Args:
            within: price range to consider (see yes_depth/no_depth)
        """
        # Get total contracts on each side (passing "within" straight
        # through, so this works for both "all levels" and "near the top").
        y = self.yes_depth(within)
        n = self.no_depth(within)
        total = y + n
        # Guard against dividing by zero, which would crash the program.
        if total == 0:
            return None
        # YES share of the total. E.g. 700 YES / 1000 total = 0.7.
        return y / total

    # =======================================================================
    # PART 3: PACKAGING RESULTS FOR DISPLAY
    # =======================================================================

    def ladder(self, levels: int = 10) -> dict:
        """
        Top price levels in YES terms, for the dashboard's depth ladder.

        bids: YES bids, best (highest) first.
        asks: YES asks implied from NO bids (ask = 1 - NO bid), best (lowest) first.
        Each level is [price, qty] as text.
        """
        # MEM-05 · cpumemory §3.3.2 p.20-24, §7.3 p.82-85
        #   NOW: sorts the whole side (allocating a list of tuples) to keep 10 levels, and for asks
        #        also builds a new Decimal per level (1 - p). Both sides, every summary().
        #   SHOULD BE: walk the price array downward from `best` and stop after 10 nonzero slots;
        #        the array is already in price order, so there is nothing to sort.
        bids = sorted(self.yes_bids.items(), reverse=True)[:levels]
        asks = sorted(((Decimal("1.00") - p, q) for p, q in self.no_bids.items()))[:levels]
        return {
            "bids": [[str(p), str(q)] for p, q in bids],
            "asks": [[str(p), str(q)] for p, q in asks],
        }

    # MEM-06 · cpumemory §6.2.1 p.49-55 (do the work once, keep the working set small)
    #   NOW: ~12-20 full passes per call — yes_spread(), implied_probability(), depth_imbalance()
    #        are each evaluated twice (once for the `is not None` check, once for the value),
    #        and the whole thing reruns for every payload even if the book did not change.
    #   SHOULD BE: if not self.dirty: return self._cached
    #        else compute each value exactly once into locals, build the dict, cache it, dirty = False.
    def summary(self) -> dict:
        """
        Returns all core metrics as a dict — this is what we'll eventually
        push to the frontend via WebSocket.
        """
        # Look these up once and reuse them below.
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        # "Near the top of the book" = within 5 cents of the best bid.
        near = Decimal("0.05")

        # Build one dictionary with every number the dashboard needs.
        # Everything is converted to text with str(...) because the web
        # page receives JSON, and JSON doesn't understand Decimal.
        #
        # The repeated pattern "X if condition else None" is a one-line
        # if/else: "give X, but if the value doesn't exist, give None".
        # e.g. str(bid[0]) if bid else None
        #      → "the bid price as text, or None if there's no bid".
        #
        # f"{value * 100:.1f}%" is an f-string (formatted text):
        #   ":.1f" means "show 1 decimal place", so 0.3912 → "39.1%".
        #   ":.3f" means 3 decimal places, so 0.71234 → "0.712".
        #
        # len(...) counts how many entries a dictionary has — here, how many
        # distinct price levels exist on each side.
        return {
            "ticker": self.ticker,
            "best_yes_bid": str(bid[0]) if bid else None,
            "best_yes_bid_qty": str(bid[1]) if bid else None,
            "best_yes_ask": str(ask[0]) if ask else None,
            "best_yes_ask_qty": str(ask[1]) if ask else None,
            "yes_spread": str(self.yes_spread()) if self.yes_spread() is not None else None,
            "implied_prob": f"{self.implied_probability() * 100:.1f}%" if self.implied_probability() is not None else None,
            "yes_depth_total": str(self.yes_depth()),
            "no_depth_total": str(self.no_depth()),
            "yes_depth_near": str(self.yes_depth(near)),
            "no_depth_near": str(self.no_depth(near)),
            "depth_imbalance": f"{self.depth_imbalance():.3f}" if self.depth_imbalance() is not None else None,
            "depth_imbalance_near": f"{self.depth_imbalance(near):.3f}" if self.depth_imbalance(near) is not None else None,
            "yes_levels": len(self.yes_bids),
            "no_levels": len(self.no_bids),
            "ladder": self.ladder(),
        }

    def print_summary(self):
        """Pretty-print the order book summary to terminal."""
        # Grab the summary dictionary, then print its values in a nice layout.
        # s['ticker'] looks up the "ticker" entry in that dictionary.
        # '=' * 55 repeats the "=" character 55 times to draw a line.
        # "\n" at the start of the first print means "start on a new line".
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
