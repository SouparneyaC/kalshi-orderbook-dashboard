"""
orderbook_delta seq-gap detection (book_seq.py + kalshi_feed.handle_message).

Message shapes follow what the live feed sent on 2026-10-24: `seq` is top-level, one counter
per subscription, advanced by snapshots, deltas and the `ok` reply to update_subscription.
"""
import json
import os
import unittest

os.environ.setdefault("KALSHI_KEY_ID", "test")            # kalshi_feed imports auth.py
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")

from app.kalshi import kalshi_feed
from app.core import market_state
from app.kalshi.book_seq import BookSeqTracker
from app.core.orderbook import OrderBook

SID = 4
A, B = "KXA-26OCT24-X", "KXB-26OCT24-Y"


def snapshot(seq, ticker):
    return {"type": "orderbook_snapshot", "sid": SID, "seq": seq, "msg": {
        "market_ticker": ticker, "yes_dollars_fp": [["0.4000", "10.00"]], "no_dollars_fp": [["0.5800", "7.00"]]}}


def delta(seq, ticker, change="1.00"):
    return {"type": "orderbook_delta", "sid": SID, "seq": seq, "msg": {
        "market_ticker": ticker, "price_dollars": "0.4000", "delta_fp": change, "side": "yes"}}


def ok(seq):
    return {"type": "ok", "sid": SID, "seq": seq, "id": 7, "msg": {"market_tickers": [A]}}


class TrackerTest(unittest.TestCase):
    def test_in_order_no_gap(self):
        t = BookSeqTracker()
        self.assertFalse(any(t.observe(s) for s in range(1, 50)))
        self.assertEqual(t.gaps, 0)

    def test_gap_counted_once_per_jump(self):
        t = BookSeqTracker()
        t.observe(1); t.observe(2)
        self.assertTrue(t.observe(5))
        self.assertFalse(t.observe(6))     # continues from the new position
        self.assertEqual(t.gaps, 1)

    def test_requests_deduplicated_until_snapshot(self):
        t = BookSeqTracker()
        t.request([A, B])
        self.assertEqual(t.take_pending(), [A, B])
        t.request([A, B])                  # both still in flight
        self.assertEqual(t.pending, set())
        t.snapshot_received(A)
        t.request([A, B])                  # A may be asked for again, B still waiting
        self.assertEqual(t.take_pending(), [A])

    def test_reset_and_forget(self):
        t = BookSeqTracker()
        t.observe(9); t.request([A]); t.take_pending(); t.request([B])
        t.forget(A); t.forget(B)
        self.assertEqual((t.pending, t.in_flight), (set(), set()))
        t.reset()
        self.assertIsNone(t.last)
        self.assertFalse(t.observe(500))   # first message after reset is never a gap


class HandleMessageSeqTest(unittest.TestCase):
    def setUp(self):
        self._saved = (market_state.book_sub_sid, dict(market_state.books))
        market_state.books.clear()
        for t in (A, B):
            market_state.books[t] = OrderBook()
            market_state.books[t].ticker = t
        market_state.book_seq.reset()
        market_state.book_seq.gaps = 0
        market_state.latency.counters.pop("book_seq_gaps", None)
        kalshi_feed.handle_message({"type": "subscribed", "id": 1,
                                    "msg": {"channel": "orderbook_delta", "sid": SID}}, 0, 0)

    def tearDown(self):
        market_state.book_sub_sid, books = self._saved
        market_state.books.clear()
        market_state.books.update(books)
        market_state.book_seq.reset()

    def feed(self, *msgs):
        for m in msgs:
            kalshi_feed.handle_message(json.loads(json.dumps(m)), 0, 0)

    def test_in_order_deltas_no_resync(self):
        self.feed(snapshot(1, A), snapshot(2, B), delta(3, A), delta(4, B), ok(5), delta(6, A))
        self.assertEqual(market_state.book_seq.gaps, 0)
        self.assertEqual(market_state.book_seq.pending, set())

    def test_skipped_seq_requests_every_book_on_the_sid(self):
        self.feed(snapshot(1, A), snapshot(2, B), delta(3, A), delta(5, B))   # 4 was lost
        self.assertEqual(market_state.book_seq.gaps, 1)
        self.assertEqual(market_state.latency.counters.get("book_seq_gaps"), 1)
        self.assertEqual(market_state.book_seq.pending, {A, B})

    def test_snapshot_clears_in_flight_and_continues_the_count(self):
        self.feed(snapshot(1, A), snapshot(2, B), delta(4, A))                # gap
        self.assertEqual(market_state.book_seq.take_pending(), [A, B])
        self.feed(ok(5), snapshot(6, A), snapshot(7, B), delta(8, A))         # resync replies, in order
        self.assertEqual(market_state.book_seq.gaps, 1)
        self.assertEqual(market_state.book_seq.in_flight, set())
        self.assertEqual(market_state.book_seq.last, 8)

    def test_other_subscriptions_ignored(self):
        self.feed(snapshot(1, A))
        kalshi_feed.handle_message({"type": "ticker", "sid": 1, "seq": 900,
                                    "msg": {"market_ticker": A, "price_dollars": "0.41"}}, 0, 0)
        self.feed(delta(2, A))
        self.assertEqual(market_state.book_seq.gaps, 0)

    def test_new_subscription_resets(self):
        self.feed(snapshot(1, A), delta(2, A))
        kalshi_feed.handle_message({"type": "subscribed", "id": 9,
                                    "msg": {"channel": "orderbook_delta", "sid": SID}}, 0, 0)
        self.feed(snapshot(1, A))                                             # counter restarts
        self.assertEqual(market_state.book_seq.gaps, 0)


class RequestSnapshotsTest(unittest.IsolatedAsyncioTestCase):
    async def test_sends_one_get_snapshot_for_queued_books(self):
        sent = []

        class WS:
            async def send(self, text):
                sent.append(json.loads(text))

        saved = (market_state.book_sub_sid, dict(market_state.books))
        try:
            market_state.book_sub_sid = SID
            market_state.books.clear()
            for t in (A, B):
                market_state.books[t] = OrderBook()
            market_state.book_seq.reset()
            market_state.book_seq.request([A, B, "DEMOTED"])
            ref = [10]
            await kalshi_feed._request_book_snapshots(WS(), ref)
            await kalshi_feed._request_book_snapshots(WS(), ref)              # nothing new queued
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0], {"id": 10, "cmd": "update_subscription", "params": {
                "sid": SID, "action": "get_snapshot", "market_tickers": [A, B]}})
            self.assertEqual(ref, [11])
        finally:
            market_state.book_sub_sid, books = saved
            market_state.books.clear()
            market_state.books.update(books)
            market_state.book_seq.reset()


if __name__ == "__main__":
    unittest.main()
