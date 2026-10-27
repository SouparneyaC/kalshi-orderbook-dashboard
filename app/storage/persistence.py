"""
Trade persistence layer using DuckDB.

Stores every trade for crash recovery, historical replay, and analytical queries.
DuckDB is columnar and embedded: no server needed, fast analytical SQL.
"""

import threading
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parents[2] / "trades.duckdb"   # repo root

_conn: duckdb.DuckDBPyConnection | None = None
# One connection, used from several threads (the server flushes via asyncio.to_thread).
# DuckDB 2.x refuses a busy connection instead of waiting, so every use takes this lock.
_lock = threading.RLock()


def get_conn() -> duckdb.DuckDBPyConnection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = duckdb.connect(str(DB_PATH))
            _conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    ts_ms BIGINT,
                    ticker VARCHAR,
                    price DOUBLE,
                    count DOUBLE,
                    side VARCHAR,
                    book_side VARCHAR,
                    inserted_at TIMESTAMP DEFAULT current_timestamp
                )
            """)
            # When we received the trade (wall ms), next to Kalshi's ts_ms. Old rows stay NULL.
            _conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS recv_ts_ms BIGINT")
            _conn.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS trade_id VARCHAR")
        return _conn


def insert_trade(trade: dict):
    with _lock:
        get_conn().execute(
            "INSERT INTO trades (ts_ms, ticker, price, count, side, book_side, recv_ts_ms, trade_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                trade.get("ts"),
                trade.get("ticker"),
                float(trade["price"]) if trade.get("price") else None,
                float(trade["count"]) if trade.get("count") else None,
                trade.get("side"),
                trade.get("book_side"),
                trade.get("recv_ts"),
                trade.get("trade_id"),
            ],
        )


class TradeColumns:
    """Trades waiting to be written, kept the way DuckDB takes them: one list per column.

    (Drepper §6.2.1, §7.3.) We used to keep one dict per trade (~10
    objects, ~650 B each, up to 100k of them when the writer fell behind) and then walk that
    list 8 times in the writer thread to build these same columns, re-parsing price and count
    from text. Now ingest appends straight into the columns, numbers already parsed, so a
    100k backlog is ~10 MB instead of ~65 MB and the writer has nothing left to convert.
    """
    __slots__ = ("ts", "ticker", "price", "count", "side", "book_side", "recv_ts", "trade_id")

    def __init__(self):
        for name in self.__slots__:
            setattr(self, name, [])

    def append(self, ts, ticker, price, count, side, book_side, recv_ts, trade_id):
        self.ts.append(ts)
        self.ticker.append(ticker)
        self.price.append(price)
        self.count.append(count)
        self.side.append(side)
        self.book_side.append(book_side)
        self.recv_ts.append(recv_ts)
        self.trade_id.append(trade_id)

    def __len__(self) -> int:
        return len(self.ts)

    def columns(self) -> list[list]:
        return [getattr(self, name) for name in self.__slots__]

    def take(self) -> "TradeColumns":
        """Hand everything to the writer and start empty (swaps the lists, copies nothing)."""
        batch = TradeColumns()
        for name in self.__slots__:
            fresh = getattr(batch, name)
            setattr(batch, name, getattr(self, name))
            setattr(self, name, fresh)
        return batch

    def put_back(self, batch: "TradeColumns"):
        """A failed write: its trades go back in front of the ones that arrived meanwhile."""
        for name in self.__slots__:
            getattr(self, name)[:0] = getattr(batch, name)

    def drop_oldest(self, n: int):
        for name in self.__slots__:
            del getattr(self, name)[:n]


def rows_for_insert(trades: TradeColumns) -> list[tuple]:
    """The rows insert_trades_batch would write, in order (used by tests)."""
    return list(zip(*trades.columns()))


def insert_trades_batch(trades: TradeColumns):
    if not trades:
        return
    # One statement with a list per column: executemany costs ~19 ms/row on the throttled t4g.
    with _lock:
        get_conn().execute(
            """INSERT INTO trades (ts_ms, ticker, price, count, side, book_side, recv_ts_ms, trade_id)
               SELECT unnest(?::BIGINT[]), unnest(?::VARCHAR[]), unnest(?::DOUBLE[]), unnest(?::DOUBLE[]),
                      unnest(?::VARCHAR[]), unnest(?::VARCHAR[]), unnest(?::BIGINT[]), unnest(?::VARCHAR[])""",
            trades.columns(),
        )


def query_recent(ticker: str | None = None, limit: int = 100) -> list[dict]:
    with _lock:
        conn = get_conn()
        if ticker:
            result = conn.execute(
                "SELECT ts_ms, ticker, price, count, side, book_side FROM trades WHERE ticker = ? ORDER BY ts_ms DESC LIMIT ?",
                [ticker, limit],
            )
        else:
            result = conn.execute(
                "SELECT ts_ms, ticker, price, count, side, book_side FROM trades ORDER BY ts_ms DESC LIMIT ?",
                [limit],
            )
        rows = result.fetchall()
    cols = ["ts_ms", "ticker", "price", "count", "side", "book_side"]
    return [dict(zip(cols, row)) for row in rows]


def trade_count() -> int:
    with _lock:
        return get_conn().execute("SELECT count(*) FROM trades").fetchone()[0]


def volume_summary(limit: int = 20) -> list[dict]:
    with _lock:
        rows = get_conn().execute("""
            SELECT ticker, sum(count) as total_volume, count(*) as num_trades,
                   avg(price) as avg_price
            FROM trades
            WHERE count IS NOT NULL
            GROUP BY ticker
            ORDER BY total_volume DESC
            LIMIT ?
        """, [limit]).fetchall()
    cols = ["ticker", "total_volume", "num_trades", "avg_price"]
    return [dict(zip(cols, row)) for row in rows]
