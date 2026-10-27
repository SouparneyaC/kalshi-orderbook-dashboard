"""
Deterministic, realistic fake Kalshi stream for benchmarks and golden tests.

Shaped after the live server on 2026-10-23 (/latency + /debug/mem):
  - ~98k markets tracked, 25 order books with ~18 levels each
  - message mix: ticker 53.6%, orderbook_delta 37.3%, trade 9.1% (+ a little lifecycle)
  - ~2,870 messages per second
  - prices on Kalshi's 0.0001 grid (27% of real trades are sub-cent), sizes in hundredths

Same seed → same messages, so before/after runs see identical input.
"""
import json
import random

MSGS_PER_SEC = 2870
SERIES = [f"KX{chr(65 + i % 26)}{chr(65 + (i // 26) % 26)}{i % 10}" for i in range(300)]


def _px(ticks: int) -> str:
    return f"{ticks / 10000:.4f}"


def _qty(hundredths: int) -> str:
    return f"{hundredths / 100:.2f}"


class SimStream:
    def __init__(self, n_markets: int = 98_000, n_books: int = 25, seed: int = 42, start_ms: int = 1_792_800_000_000):
        self.rng = random.Random(seed)
        self.start_ms = start_ms
        self.tickers = [f"{SERIES[i % len(SERIES)]}-26OCT{i:06d}" for i in range(n_markets)]
        # Zipf-ish dollar volume: a few huge markets, a long tail
        self.dollar_volume = [int(5e7 / (r + 1) ** 1.1) + self.rng.randint(0, 500) for r in range(n_markets)]
        self.book_tickers = self.tickers[:n_books]
        self.subpenny = {t: (i % 2 == 0) for i, t in enumerate(self.book_tickers)}
        self.levels = {}  # (ticker, side) -> {ticks: hundredths}, the "true" book used to make sane deltas
        self.trade_seq = 0
        self.n_trade_markets = min(5000, n_markets)

    # --- message builders -------------------------------------------------

    def _step(self, t: str) -> int:
        return 1 if self.subpenny[t] else 100

    def snapshot(self, t: str, ts_ms: int) -> dict:
        step = self._step(t)
        mid = self.rng.randint(1500, 8500) // step * step
        yes, no = {}, {}
        for k in range(9):
            yes[mid - step * (1 + k * self.rng.randint(1, 3))] = self.rng.randint(100, 500_000)
            no[10000 - mid - step * (1 + k * self.rng.randint(1, 3))] = self.rng.randint(100, 500_000)
        self.levels[(t, "yes")] = yes
        self.levels[(t, "no")] = no
        return {"type": "orderbook_snapshot", "sid": 4, "seq": 1, "msg": {
            "market_ticker": t, "market_id": f"id-{t}",
            "yes_dollars_fp": [[_px(p), _qty(q)] for p, q in sorted(yes.items())],
            "no_dollars_fp": [[_px(p), _qty(q)] for p, q in sorted(no.items())],
            "ts_ms": ts_ms,
        }}

    def delta(self, ts_ms: int) -> dict:
        t = self.book_tickers[min(int(self.rng.expovariate(0.15)), len(self.book_tickers) - 1)]
        side = "yes" if self.rng.random() < 0.5 else "no"
        lv = self.levels[(t, side)]
        r = self.rng.random()
        if lv and (r < 0.15 or len(lv) > 14):          # remove a whole level
            p = self.rng.choice(list(lv))
            d = -lv.pop(p)
        elif lv and r < 0.6:                            # change an existing level
            p = self.rng.choice(list(lv))
            d = self.rng.randint(-lv[p] + 1, 50_000)
            lv[p] += d
        else:                                           # new level near the top
            step = self._step(t)
            top = max(lv) if lv else 5000
            other = self.levels[(t, "no" if side == "yes" else "yes")]
            ceiling = 10000 - (max(other) if other else 0) - step   # stay below the other side's ask: no crossed book
            p = max(step, min(ceiling, top + step * self.rng.randint(-8, 3)))
            d = self.rng.randint(100, 80_000)
            lv[p] = lv.get(p, 0) + d
        return {"type": "orderbook_delta", "sid": 4, "seq": 0, "msg": {
            "market_ticker": t, "market_id": f"id-{t}",
            "price_dollars": _px(p), "delta_fp": _qty(d), "side": side, "ts_ms": ts_ms,
        }}

    def ticker(self, ts_ms: int) -> dict:
        i = self.rng.randrange(len(self.tickers))
        t = self.tickers[i]
        self.dollar_volume[i] += self.rng.randint(0, 50)
        bid = self.rng.randint(1, 98)
        return {"type": "ticker", "sid": 1, "msg": {
            "market_id": f"id-{t}", "market_ticker": t,
            "price_dollars": _px(bid * 100 + 50), "yes_bid_dollars": _px(bid * 100), "yes_ask_dollars": _px(bid * 100 + 100),
            "yes_bid_size_fp": _qty(self.rng.randint(100, 900_000)), "yes_ask_size_fp": _qty(self.rng.randint(100, 900_000)),
            "last_trade_size_fp": _qty(self.rng.randint(100, 50_000)),
            "volume_fp": _qty(self.dollar_volume[i] * 137), "volume_24h_fp": _qty(self.dollar_volume[i] * 12),
            "open_interest_fp": _qty(self.dollar_volume[i] * 50),
            "dollar_volume": self.dollar_volume[i], "dollar_open_interest": self.dollar_volume[i] // 3,
            "previous_yes_bid_dollars": _px(bid * 100), "previous_yes_ask_dollars": _px(bid * 100 + 100),
            "previous_price_dollars": _px(bid * 100 + 50),
            "ts": ts_ms // 1000, "ts_ms": ts_ms, "time": "2026-10-23T17:00:00.000000Z",
        }}

    def trade(self, ts_ms: int) -> dict:
        self.trade_seq += 1
        t = self.tickers[min(int(self.rng.paretovariate(1.2)) - 1, self.n_trade_markets - 1)]
        yes = self.rng.randint(1, 9999) if self.rng.random() < 0.27 else self.rng.randint(1, 99) * 100
        return {"type": "trade", "sid": 2, "seq": self.trade_seq, "msg": {
            "trade_id": f"{self.trade_seq:08x}-6b1f-4d2a-9c3e-{self.trade_seq:012x}",
            "market_ticker": t, "yes_price_dollars": _px(yes), "no_price_dollars": _px(10000 - yes),
            "count_fp": _qty(self.rng.choice([100, 200, 500, 1000, 2500, 10000, 1, 10])),
            "taker_side": "yes", "taker_outcome_side": "yes" if self.rng.random() < 0.5 else "no",
            "taker_book_side": "bid" if self.rng.random() < 0.5 else "ask",
            "is_block_trade": False, "ts": ts_ms // 1000, "ts_ms": ts_ms,
        }}

    def lifecycle(self, ts_ms: int) -> dict:
        t = self.rng.choice(self.tickers)
        return {"type": "market_lifecycle_v2", "sid": 3, "msg": {
            "market_ticker": t, "event_type": "metadata_updated",
            "additional_metadata": {"title": f"Will {t} resolve YES?", "event_ticker": t.split("-")[0]},
        }}

    # --- the stream -------------------------------------------------------

    def messages(self, n: int):
        """Yields (recv_ms, message dict). Starts with one snapshot per book."""
        for k, t in enumerate(self.book_tickers):
            yield self.start_ms + k, self.snapshot(t, self.start_ms + k - 40)
        for i in range(n):
            recv_ms = self.start_ms + 100 + i * 1000 // MSGS_PER_SEC
            ts_ms = recv_ms - 40
            r = self.rng.random()
            if r < 0.536:
                m = self.ticker(ts_ms)
            elif r < 0.909:
                m = self.delta(ts_ms)
            elif r < 0.9995:
                m = self.trade(ts_ms)
            else:
                m = self.lifecycle(ts_ms)
            yield recv_ms, m

    def raw_messages(self, n: int) -> list[tuple[int, str]]:
        """Same stream as JSON text, the form it arrives in from the socket."""
        return [(ms, json.dumps(m)) for ms, m in self.messages(n)]
