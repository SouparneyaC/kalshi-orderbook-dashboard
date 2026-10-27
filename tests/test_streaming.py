"""The faster streaming code must give the same answers as the old code."""
import hashlib
import random
import statistics
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from streaming import DistinctCounter, MisraGries, WindowedTradeBuffer  # noqa: E402


class OldMisraGries(MisraGries):
    """The previous add(), kept verbatim as the reference."""
    def add(self, item, weight=1.0):
        self.total += weight
        if item in self.counters:
            self.counters[item] += weight
        elif len(self.counters) < self.k:
            self.counters[item] = weight
        else:
            min_val = min(self.counters.values())
            if weight <= min_val:
                for key in self.counters:
                    self.counters[key] -= weight
                self.counters = {k: v for k, v in self.counters.items() if v > 0}
            else:
                for key in self.counters:
                    self.counters[key] -= min_val
                self.counters = {k: v for k, v in self.counters.items() if v > 0}
                self.counters[item] = weight - min_val


class OldDistinct(DistinctCounter):
    def add(self, item):
        for i in range(self.num_hashes):
            h = int(hashlib.md5(f"{self.seeds[i]}:{item}".encode()).hexdigest()[:8], 16)
            if h < self.mins[i]:
                self.mins[i] = h


class OldBuffer:
    def __init__(self, maxlen):
        self.maxlen, self.trades = maxlen, []

    def add(self, t):
        self.trades.append(t)
        if len(self.trades) > self.maxlen:
            self.trades = self.trades[-self.maxlen:]

    def recent(self, n=50):
        return self.trades[-n:]


class StreamingTests(unittest.TestCase):
    def test_misra_gries_identical(self):
        rng = random.Random(5)
        new, old = MisraGries(k=60), OldMisraGries(k=60)
        for _ in range(200_000):
            item = f"M{min(int(rng.paretovariate(1.1)), 5000)}"
            w = rng.choice([1.0, 2.0, 5.0, 10.0, 0.5, 100.0])
            new.add(item, w)
            old.add(item, w)
            if rng.random() < 0.0005:
                new.decay(0.85); old.decay(0.85)
        self.assertEqual(new.top(), old.top())   # same items, same counts, same order

    def test_distinct_counter_as_accurate_as_md5(self):
        """Not identical (different hash), so check accuracy is no worse over many seeds."""
        errs_new, errs_old = [], []
        for seed in range(40):
            random.seed(seed)
            new, old = DistinctCounter(16), OldDistinct(16)
            true = 3000
            for i in range(true):
                for c in (new, old):
                    c.add(f"KXSERIES-{seed}-{i}")
            errs_new.append(abs(new.estimate() - true) / true)
            errs_old.append(abs(old.estimate() - true) / true)
        med_new, med_old = statistics.median(errs_new), statistics.median(errs_old)
        print(f"\n  distinct counter median error: hash()={med_new:.1%}  md5={med_old:.1%}")
        self.assertLess(med_new, max(0.5, med_old * 1.5))

    def test_trade_buffer_identical(self):
        new, old = WindowedTradeBuffer(500), OldBuffer(500)
        for i in range(3000):
            new.add({"i": i}); old.add({"i": i})
            if i % 37 == 0:
                for n in (1, 50, 499, 500, 800):
                    self.assertEqual(new.recent(n), old.recent(n))
        self.assertEqual(len(new.trades), 500)


if __name__ == "__main__":
    unittest.main()
