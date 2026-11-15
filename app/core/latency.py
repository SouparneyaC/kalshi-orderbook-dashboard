"""
Latency instrumentation.

Every event is stamped at five points: venue (exchange's own ts), recv (socket read),
hub (state updated), send (pushed to browsers), render (browser painted it).
The gaps between stamps are "stages". Each stage keeps a rolling window of recent
samples and reports p50 / p99 / p99.9, never averages, because traders feel the tail.
"""

import asyncio
import gc
import math
import time
from collections import deque

WINDOW = 4096          # samples kept per stage
SNAPSHOT_TTL_S = 1.0   # recompute percentiles at most once per second


def now_ms() -> int:
    """Wall-clock milliseconds. Use only when comparing with another machine's clock."""
    return time.time_ns() // 1_000_000


def perf_ns() -> int:
    """Monotonic nanoseconds. Use for durations inside this process."""
    return time.perf_counter_ns()


def ns_to_ms(ns: int) -> float:
    return ns / 1_000_000


class StageStats:
    """Rolling window of latency samples (ms) for one stage."""

    def __init__(self, maxlen: int = WINDOW):
        self.samples: deque[float] = deque(maxlen=maxlen)
        self.total = 0
        self._snap: dict | None = None
        self._snap_total = -1

    def add(self, ms: float):
        self.samples.append(ms)
        self.total += 1

    def snapshot(self) -> dict | None:
        if not self.samples:
            return None
        # Sorting 4096 samples x ~25 stages took ~7 ms, once a second, on the
        # event loop. Most stages (gc, db_flush, queue...) get no new samples in a given second,
        # so a stage is only re-sorted when it has new samples. Same numbers, a fraction of the work.
        if self.total == self._snap_total:
            return self._snap
        s = sorted(self.samples)
        n = len(s)

        def pct(p: float) -> float:
            return round(s[min(n - 1, int(p * n))], 2)

        self._snap = {
            "p50": pct(0.50),
            "p99": pct(0.99),
            "p999": pct(0.999),
            "max": round(s[-1], 2),
            "n": n,
            "total": self.total,
        }
        self._snap_total = self.total
        return self._snap


SUB = 16   # histogram buckets per doubling: each is 2^(1/16) = 4.4% wide, so a value read back is within ±2.2%


class MinuteHist:
    """Every sample of one stage for one minute, in log buckets (metrics_store.py saves it).

    The rolling window above holds the last 4096 samples, which is ~1.5 s for recv_to_hub and minutes
    for send_to_render, so their p99s describe different periods. This counts EVERY sample of a fixed
    minute, and minutes add up: an hour's histogram is the sum of its minutes' bucket counts.

    Bucket k holds values in [2^(k/SUB), 2^((k+1)/SUB)) microseconds. Zero and negative samples (a
    cross-machine stage like venue_to_recv can read below zero when the clocks disagree) are kept
    separately instead of being dropped, so count always equals the number of samples recorded.
    """
    __slots__ = ("count", "sum", "min", "max", "pos", "neg", "zero")

    def __init__(self):
        self.count = 0
        self.sum = 0.0
        self.min = math.inf
        self.max = -math.inf
        self.pos: dict[int, int] = {}
        self.neg: dict[int, int] = {}   # buckets of -ms, for negative samples
        self.zero = 0

    def add(self, ms: float):
        self.count += 1
        self.sum += ms
        if ms < self.min:
            self.min = ms
        if ms > self.max:
            self.max = ms
        if ms > 0:
            k = math.floor(math.log2(ms * 1000.0) * SUB)
            self.pos[k] = self.pos.get(k, 0) + 1
        elif ms < 0:
            k = math.floor(math.log2(-ms * 1000.0) * SUB)
            self.neg[k] = self.neg.get(k, 0) + 1
        else:
            self.zero += 1


def bucket_mid_ms(k: int) -> float:
    """Geometric middle of bucket k, in ms: the estimate with the smallest worst-case relative error."""
    return 2.0 ** ((k + 0.5) / SUB) / 1000.0


def hist_percentile(q: float, pos: dict, neg: dict, zero: int, lo: float, hi: float) -> float | None:
    """Percentile q (0..1) from bucket counts. Clamped to the exact min/max, so p0 and p100 are exact."""
    n = sum(pos.values()) + sum(neg.values()) + zero
    if n == 0:
        return None
    rank = max(1, math.ceil(q * n))          # the rank-th smallest sample (nearest-rank definition)
    if rank >= n:
        return hi                            # the largest sample is known exactly
    if rank == 1:
        return lo                            # and so is the smallest
    seen = 0
    for k in sorted(neg, reverse=True):      # most negative first
        seen += neg[k]
        if seen >= rank:
            return min(max(-bucket_mid_ms(k), lo), hi)
    seen += zero
    if seen >= rank:
        return 0.0
    for k in sorted(pos):
        seen += pos[k]
        if seen >= rank:
            return min(max(bucket_mid_ms(k), lo), hi)
    return hi


class LatencyMonitor:
    def __init__(self):
        self.stages: dict[str, StageStats] = {}
        self.counters: dict[str, int] = {}
        self.minute: dict[str, MinuteHist] = {}   # this minute's histograms; metrics_store swaps it out
        self._cached: dict | None = None
        self._cached_at = 0.0

    def record(self, stage: str, ms: float):
        st = self.stages.get(stage)
        if st is None:
            st = self.stages[stage] = StageStats()
        st.add(ms)
        h = self.minute.get(stage)
        if h is None:
            h = self.minute[stage] = MinuteHist()
        h.add(ms)

    def take_minute(self) -> dict[str, "MinuteHist"]:
        """Hand over this minute's histograms and start fresh ones. O(1): one dict swap."""
        done, self.minute = self.minute, {}
        return done

    def incr(self, name: str, by: int = 1):
        self.counters[name] = self.counters.get(name, 0) + by

    def snapshot(self, force: bool = False) -> dict:
        """Percentiles for every stage. Cached so per-payload calls stay cheap."""
        now = time.monotonic()
        if force or self._cached is None or now - self._cached_at >= SNAPSHOT_TTL_S:
            self._cached = {
                "stages": {name: st.snapshot() for name, st in sorted(self.stages.items())},
                "counters": dict(self.counters),
            }
            self._cached_at = now
        return self._cached


async def loop_lag_probe(monitor: LatencyMonitor, interval: float = 0.1):
    """Schedule a sleep and record how late it wakes. Anything blocking the event loop shows up here."""
    loop = asyncio.get_running_loop()
    while True:
        expected = loop.time() + interval
        await asyncio.sleep(interval)
        monitor.record("loop_lag", max(0.0, (loop.time() - expected) * 1000))


def install_gc_probe(monitor: LatencyMonitor):
    """Record every garbage-collector pause as a 'gc_pause' stage (and per generation).

    A collection stops everything else in the process, so on the event loop it looks exactly
    like a slow message. cpumemory §7.1: measure before you tune.
    """
    started = [0]

    def on_gc(phase: str, info: dict):
        if phase == "start":
            started[0] = time.perf_counter_ns()
        else:
            ms = (time.perf_counter_ns() - started[0]) / 1_000_000
            monitor.record("gc_pause", ms)
            monitor.record(f"gc_pause.gen{info.get('generation', '?')}", ms)

    gc.callbacks.append(on_gc)


def freeze_long_lived() -> int:
    """Collect garbage once, then move every surviving object into the GC's permanent generation.

    CPython's cycle collector periodically re-walks every long-lived
    container object; for us that is ~90k market records, a ~50 ms stall each time. Market state
    lives for the whole run, so re-checking it finds nothing. After freeze(), full collections
    only walk objects created since the last freeze. Frozen objects are still freed normally
    by reference counting when we delete them (e.g. pruned markets); only cycle-checking stops.
    The collect() first makes sure we don't freeze garbage cycles that would then never be freed.
    """
    gc.collect()
    gc.freeze()
    return gc.get_freeze_count()


async def gc_freeze_loop(monitor: LatencyMonitor, first_after_s: float = 120.0, every_s: float = 600.0):
    """Freeze after warm-up (state is mostly built), then every 10 min for markets added since."""
    await asyncio.sleep(first_after_s)
    while True:
        t0 = time.perf_counter_ns()
        frozen = freeze_long_lived()
        ms = (time.perf_counter_ns() - t0) / 1_000_000
        monitor.record("gc_freeze", ms)
        print(f"  [GC] froze long-lived objects ({frozen:,} frozen, took {ms:.1f} ms)")
        await asyncio.sleep(every_s)
