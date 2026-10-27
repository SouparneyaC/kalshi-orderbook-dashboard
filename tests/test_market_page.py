"""
Market page backend: the full-depth book, the per-market trade ring, the live tab sender
(MarketClient), the route guards, and the metadata trimming.
"""
import asyncio
import json
import os
import unittest
from collections import deque
from unittest import mock

os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")

import market_page
import market_state
import server
from orderbook import OrderBook

T = "KXTEST-26-A"


def make_book(levels=15):
    b = OrderBook(T)
    b.load_snapshot({
        "yes_dollars_fp": [[f"{0.40 - i * 0.01:.4f}", "10.00"] for i in range(levels)],
        "no_dollars_fp": [[f"{0.50 - i * 0.01:.4f}", "20.00"] for i in range(levels)],
    }, ticker=T)
    return b


class DepthTest(unittest.TestCase):
    def test_depth_returns_every_level_while_ladder_stops_at_ten(self):
        b = make_book(15)
        self.assertEqual(len(b.ladder()["bids"]), 10)
        d = b.depth()
        self.assertEqual((len(d["bids"]), len(d["asks"])), (15, 15))
        self.assertEqual(d["bids"][0], ["0.4000", "10.00"])      # best bid first
        self.assertEqual(d["asks"][0], ["0.5000", "20.00"])      # best ask (1 - 0.50 NO bid) first

    def test_depth_is_cached_until_the_book_changes(self):
        b = make_book(3)
        self.assertIs(b.depth(), b.depth())
        v = b.ver
        b.apply_delta({"price_dollars": "0.4000", "delta_fp": "5.00", "side": "yes"})
        self.assertEqual(b.ver, v + 1)
        self.assertEqual(b.depth()["bids"][0], ["0.4000", "15.00"])

    def test_every_kind_of_change_bumps_the_version(self):
        b = make_book(2)
        v = b.ver
        b.load_snapshot({"yes_dollars_fp": [], "no_dollars_fp": []})
        self.assertGreater(b.ver, v)
        v = b.ver
        b.clear()
        self.assertGreater(b.ver, v)


class TradeRingTest(unittest.TestCase):
    def setUp(self):
        self._saved = (dict(market_state.books), dict(market_state.market_trades), list(market_state.trade_buffer.trades))
        market_state.books.clear()
        market_state.market_trades.clear()
        market_state.trade_buffer.clear()

    def tearDown(self):
        market_state.books.clear(); market_state.books.update(self._saved[0])
        market_state.market_trades.clear(); market_state.market_trades.update(self._saved[1])
        market_state.trade_buffer.clear()
        for t in self._saved[2]:
            market_state.trade_buffer.add(t)

    def trade(self, n, ticker=T):
        t = {"ticker": ticker, "price": "0.5000", "count": "1.00", "side": "yes", "ts": n, "recv_ts": n}
        market_state.trade_buffer.add(t)
        market_state.record_market_trade(ticker, t)
        return t

    def test_unbooked_markets_keep_no_ring(self):
        self.trade(1)
        self.assertNotIn(T, market_state.market_trades)

    def test_first_trade_seeds_from_the_global_buffer_without_duplicating(self):
        self.trade(1)                            # before the book: only in trade_buffer
        market_state.books[T] = make_book(1)
        self.trade(2)                            # first trade after the book: ring is seeded from the buffer
        self.assertEqual([t["ts"] for t in market_state.market_trades[T]], [1, 2])
        self.trade(3)
        self.assertEqual([t["ts"] for t in market_state.market_trades[T]], [1, 2, 3])

    def test_ring_is_capped(self):
        market_state.books[T] = make_book(1)
        for i in range(market_state.MARKET_TRADES_MAX + 20):
            self.trade(i)
        self.assertEqual(len(market_state.market_trades[T]), market_state.MARKET_TRADES_MAX)

    def test_prune_drops_rings_of_demoted_books(self):
        market_state.books[T] = make_book(1)
        self.trade(1); self.trade(2)
        del market_state.books[T]
        market_state.prune_stale_markets()
        self.assertNotIn(T, market_state.market_trades)


class FakeWS:
    def __init__(self):
        self.sent, self.closed = [], False

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True


class MarketClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (dict(market_state.books), dict(market_state.tickers), dict(market_state.market_trades))
        market_state.books.clear(); market_state.tickers.clear(); market_state.market_trades.clear()
        market_state.books[T] = make_book(15)
        m = market_state.Market()
        m.price, m.yes_bid, m.yes_ask = "0.4500", "0.4000", "0.5000"
        m.yes_bid_size = m.yes_ask_size = "1.00"
        m.volume = m.open_interest = m.dollar_volume = "100"
        m.dv = 100.0
        market_state.tickers[T] = m
        self._interval = market_page.MIN_PUSH_INTERVAL_S
        market_page.MIN_PUSH_INTERVAL_S = 0.01

    async def asyncTearDown(self):
        market_page.MIN_PUSH_INTERVAL_S = self._interval
        market_page.watchers.clear()
        for store, saved in ((market_state.books, self._saved[0]), (market_state.tickers, self._saved[1]),
                             (market_state.market_trades, self._saved[2])):
            store.clear(); store.update(saved)

    async def start(self):
        ws = FakeWS()
        c = market_page.MarketClient(ws, T)
        c.register()
        task = asyncio.create_task(c.run())
        return ws, c, task

    async def until(self, cond):
        for _ in range(200):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail("timed out")

    async def test_first_frame_has_full_book_quote_and_no_trades_key_problems(self):
        ws, c, task = await self.start()
        await self.until(lambda: ws.sent)
        f = ws.sent[0]
        self.assertEqual(f["type"], "market")
        self.assertTrue(f["has_book"])
        self.assertEqual(len(f["book"]["depth"]["bids"]), 15)       # full depth, not 10
        self.assertNotIn("ladder", f["book"])
        self.assertEqual(f["quote"]["yes_bid"], "0.4000")
        await c.close(); task.cancel()

    async def test_book_change_sends_a_new_book_and_a_quiet_frame_omits_it(self):
        ws, c, task = await self.start()
        await self.until(lambda: len(ws.sent) >= 1)
        c.wake.set()                                                  # nothing changed
        await self.until(lambda: len(ws.sent) >= 2)
        self.assertNotIn("book", ws.sent[1])
        market_state.books[T].apply_delta({"price_dollars": "0.4000", "delta_fp": "5.00", "side": "yes"})
        market_page.notify(T)
        await self.until(lambda: any("book" in f for f in ws.sent[2:]))
        newest = [f for f in ws.sent[2:] if "book" in f][0]
        self.assertEqual(newest["book"]["depth"]["bids"][0], ["0.4000", "15.00"])
        await c.close(); task.cancel()

    async def test_new_trade_is_sent_once(self):
        ws, c, task = await self.start()
        await self.until(lambda: len(ws.sent) >= 1)
        t = {"ticker": T, "price": "0.4500", "count": "3.00", "side": "no", "ts": 5, "recv_ts": 5}
        market_state.trade_buffer.add(t)
        market_state.record_market_trade(T, t)
        market_page.notify(T)
        await self.until(lambda: any("trades" in f for f in ws.sent))
        frames = [f for f in ws.sent if "trades" in f]
        self.assertEqual(frames[-1]["trades"][-1], {"ts": 5, "side": "no", "price": "0.4500", "count": "3.00"})
        n = len(frames)
        c.wake.set()
        await asyncio.sleep(0.1)
        self.assertEqual(len([f for f in ws.sent if "trades" in f]), n)   # unchanged tape is not resent
        await c.close(); task.cancel()

    async def test_demoted_book_is_reported_and_close_unregisters(self):
        ws, c, task = await self.start()
        await self.until(lambda: len(ws.sent) >= 1)
        del market_state.books[T]
        market_page.notify(T)
        await self.until(lambda: not ws.sent[-1]["has_book"])
        await c.close(); task.cancel()
        self.assertNotIn(T, market_page.watchers)
        self.assertTrue(ws.closed)

    async def test_notify_with_no_watchers_is_a_noop(self):
        market_page.notify("NOBODY-WATCHES")      # must not raise or create an entry
        self.assertNotIn("NOBODY-WATCHES", market_page.watchers)


class RouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_page_404_without_book_and_escapes_the_ticker(self):
        market_state.books.pop("<b>x</b>", None)
        r = await server.market_page_html("<b>x</b>")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(b"<b>x</b>", r.body)

    async def test_page_serves_html_when_booked(self):
        market_state.books[T] = make_book(1)
        try:
            r = await server.market_page_html(T)
            self.assertEqual(r.status_code, 200)
        finally:
            del market_state.books[T]

    async def test_ws_rejects_unbooked_market(self):
        class WS:
            code = None
            async def close(self, code=1000): self.code = code
            async def accept(self): raise AssertionError("must not accept")
        ws = WS()
        await server.market_ws(ws, "NOPE")
        self.assertEqual(ws.code, 4404)

    async def test_info_error_becomes_502(self):
        with mock.patch.object(market_page, "get_info", side_effect=RuntimeError("boom")):
            r = await server.market_info(T)
        self.assertEqual(r.status_code, 502)


class InfoTest(unittest.IsolatedAsyncioTestCase):
    def test_trim_keeps_only_panel_fields(self):
        info = market_page._trim_info(
            {"ticker": T, "title": "Seattle wins", "yes_sub_title": "Seattle", "status": "active",
             "rules_primary": "If Seattle wins...", "close_time": "2026-11-05T01:15:00Z",
             "mve_selected_legs": [{"market_ticker": "X", "side": "yes"}], "secret_field": 1},
            {"category": "Sports", "title": "Chicago vs Seattle", "settlement_sources": [{"name": "NFL", "url": "u"}]})
        self.assertEqual(info["category"], "Sports")
        self.assertEqual(info["legs"][0]["market_ticker"], "X")
        self.assertNotIn("secret_field", info)

    def test_trim_survives_a_missing_event(self):
        info = market_page._trim_info({"ticker": T}, None)
        self.assertIsNone(info["category"])
        self.assertEqual(info["settlement_sources"], [])

    async def test_info_is_cached(self):
        market_page._info_cache.clear()
        calls = []
        def fake(t):
            calls.append(t); return {"ticker": t}
        with mock.patch.object(market_page, "_fetch_info", fake):
            await market_page.get_info(T); await market_page.get_info(T)
        self.assertEqual(calls, [T])
        market_page._info_cache.clear()


if __name__ == "__main__":
    unittest.main()
