"""
Order books go to the exact top N markets by recent dollars traded (faded x0.85 every 30 s).
"""
import os
import unittest

os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")

import kalshi_feed
import market_state as ms


def trade(ticker, yes_px, count, side="yes", tid=[0]):
    tid[0] += 1
    return {"type": "trade", "sid": 2, "msg": {"market_ticker": ticker, "yes_price_dollars": f"{yes_px:.4f}",
            "count_fp": f"{count:.2f}", "taker_outcome_side": side, "trade_id": f"sel-{tid[0]}", "ts_ms": 1}}


class BookSelectionTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(ms.recent_dollars)
        ms.recent_dollars.clear()

    def tearDown(self):
        ms.recent_dollars.clear(); ms.recent_dollars.update(self.saved)

    def test_dollars_not_contracts(self):
        kalshi_feed.handle_message(trade("PENNY-PARLAY", 0.001, 70_000), 0, 0)   # $70
        kalshi_feed.handle_message(trade("BTC15M", 0.50, 1_000), 0, 0)            # $500
        kalshi_feed.handle_message(trade("GAME", 0.30, 1_000, side="no"), 0, 0)   # NO taker pays 0.70: $700
        self.assertEqual([t for t, _ in ms.top_recent_dollars(3)], ["GAME", "BTC15M", "PENNY-PARLAY"])
        self.assertAlmostEqual(ms.recent_dollars["GAME"], 700.0)

    def test_exact_top_n_in_a_long_tail(self):
        # 50 steady markets, then 2,000 one-off trades of $100 each: a K=60 Misra-Gries sketch loses the
        # steady markets in this tail, the exact counter must not.
        for _ in range(20):
            for i in range(50):
                ms.add_recent_dollars(f"STEADY{i}", 60.0)                        # $1,200 each
        for j in range(2000):
            ms.add_recent_dollars(f"ONEOFF{j}", 100.0)
        self.assertEqual({t for t, _ in ms.top_recent_dollars(50)}, {f"STEADY{i}" for i in range(50)})

    def test_decay_fades_and_drops_dust(self):
        ms.recent_dollars.update({"A": 100.0, "B": 1.1})
        import asyncio

        async def one_cycle():
            real, calls = asyncio.sleep, [0]
            async def once(_):
                calls[0] += 1
                if calls[0] > 1:
                    raise asyncio.CancelledError   # stop after exactly one decay pass
                await real(0)
            ms.asyncio.sleep = once
            try:
                await ms.analytics_decay_loop()
            except asyncio.CancelledError:
                pass
            finally:
                ms.asyncio.sleep = real
        asyncio.run(one_cycle())
        self.assertNotIn("B", ms.recent_dollars)                                    # 1.1 x 0.85 < $1
        self.assertAlmostEqual(ms.recent_dollars["A"], 85.0)


class TopListReservationTest(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(ms.recent_dollars), dict(ms.tickers), ms.TOP_LIST_BOOKS)
        ms.recent_dollars.clear(); ms.tickers.clear()
        for i in range(10):                       # big lifetime $ volume, quiet right now
            ms.update_ticker(f"BIG{i}", {"dollar_volume": 10_000_000 - i})
        for i in range(10):                       # small lifetime, very active now
            ms.update_ticker(f"LIVE{i}", {"dollar_volume": 1_000 + i})
            ms.add_recent_dollars(f"LIVE{i}", 50_000 - i)
        ms.add_recent_dollars("BIG0", 99_999)      # also active: must not be counted twice
        ms.TOP_LIST_BOOKS = 3

    def tearDown(self):
        r, t, k = self.saved
        ms.recent_dollars.clear(); ms.recent_dollars.update(r)
        ms.tickers.clear(); ms.tickers.update(t)
        ms.TOP_LIST_BOOKS = k

    def test_top_rows_reserved_rest_by_activity(self):
        picks = ms.book_candidates(6)
        self.assertEqual(picks[:3], ["BIG0", "BIG1", "BIG2"])      # top of the table
        self.assertEqual(picks[3:], ["LIVE0", "LIVE1", "LIVE2"])    # most active now, BIG0 not repeated
        self.assertEqual(len(set(picks)), 6)

    def test_reservation_never_exceeds_n(self):
        self.assertEqual(ms.book_candidates(2), ["BIG0", "BIG1"])


if __name__ == "__main__":
    unittest.main()
