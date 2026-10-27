"""Trades kept as columns must reach DuckDB exactly as the old dict rows did."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.storage import persistence  # noqa: E402
from app.storage.persistence import TradeColumns  # noqa: E402


def sample(n, start=0):
    t = TradeColumns()
    for i in range(start, start + n):
        t.append(1_700_000_000_000 + i, f"KX-{i % 7}", (i % 9999 + 1) / 10000 if i % 5 else None,
                 float(i % 300 + 1), "yes" if i % 2 else "no", "bid", 1_700_000_000_040 + i, f"id-{i}")
    return t


class TradeColumnsTest(unittest.TestCase):
    def test_take_put_back_drop(self):
        t = sample(10)
        batch = t.take()
        self.assertEqual((len(t), len(batch)), (0, 10))
        t.append(*persistence.rows_for_insert(sample(1, 10))[0])
        t.put_back(batch)                                   # failed write: old ones go first again
        self.assertEqual([r[-1] for r in persistence.rows_for_insert(t)], [f"id-{i}" for i in range(11)])
        t.drop_oldest(4)
        self.assertEqual(persistence.rows_for_insert(t)[0][-1], "id-4")
        self.assertEqual(len(t), 7)

    def test_insert_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            persistence.DB_PATH = Path(d) / "t.duckdb"
            persistence._conn = None
            rows = sample(2500)
            persistence.insert_trades_batch(rows)
            got = persistence.get_conn().execute(
                "SELECT ts_ms, ticker, price, count, side, book_side, recv_ts_ms, trade_id FROM trades ORDER BY ts_ms"
            ).fetchall()
            persistence._conn.close()
            persistence._conn = None
        self.assertEqual(got, persistence.rows_for_insert(rows))


if __name__ == "__main__":
    unittest.main()
