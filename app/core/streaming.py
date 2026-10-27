"""
Streaming algorithms for real-time market intelligence.

Based on Chapter 6 of "Foundations of Data Science" (Blum, Hopcroft, Kannan):
- Misra-Gries: find heavy-hitter markets by trade volume
- Second Moment: estimate concentration of activity across markets
- Distinct Elements: count active markets in O(log m) space
"""

import random
import time
from collections import defaultdict, deque
from itertools import islice


class MisraGries:
    """
    Finds frequent elements (heavy hitters) in a stream.

    Maintains k counters. After processing n items, any element with
    frequency > n/(k+1) is guaranteed to be in the counter set.
    Elements in the set have estimated counts within n/(k+1) of true count.

    We use this to identify which markets are trading most actively,
    so we can promote them to full orderbook subscriptions.
    """

    def __init__(self, k: int = 50):
        self.k = k
        self.counters: dict[str, float] = {}
        self.total = 0

    def add(self, item: str, weight: float = 1.0):
        self.total += weight
        if item in self.counters:
            self.counters[item] += weight
        elif len(self.counters) < self.k:
            self.counters[item] = weight
        else:
            # The counters are decremented in place and only the ones that hit
            # zero are deleted. The old code built a brand-new dict here on most trades.
            counters = self.counters
            min_val = min(counters.values())
            dec = weight if weight <= min_val else min_val
            gone = []
            for key, v in counters.items():
                v -= dec
                if v > 0:
                    counters[key] = v      # same key, so the dict does not resize mid-loop
                else:
                    gone.append(key)
            for key in gone:
                del counters[key]
            if weight > min_val:
                counters[item] = weight - min_val

    def top(self, n: int | None = None) -> list[tuple[str, float]]:
        items = sorted(self.counters.items(), key=lambda x: x[1], reverse=True)
        if n is not None:
            items = items[:n]
        return items

    def contains(self, item: str) -> bool:
        return item in self.counters

    def decay(self, factor: float = 0.9, threshold: float = 1.0):
        """Multiply all counters by factor, remove any below threshold."""
        self.total *= factor
        to_remove = []
        for key in self.counters:
            self.counters[key] *= factor
            if self.counters[key] < threshold:
                to_remove.append(key)
        for key in to_remove:
            del self.counters[key]

    def reset(self):
        self.counters.clear()
        self.total = 0


class SecondMomentEstimator:
    """
    Estimates the second frequency moment (Σ f_s²) of a stream.

    Uses random ±1 hash per symbol, maintains running sum.
    The square of the sum is an unbiased estimator of Σf².

    We use multiple independent estimators and average for accuracy.

    The concentration ratio Σf² / (Σf)² × m tells us whether
    activity is concentrated (few hot markets) or dispersed (many active).
    """

    def __init__(self, num_estimators: int = 8):
        self.num_estimators = num_estimators
        self.sums = [0.0] * num_estimators
        self.seeds = [random.randint(0, 2**32) for _ in range(num_estimators)]
        self.total_count = 0

    def _hash_sign(self, item: str, seed_idx: int) -> int:
        # Built-in hash: stable within the process, which is all the sketch needs.
        # (The old md5 + per-item cache grew by 8 entries per market, forever.)
        return 1 if hash((self.seeds[seed_idx], item)) & 1 else -1

    def add(self, item: str, weight: float = 1.0):
        self.total_count += weight
        for i in range(self.num_estimators):
            self.sums[i] += self._hash_sign(item, i) * weight

    def estimate(self) -> float:
        if self.total_count == 0:
            return 0.0
        estimates = [s * s for s in self.sums]
        estimates.sort()
        mid = len(estimates) // 2
        return estimates[mid]

    def concentration_ratio(self) -> float | None:
        """
        Returns a value between 0 and 1.
        Close to 0 = activity spread across many markets.
        Close to 1 = activity concentrated in few markets.
        """
        if self.total_count == 0:
            return None
        f2 = self.estimate()
        return f2 / (self.total_count ** 2)

    def decay(self, factor: float = 0.9):
        """Decay all running sums to fade old data."""
        self.total_count *= factor
        for i in range(self.num_estimators):
            self.sums[i] *= factor

    def reset(self):
        self.sums = [0.0] * self.num_estimators
        self.total_count = 0
        self.seeds = [random.randint(0, 2**32) for _ in range(self.num_estimators)]


class DistinctCounter:
    """
    Estimates the number of distinct elements in a stream using O(log m) space.

    Tracks the minimum hash value seen. For d distinct elements uniformly
    hashed to [0, M), the expected minimum is M/(d+1), so d ≈ M/min - 1.

    Uses multiple independent hash functions and takes the median for accuracy.
    """

    def __init__(self, num_hashes: int = 16):
        self.num_hashes = num_hashes
        self.M = 2**32
        self.mins = [self.M] * num_hashes
        self.seeds = [random.randint(0, 2**32) for _ in range(num_hashes)]

    def add(self, item: str):
        # Python's built-in hash instead of MD5 (same fix SecondMomentEstimator got).
        # MD5 made ~100 throwaway objects per trade; hash() of a (seed, item) tuple makes none we keep.
        for i in range(self.num_hashes):
            h = hash((self.seeds[i], item)) & 0xFFFFFFFF
            if h < self.mins[i]:
                self.mins[i] = h

    def estimate(self) -> int:
        estimates = []
        for min_val in self.mins:
            if min_val < self.M:
                est = max(1, int(self.M / (min_val + 1)) - 1)
                estimates.append(est)
        if not estimates:
            return 0
        estimates.sort()
        return estimates[len(estimates) // 2]

    def reset(self):
        self.mins = [self.M] * self.num_hashes
        self.seeds = [random.randint(0, 2**32) for _ in range(self.num_hashes)]


class WindowedTradeBuffer:
    """
    Fixed-size circular buffer of recent trades.
    Keeps the last N trades for display without unbounded memory growth.
    """

    def __init__(self, maxlen: int = 200):
        self.maxlen = maxlen
        # A deque with maxlen drops the oldest trade by itself, in O(1).
        # The old list was re-sliced into a brand-new 500-element list on every trade.
        self.trades: deque[dict] = deque(maxlen=maxlen)

    def add(self, trade: dict):
        self.trades.append(trade)

    def recent(self, n: int = 50) -> list[dict]:
        k = len(self.trades)
        return list(islice(self.trades, max(0, k - n), k))

    def clear(self):
        self.trades.clear()
