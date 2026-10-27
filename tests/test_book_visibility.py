"""
Every market with an order book must reach the browser, even when its $ volume is outside the
top PAYLOAD_MAX_MARKETS. Unbooked markets keep the old rule (top N by $ volume).
"""
import os
import unittest

os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")

import market_state as ms
from orderbook import OrderBook


class BookVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(ms.tickers), dict(ms.books), dict(ms.view), set(ms.dirty_markets),
                      ms._last_view_resync, ms.PAYLOAD_MAX_MARKETS)
        ms.tickers.clear(); ms.books.clear(); ms.view.clear(); ms.dirty_markets.clear()
        ms.PAYLOAD_MAX_MARKETS = 5
        for i in range(20):                         # BIG0 > BIG1 > ... by $ volume
            self.ticker(f"BIG{i}", 1_000_000 - i * 1000)
        for i in range(3):                          # tiny $ volume, but booked
            self.ticker(f"HOT{i}", 10 + i)
            ms.books[f"HOT{i}"] = OrderBook()
        ms._last_view_resync = 0.0

    def tearDown(self):
        t, b, v, d, last, cap = self.saved
        ms.tickers.clear(); ms.tickers.update(t)
        ms.books.clear(); ms.books.update(b)
        ms.view.clear(); ms.view.update(v)
        ms.dirty_markets.clear(); ms.dirty_markets.update(d)
        ms._last_view_resync = last
        ms.PAYLOAD_MAX_MARKETS = cap

    def ticker(self, t, dv):
        ms.update_ticker(t, {"market_ticker": t, "price_dollars": "0.50", "dollar_volume": dv,
                            "volume_fp": "1", "open_interest_fp": "1"})
        ms.dirty_markets.add(t)

    def expected(self):
        return {f"BIG{i}" for i in range(5)} | {"HOT0", "HOT1", "HOT2"}

    def test_full_payload_has_top_n_plus_all_books(self):
        sent = {m["ticker"] for m in ms.build_full_payload()["markets"]}
        self.assertEqual(sent, self.expected())

    def test_delta_view_has_top_n_plus_all_books(self):
        p = ms.build_delta_payload()                # first delta: exact re-rank
        self.assertEqual(set(ms.view), self.expected())
        self.assertTrue({"HOT0", "HOT1", "HOT2"} <= {m["ticker"] for m in p["markets_changed"]})

    def test_book_promoted_later_is_added_and_kept(self):
        ms.build_delta_payload()
        self.ticker("HOT9", 1)
        ms.books["HOT9"] = OrderBook()
        for i in range(5):                          # churn among the big markets
            self.ticker(f"BIG{10 + i}", 2_000_000 + i)
        p = ms.build_delta_payload()
        self.assertIn("HOT9", ms.view)
        self.assertIn("HOT9", {m["ticker"] for m in p["markets_changed"]})
        self.assertTrue({"HOT0", "HOT1", "HOT2"} <= set(ms.view))
        unbooked = [t for t in ms.view if t not in ms.books]
        self.assertEqual(len(unbooked), 5)          # unbooked rows still capped at N

    def test_demoted_book_drops_out_on_rerank(self):
        ms.build_delta_payload()
        del ms.books["HOT0"]
        ms._last_view_resync = 0.0
        p = ms.build_delta_payload()
        self.assertNotIn("HOT0", ms.view)
        self.assertIn("HOT0", p["markets_removed"])


if __name__ == "__main__":
    unittest.main()
