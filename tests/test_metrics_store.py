"""Per-minute histograms and the DuckDB measurement store.

The point of the store is that decisions get made from it, so these tests pin down accuracy:
percentiles read from buckets vs exact sorted percentiles, exact count/min/max, zero and negative
samples kept, and the self-check that every recorded sample reaches the minute it belongs to.
"""
import math
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")

import duckdb

from app.core.latency import SUB, LatencyMonitor, MinuteHist, hist_percentile
from app.storage.metrics_store import MetricsStore

MAX_REL_ERR = 2 ** (1 / (2 * SUB)) - 1   # half a bucket, geometric: 2.19% at SUB=16


def exact_pct(samples, q):
    s = sorted(samples)
    return s[max(1, math.ceil(q * len(s))) - 1]


class TestMinuteHist(unittest.TestCase):
    def test_percentiles_within_half_a_bucket(self):
        rng = random.Random(7)
        # A latency-shaped mix: a lognormal body around 0.05 ms plus a fat tail out to seconds.
        samples = [rng.lognormvariate(math.log(0.05), 0.8) for _ in range(50_000)]
        samples += [rng.uniform(5, 3000) for _ in range(500)]
        h = MinuteHist()
        for v in samples:
            h.add(v)
        for q in (0.5, 0.9, 0.99, 0.999):
            est = hist_percentile(q, h.pos, h.neg, h.zero, h.min, h.max)
            ref = exact_pct(samples, q)
            self.assertLessEqual(abs(est - ref) / ref, MAX_REL_ERR + 1e-12, f"p{q}: {est} vs {ref}")

    def test_exact_fields(self):
        h = MinuteHist()
        vals = [0.003, 1.5, 250.0, 7.0, 7.0]
        for v in vals:
            h.add(v)
        self.assertEqual(h.count, 5)
        self.assertEqual(h.min, 0.003)
        self.assertEqual(h.max, 250.0)
        self.assertAlmostEqual(h.sum, sum(vals))
        self.assertEqual(hist_percentile(1.0, h.pos, h.neg, h.zero, h.min, h.max), 250.0)  # clamped to max
        self.assertEqual(hist_percentile(0.0, h.pos, h.neg, h.zero, h.min, h.max), 0.003)  # clamped to min

    def test_zero_and_negative_kept(self):
        # venue_to_recv crosses two clocks and can read <= 0; those samples must count, in order.
        h = MinuteHist()
        for v in (-48.0, -3.0, 0.0, 0.0, 6.0, 7.0):
            h.add(v)
        self.assertEqual(h.count, 6)
        self.assertEqual(h.zero, 2)
        self.assertEqual(sum(h.neg.values()), 2)
        p = lambda q: hist_percentile(q, h.pos, h.neg, h.zero, h.min, h.max)
        self.assertAlmostEqual(p(1 / 6), -48.0, delta=48 * MAX_REL_ERR)
        self.assertAlmostEqual(p(2 / 6), -3.0, delta=3 * MAX_REL_ERR)
        self.assertEqual(p(0.5), 0.0)
        self.assertAlmostEqual(p(1.0), 7.0)

    def test_minutes_merge_exactly(self):
        # An hour is the sum of its minutes: merging bucket counts loses nothing.
        rng = random.Random(1)
        a, b, both = MinuteHist(), MinuteHist(), MinuteHist()
        for i in range(4000):
            v = rng.expovariate(1 / 0.2)
            (a if i % 2 else b).add(v)
            both.add(v)
        merged = dict(a.pos)
        for k, c in b.pos.items():
            merged[k] = merged.get(k, 0) + c
        self.assertEqual(merged, both.pos)

    def test_take_minute_swaps(self):
        m = LatencyMonitor()
        m.record("x", 1.0)
        first = m.take_minute()
        m.record("x", 2.0)
        self.assertEqual(first["x"].count, 1)
        self.assertEqual(m.minute["x"].count, 1)
        self.assertEqual(m.stages["x"].total, 2)   # the rolling window is untouched


class TestStore(unittest.TestCase):
    def test_round_trip_and_self_checks(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "m.duckdb"
            mon = LatencyMonitor()
            msgs = [0]
            store = MetricsStore(mon, lambda: {"counters": {"msgs": msgs[0]}, "levels": {"books": 200}},
                                 settings={"BOOK_COUNT": 200}, db_path=db)
            store.start()
            t = 1_800_000_000 * 10**9          # a fixed wall time, minute-aligned
            for minute in range(3):
                for i in range(1000):
                    mon.record("recv_to_hub", 0.01 + i * 1e-5)
                mon.record("venue_to_recv.trade", -2.0)
                msgs[0] += 6000
                store.swap(t + minute * 60 * 10**9, t + (minute + 1) * 60 * 10**9)
            store.q.put(None)
            store.thread.join(10)
            self.assertEqual(store.flush_errors, 0)

            con = duckdb.connect(str(db))
            self.assertEqual(con.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            rows = con.execute("SELECT count, min_ms, cross_machine FROM stage_minute "
                               "WHERE stage='recv_to_hub' ORDER BY minute").fetchall()
            self.assertEqual([r[0] for r in rows], [1000, 1000, 1000])
            neg = con.execute("SELECT min_ms, cross_machine, neg_counts FROM stage_minute "
                              "WHERE stage='venue_to_recv.trade' LIMIT 1").fetchone()
            self.assertEqual(neg[0], -2.0)
            self.assertTrue(neg[1])
            self.assertEqual(neg[2], [1])
            metric = dict(con.execute("SELECT name, value FROM minute_metric "
                                      "WHERE minute = (SELECT max(minute) FROM minute_metric)").fetchall())
            self.assertEqual(metric["check.hist_count_mismatch"], 0)
            self.assertEqual(metric["check.gap_s"], 0)
            self.assertAlmostEqual(metric["msgs_per_s"], 100.0)
            self.assertEqual(metric["books"], 200)
            con.close()


if __name__ == "__main__":
    unittest.main()
