"""
Sequence-gap detection for the orderbook_delta subscription.

Observed on the live Kalshi feed (2026-10-24, 506 messages across 3 markets):
- `seq` is top-level, next to `type` and `sid`.
- It is ONE counter per subscription (sid), shared by all markets on it, and it advances on
  every message on that sid: orderbook_snapshot, orderbook_delta, and the `ok` reply to our own
  update_subscription commands. The `subscribed` reply carries no seq.
- Snapshots do not restart it (initial snapshots were seq 1, 2; an add_markets snapshot 228;
  a get_snapshot reply 372).

So a gap (seq != last + 1) means some message on the sid was lost, and we cannot tell which
market it belonged to: every book on that sid must be re-snapshotted. Deltas are additive
(orderbook.py _Side.add), so one lost delta leaves a book wrong until it is reloaded.
"""


class BookSeqTracker:
    """Per-subscription seq check plus the queue of snapshot requests it triggers.

    The per-message cost is one int compare (`observe`). Requests are de-duplicated: a market
    whose snapshot is already in flight is not asked for again until that snapshot arrives.
    """
    __slots__ = ("last", "pending", "in_flight", "gaps")

    def __init__(self):
        self.last: int | None = None
        self.pending: set[str] = set()     # tickers to request a snapshot for
        self.in_flight: set[str] = set()   # requested, snapshot not yet received
        self.gaps = 0

    def reset(self):
        """New connection or new subscription: forget everything."""
        self.last = None
        self.pending.clear()
        self.in_flight.clear()

    def observe(self, seq: int) -> bool:
        """Record one message's seq. Returns True if one or more messages were skipped."""
        last = self.last
        self.last = seq
        if last is not None and seq != last + 1:
            self.gaps += 1
            return True
        return False

    def request(self, tickers) -> None:
        """Queue snapshot requests, skipping markets already waiting for one."""
        for t in tickers:
            if t not in self.in_flight:
                self.pending.add(t)

    def take_pending(self) -> list[str]:
        """Hand the queued tickers to the sender and mark them in flight."""
        batch = sorted(self.pending)
        self.pending.clear()
        self.in_flight.update(batch)
        return batch

    def snapshot_received(self, ticker: str) -> None:
        self.in_flight.discard(ticker)

    def forget(self, ticker: str) -> None:
        """Market demoted: drop any request state for it."""
        self.pending.discard(ticker)
        self.in_flight.discard(ticker)
