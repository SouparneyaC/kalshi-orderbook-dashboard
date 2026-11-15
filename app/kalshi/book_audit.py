"""
Book accuracy audit: is our order book exactly the book Kalshi holds?

Seq-gap detection (book_seq.py) catches a lost message. It cannot catch a book that went wrong some
other way. So every few seconds one book is checked against Kalshi itself: we ask for a get_snapshot
of it and compare every level with ours in the moment before the snapshot is loaded.

The comparison needs no tolerance for timing. The reply comes on the same subscription as the deltas,
in seq order, so when it arrives we have applied exactly the deltas that came before it. If the book
is right the two are identical.

The rotation picks the book audited longest ago, and a book gets no audit until its first snapshot
has loaded, since an empty book waiting for one is not wrong. Loading the snapshot also repairs a
wrong book, so a mismatch is counted once and shows up as a row, not as a lasting error.
"""
import os
from collections import deque

from app.core.orderbook import to_hundredths, to_ticks

INTERVAL_MS = int(float(os.environ.get("BOOK_AUDIT_INTERVAL", "5")) * 1000)   # 0 turns it off
LOST_AFTER_MS = 30_000
MAX_ROWS = 10_000     # rows waiting for the minute write


def side_levels(pairs) -> dict[int, int]:
    """Kalshi's [price, qty] strings as {ticks: hundredths}."""
    return {to_ticks(p): to_hundredths(q) for p, q in pairs or ()}


def diff_levels(ours: dict[int, int], theirs: dict[int, int]) -> tuple[int, int, int]:
    """(levels compared, levels wrong, worst quantity error in hundredths)."""
    compared = wrong = worst = 0
    for p in ours.keys() | theirs.keys():
        compared += 1
        err = abs(ours.get(p, 0) - theirs.get(p, 0))
        if err:
            wrong += 1
            worst = max(worst, err)
    return compared, wrong, worst


class BookAudit:
    __slots__ = ("ready", "in_flight", "rows", "next_ms", "interval_ms")

    def __init__(self, interval_ms: int = INTERVAL_MS):
        self.ready: dict[str, dict] = {}        # ticker -> {"loaded_ms", "audited_ms"}
        self.in_flight: dict[str, int] = {}     # ticker -> when requested
        self.rows: deque[tuple] = deque(maxlen=MAX_ROWS)
        self.interval_ms = interval_ms
        self.next_ms = 0

    def reset(self):
        """New connection or subscription: nothing we held can be compared any more."""
        self.ready.clear()
        self.in_flight.clear()

    def forget(self, ticker: str):
        """Market left the book set, or came back with a new empty book."""
        self.ready.pop(ticker, None)
        self.in_flight.pop(ticker, None)

    def due(self, now_ms: int) -> bool:
        return self.interval_ms > 0 and now_ms >= self.next_ms

    def next_ticker(self, books, now_ms: int) -> tuple[str | None, int]:
        """The book to audit now, and how many requests were given up on as lost."""
        self.next_ms = now_ms + self.interval_ms
        lost = [t for t, at in self.in_flight.items() if now_ms - at > LOST_AFTER_MS]
        for t in lost:
            del self.in_flight[t]
        for t in [t for t in self.ready if t not in books]:
            self.forget(t)
        waiting = [t for t in self.ready if t not in self.in_flight]
        if not waiting:
            return None, len(lost)
        ticker = min(waiting, key=lambda t: self.ready[t]["audited_ms"])
        self.in_flight[ticker] = now_ms
        return ticker, len(lost)

    def on_snapshot(self, ticker: str, book, msg: dict, now_ms: int) -> tuple | None:
        """Call BEFORE load_snapshot. Returns a result row if this snapshot was an audit."""
        row = None
        if self.in_flight.pop(ticker, None) is not None and ticker in self.ready:
            yes = diff_levels(dict(zip(book.yes.px, book.yes.qty)), side_levels(msg.get("yes_dollars_fp")))
            no = diff_levels(dict(zip(book.no.px, book.no.qty)), side_levels(msg.get("no_dollars_fp")))
            row = (now_ms, ticker, yes[0] + no[0], yes[1] + no[1], max(yes[2], no[2]) / 100,
                   (now_ms - self.ready[ticker]["loaded_ms"]) / 1000)
            self.rows.append(row)
        prev = self.ready.get(ticker)
        self.ready[ticker] = {"loaded_ms": now_ms, "audited_ms": now_ms if row or not prev else prev["audited_ms"]}
        return row

    def take_rows(self) -> list[tuple]:
        rows = list(self.rows)
        self.rows.clear()
        return rows
