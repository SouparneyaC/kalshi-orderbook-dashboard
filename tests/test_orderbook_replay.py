"""
The array-based OrderBook must agree with the old Decimal-dict one.

tests/legacy_orderbook.py is the previous, Decimal-dict orderbook.py, unchanged. We feed both books the
same random snapshots and deltas (cent and sub-cent prices, levels emptied, deltas for levels
that don't exist, duplicate prices, empty sides) and compare every number the dashboard sees.
"""
import random
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orderbook import OrderBook                      # noqa: E402
from tests.legacy_orderbook import OrderBook as OldBook  # noqa: E402

EXACT = {"implied_prob", "depth_imbalance", "depth_imbalance_near", "yes_levels", "no_levels", "ticker"}


def same_number(a, b):
    if a is None or b is None:
        return a is b
    return Decimal(a) == Decimal(b)          # "0" == "0.00", "0.39" == "0.3900"


def px(t):
    return f"{t / 10000:.4f}"


def qty(h):
    return f"{h / 100:.2f}"


class ReplayTest(unittest.TestCase):
    def assert_same(self, new, old, where):
        a, b = new.summary(), old.summary()
        for k in b:
            if k == "ladder":
                for side in ("bids", "asks"):
                    self.assertEqual(len(a[k][side]), len(b[k][side]), f"{where} ladder {side}")
                    for (p1, q1), (p2, q2) in zip(a[k][side], b[k][side]):
                        self.assertTrue(same_number(p1, p2) and same_number(q1, q2), f"{where} ladder {side}")
            elif k in EXACT:
                self.assertEqual(a[k], b[k], f"{where}: {k}")
            else:
                self.assertTrue(same_number(a[k], b[k]), f"{where}: {k} new={a[k]} old={b[k]}")
        # the slower query methods scripts use
        self.assertEqual(new.best_yes_bid(), old.best_yes_bid(), where)
        self.assertEqual(new.best_no_ask(), old.best_no_ask(), where)
        self.assertEqual(new.no_spread(), old.no_spread(), where)
        self.assertEqual(new.implied_probability(), old.implied_probability(), where)
        self.assertEqual(new.yes_depth(Decimal("0.02")), old.yes_depth(Decimal("0.02")), where)
        self.assertEqual(new.depth_imbalance(Decimal("0.05")), old.depth_imbalance(Decimal("0.05")), where)
        self.assertEqual(new.yes_bids, old.yes_bids, where)

    def random_snapshot(self, rng, step):
        def side():
            mid = rng.randint(1, 9999 // step) * step
            return [[px(max(step, min(9999, mid - step * rng.randint(0, 40)))), qty(rng.randint(1, 900_000))]
                    for _ in range(rng.randint(0, 25))]
        return {"market_ticker": "T", "yes_dollars_fp": side(), "no_dollars_fp": side()}

    def test_random_replay(self):
        rng = random.Random(11)
        checks = 0
        for game in range(300):
            step = rng.choice([1, 100, 10])          # sub-cent, whole cent, tenth of a cent
            new, old = OrderBook(), OldBook()
            snap = self.random_snapshot(rng, step)
            new.load_snapshot(snap, ticker="T")
            old.load_snapshot(snap, ticker="T")
            self.assert_same(new, old, f"game {game} snapshot")
            for n in range(rng.randint(1, 400)):
                side = rng.choice(["yes", "no"])
                levels = (new.yes if side == "yes" else new.no).px
                r = rng.random()
                if levels and r < 0.3:               # empty an existing level exactly
                    p = rng.choice(list(levels))
                    cur = dict(zip(levels, (new.yes if side == "yes" else new.no).qty))[p]
                    d = -cur
                elif levels and r < 0.6:             # change an existing level, sometimes past zero
                    p = rng.choice(list(levels))
                    d = rng.randint(-500_000, 500_000)
                else:                                # a price that may or may not exist
                    p = rng.randint(1, 9999 // step) * step
                    d = rng.randint(-50_000, 300_000)
                delta = {"price_dollars": px(p), "delta_fp": qty(d), "side": side}
                new.apply_delta(delta)
                old.apply_delta(delta)
                if n % 7 == 0:
                    self.assert_same(new, old, f"game {game} delta {n}")
                    checks += 1
            self.assert_same(new, old, f"game {game} end")
        self.assertGreater(checks, 1000)

    def test_rest_snapshot_and_empty(self):
        rest = {"orderbook_fp": {"yes_dollars": [["0.3900", "10.00"], ["0.4000", "5.50"]], "no_dollars": []}}
        new, old = OrderBook(), OldBook()
        new.load_snapshot(rest, ticker="R"); old.load_snapshot(rest, ticker="R")
        self.assert_same(new, old, "rest")
        new.clear(); old.clear()
        self.assert_same(new, old, "cleared")

    def test_summary_is_cached_until_change(self):
        b = OrderBook()
        b.load_snapshot({"yes_dollars_fp": [["0.5000", "1.00"]], "no_dollars_fp": [["0.4000", "2.00"]]}, ticker="C")
        s1 = b.summary()
        self.assertIs(b.summary(), s1)                       # no change → same object, no work
        b.apply_delta({"price_dollars": "0.5100", "delta_fp": "3.00", "side": "yes"})
        s2 = b.summary()
        self.assertIsNot(s2, s1)
        self.assertEqual(s2["best_yes_bid"], "0.5100")
        b.ticker = "D"                                       # promote code sets .ticker directly
        self.assertEqual(b.summary()["ticker"], "D")


if __name__ == "__main__":
    unittest.main()
