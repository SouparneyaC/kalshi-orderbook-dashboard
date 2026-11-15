"""
Book accuracy audit (book_audit.py + kalshi_feed.handle_message).

The key test corrupts a book on purpose and checks the audit reports it: an audit that cannot catch
a planted error proves nothing.
"""
import os
import unittest

os.environ.setdefault("KALSHI_KEY_ID", "test")            # kalshi_feed imports auth.py
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")

from app.kalshi import kalshi_feed
from app.core import market_state
from app.kalshi.book_audit import BookAudit, diff_levels
from app.core.orderbook import OrderBook

SID = 4
A, B = "KXA-26NOV04-X", "KXB-26NOV04-Y"


def snapshot(seq, ticker, yes=(("0.4000", "10.00"), ("0.4100", "5.00")), no=(("0.5800", "7.00"),)):
    return {"type": "orderbook_snapshot", "sid": SID, "seq": seq, "msg": {
        "market_ticker": ticker, "yes_dollars_fp": [list(l) for l in yes], "no_dollars_fp": [list(l) for l in no]}}


def delta(seq, ticker, price="0.4000", change="1.00", side="yes"):
    return {"type": "orderbook_delta", "sid": SID, "seq": seq, "msg": {
        "market_ticker": ticker, "price_dollars": price, "delta_fp": change, "side": side}}


class DiffTest(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(diff_levels({1: 5, 2: 6}, {1: 5, 2: 6}), (2, 0, 0))

    def test_wrong_quantity_missing_and_extra_level(self):
        self.assertEqual(diff_levels({1: 5, 2: 6}, {1: 9, 3: 2}), (3, 3, 6))


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.a = BookAudit(interval_ms=5000)
        self.books = {A: OrderBook(), B: OrderBook()}

    def test_book_without_snapshot_is_never_picked(self):
        self.assertEqual(self.a.next_ticker(self.books, 1000), (None, 0))

    def test_oldest_audit_goes_first(self):
        self.a.on_snapshot(A, self.books[A], {}, 1000)
        self.a.on_snapshot(B, self.books[B], {}, 2000)
        self.assertEqual(self.a.next_ticker(self.books, 3000)[0], A)
        self.assertEqual(self.a.next_ticker(self.books, 4000)[0], B)    # A is in flight

    def test_lost_request_is_counted_and_freed(self):
        self.a.on_snapshot(A, self.books[A], {}, 0)
        self.assertEqual(self.a.next_ticker(self.books, 1000)[0], A)
        self.assertEqual(self.a.next_ticker(self.books, 40_000), (A, 1))

    def test_demoted_book_leaves_the_rotation(self):
        self.a.on_snapshot(A, self.books[A], {}, 0)
        self.assertEqual(self.a.next_ticker({}, 1000), (None, 0))
        self.assertNotIn(A, self.a.ready)

    def test_due_and_off_switch(self):
        self.assertTrue(self.a.due(0))
        self.a.next_ticker(self.books, 100)
        self.assertFalse(self.a.due(5099))
        self.assertTrue(self.a.due(5100))
        self.assertFalse(BookAudit(interval_ms=0).due(10**12))


class FeedTest(unittest.TestCase):
    def setUp(self):
        market_state.books.clear()
        market_state.book_audit.reset()
        market_state.book_audit.rows.clear()
        market_state.book_sub_sid = SID
        market_state.book_seq.reset()
        kalshi_feed.handle_message(snapshot(1, A), 0, 1000)

    def tearDown(self):
        market_state.books.clear()
        market_state.book_audit.reset()
        market_state.book_sub_sid = None

    def audit_with(self, seq, msg):
        ticker, _ = market_state.book_audit.next_ticker(market_state.books, 2000)
        self.assertEqual(ticker, A)
        kalshi_feed.handle_message(msg, 0, 3000)
        return market_state.book_audit.take_rows()

    def test_first_snapshot_is_not_an_audit(self):
        self.assertEqual(market_state.book_audit.take_rows(), [])

    def test_book_kept_by_deltas_matches_exactly(self):
        kalshi_feed.handle_message(delta(2, A, change="2.50"), 0, 1100)
        rows = self.audit_with(3, snapshot(3, A, yes=(("0.4000", "12.50"), ("0.4100", "5.00"))))
        self.assertEqual(rows[0][1:4], (A, 3, 0))

    def test_planted_error_is_caught_then_repaired(self):
        market_state.books[A].yes.add(4000, 700)     # 7 contracts Kalshi never sent
        rows = self.audit_with(2, snapshot(2, A))
        self.assertEqual(rows[0][1:5], (A, 3, 1, 7.0))
        self.assertEqual(rows[0][5], 2.0)            # seconds since the book last loaded
        self.assertEqual(diff_levels(dict(zip(market_state.books[A].yes.px, market_state.books[A].yes.qty)),
                                     {4000: 1000, 4100: 500})[1], 0)

    def test_missing_level_is_caught(self):
        rows = self.audit_with(2, snapshot(2, A, no=(("0.5800", "7.00"), ("0.5700", "3.00"))))
        self.assertEqual(rows[0][3], 1)

    def test_seq_gap_resync_snapshot_is_not_an_audit(self):
        kalshi_feed.handle_message(snapshot(2, A), 0, 1500)
        self.assertEqual(market_state.book_audit.take_rows(), [])


if __name__ == "__main__":
    unittest.main()
