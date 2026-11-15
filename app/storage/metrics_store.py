"""
Live measurement history: one row of histograms and counters per minute, kept in DuckDB.

Once a minute, on the wall-clock minute, the event loop swaps out latency.minute (every sample of
every stage for that minute, in log buckets) plus a snapshot of the counters, and hands both to ONE
writer thread. The thread owns its own DuckDB connection to metrics.duckdb (never the trade DB) and
does everything slow: reading /proc, asking chrony for the clock offset, inserting. The loop's share
is a dict swap and a counter snapshot.

Tables:
  runs           one row per process start: commit, settings. A restart is a row, not a silent gap.
  stage_minute   per stage per minute: count, sum, exact min/max, p50/p90/p99/p99.9 read from the
                 buckets (±2.2%), and the bucket counts themselves so minutes can be merged exactly.
  minute_metric  per minute, long format (name, value): load (msgs/s, trades/s...), levels (books,
                 tabs...), process CPU, steal, RSS, clock offset, and self-checks (name 'check.*').

Every row carries boot_id and commit, so "before/after commit X" is a query, not a hand-copied table.
"""
import asyncio
import gc
import json
import os
import platform
import queue
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from app.core.latency import LatencyMonitor, MinuteHist, SUB, hist_percentile

DB_PATH = Path(os.environ.get("METRICS_DB", Path(__file__).resolve().parents[2] / "metrics.duckdb"))
ENABLED = os.environ.get("METRICS", "1") != "0"

# Stages whose samples compare two machines' clocks, and stages stamped in whole milliseconds.
# Stored on every row so nobody reads a sub-millisecond change in venue_to_recv as real.
CROSS_MACHINE = ("venue_to_recv",)
WHOLE_MS = ("venue_to_recv", "send_to_render")

SCHEMA = """
CREATE TABLE IF NOT EXISTS book_audit (
    boot_id VARCHAR, commit VARCHAR, audited_at TIMESTAMPTZ, ticker VARCHAR, levels_compared INTEGER,
    levels_wrong INTEGER, worst_qty_error DOUBLE, book_age_s DOUBLE
);
CREATE TABLE IF NOT EXISTS runs (
    boot_id VARCHAR PRIMARY KEY, started_at TIMESTAMPTZ, commit VARCHAR, host VARCHAR,
    python VARCHAR, settings JSON
);
CREATE TABLE IF NOT EXISTS stage_minute (
    boot_id VARCHAR, commit VARCHAR, minute TIMESTAMPTZ,
    interval_start_ns BIGINT, interval_end_ns BIGINT,
    stage VARCHAR, count BIGINT, sum_ms DOUBLE, min_ms DOUBLE, max_ms DOUBLE,
    p50_ms DOUBLE, p90_ms DOUBLE, p99_ms DOUBLE, p999_ms DOUBLE,
    sub_per_doubling INTEGER, pos_keys INTEGER[], pos_counts BIGINT[],
    neg_keys INTEGER[], neg_counts BIGINT[], zero_count BIGINT,
    cross_machine BOOLEAN, whole_ms BOOLEAN
);
CREATE TABLE IF NOT EXISTS minute_metric (
    boot_id VARCHAR, commit VARCHAR, minute TIMESTAMPTZ, name VARCHAR, value DOUBLE
);
"""


def git_commit() -> str:
    """The commit this process runs. GIT_COMMIT wins (set it when the code is not a git checkout)."""
    if os.environ.get("GIT_COMMIT"):
        return os.environ["GIT_COMMIT"]
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=Path(__file__).parent,
                             capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5)
        sha = out.stdout.strip() or "unknown"
        return sha + ("-dirty" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"


def _read_proc_stat() -> tuple[int, int] | None:
    """(total, steal) jiffies over all CPUs, Linux only."""
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        return sum(vals[:8]), vals[7]
    except Exception:
        return None


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    try:
        import resource   # macOS fallback: peak, not current
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    except Exception:
        return None


def _clock_offset_ms() -> float | None:
    """Server clock minus NTP time, from chrony ('System time: 0.000001 seconds fast')."""
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            if line.startswith("System time"):
                v = float(line.split(":")[1].split()[0]) * 1000
                return v if "fast" in line else -v
    except Exception:
        pass
    return None


def stage_rows(boot_id, commit, minute_s, start_ns, end_ns, hists: dict[str, MinuteHist]) -> list[tuple]:
    rows = []
    for stage, h in sorted(hists.items()):
        if not h.count:
            continue
        pct = [hist_percentile(q, h.pos, h.neg, h.zero, h.min, h.max) for q in (0.50, 0.90, 0.99, 0.999)]
        pk = sorted(h.pos)
        nk = sorted(h.neg)
        base = stage.split(".")[0]
        rows.append((
            boot_id, commit, minute_s, start_ns, end_ns, stage, h.count, h.sum, h.min, h.max, *pct,
            SUB, pk, [h.pos[k] for k in pk], nk, [h.neg[k] for k in nk], h.zero,
            base in CROSS_MACHINE, base in WHOLE_MS,
        ))
    return rows


class MetricsStore:
    """Owns the per-minute swap (event loop) and the writer thread (DuckDB)."""

    def __init__(self, monitor: LatencyMonitor, context_fn, settings: dict, db_path: Path = DB_PATH):
        self.monitor = monitor
        self.context_fn = context_fn      # () -> {"counters": {...monotonic...}, "levels": {...}}
        self.settings = settings
        self.db_path = Path(db_path)
        self.boot_id = uuid.uuid4().hex[:12]
        self.commit = git_commit()
        self.q: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.last_flush_ms: float | None = None   # written by the thread, read by the loop: a float swap
        self.flush_errors = 0
        self.minutes_written = 0

    # ---- event loop side -------------------------------------------------------------------------

    def start(self):
        self.thread = threading.Thread(target=self._writer, name="metrics-writer", daemon=True)
        self.thread.start()
        self.q.put(("run", time.time_ns()))

    def swap(self, start_ns: int, end_ns: int) -> tuple:
        """Everything the loop does once a minute. Kept tiny on purpose."""
        t0 = time.perf_counter_ns()
        # A GC pause is itself recorded as a sample (gc_probe). With GC on, one could land between
        # the swap and the totals read and fail the count self-check by 1, so GC waits ~0.1 ms.
        gc_was_on = gc.isenabled()
        gc.disable()
        try:
            hists = self.monitor.take_minute()
            stage_totals = {name: st.total for name, st in self.monitor.stages.items()}
        finally:
            if gc_was_on:
                gc.enable()
        ctx = self.context_fn()
        item = ("minute", start_ns, end_ns, hists, stage_totals, ctx, time.process_time_ns(),
                self.last_flush_ms, time.perf_counter_ns() - t0)
        self.q.put(item)
        return item

    async def run(self):
        """Swap on every wall-clock minute boundary. The first partial minute starts now."""
        self.monitor.take_minute()             # samples from before the store started are not a minute
        self._start_ns = time.time_ns()
        while True:
            now = time.time()
            await asyncio.sleep(60 - (now % 60) + 0.0005)
            end_ns = time.time_ns()
            self.swap(self._start_ns, end_ns)
            self._start_ns = end_ns

    def stop(self, timeout: float = 5.0):
        """Save the last partial minute (marked by its short interval_s), then end the thread."""
        if getattr(self, "_start_ns", None) is not None:
            self.swap(self._start_ns, time.time_ns())
            self._start_ns = None
        self.q.put(None)
        if self.thread:
            self.thread.join(timeout)

    # ---- writer thread ---------------------------------------------------------------------------

    def _writer(self):
        import duckdb
        con = duckdb.connect(str(self.db_path))
        con.execute(SCHEMA)
        prev = None   # (stage_totals, counters, cpu_ns, end_ns, proc_stat)
        while True:
            item = self.q.get()
            if item is None:
                break
            try:
                t0 = time.perf_counter_ns()
                if item[0] == "run":
                    con.execute("INSERT INTO runs VALUES (?, to_timestamp(?), ?, ?, ?, ?)",
                                [self.boot_id, item[1] / 1e9, self.commit, socket.gethostname(),
                                 platform.python_version(), json.dumps(self.settings)])
                    continue
                prev = self._write_minute(con, item, prev)
                self.minutes_written += 1
                self.last_flush_ms = (time.perf_counter_ns() - t0) / 1e6
            except Exception as e:   # never let a bad minute kill the thread
                self.flush_errors += 1
                print(f"  [METRICS] write failed: {e!r}")
        con.close()

    def _write_minute(self, con, item, prev):
        _, start_ns, end_ns, hists, stage_totals, ctx, cpu_ns, last_flush_ms, swap_ns = item
        # The minute is named by its nominal start (the wall-clock minute it belongs to); the exact
        # interval is stored too, because the swap runs a little after the boundary (loop lag).
        minute_s = (end_ns // 1_000_000_000 - 1) // 60 * 60
        interval_s = (end_ns - start_ns) / 1e9
        con.executemany(
            "INSERT INTO stage_minute VALUES (?,?,to_timestamp(?),?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            stage_rows(self.boot_id, self.commit, minute_s, start_ns, end_ns, hists))

        m: dict[str, float] = {"interval_s": interval_s, "swap_ms": swap_ns / 1e6}
        if last_flush_ms is not None:
            m["metrics_flush_ms"] = last_flush_ms     # the previous minute's write
        for k, v in ctx.get("levels", {}).items():
            if v is not None:
                m[k] = float(v)
        proc = _read_proc_stat()
        rss = _rss_mb()
        if rss is not None:
            m["rss_mb"] = rss
        off = _clock_offset_ms()
        if off is not None:
            m["clock_offset_ms"] = off

        counters = ctx.get("counters", {})
        if prev is not None:
            p_totals, p_counters, p_cpu, p_end, p_proc = prev
            dt = (end_ns - p_end) / 1e9
            for k, v in counters.items():
                d = v - p_counters.get(k, 0)
                if d < 0:          # a counter went backwards: only a restart does that, and a
                    d = v          # restart makes a new boot_id, so flag it rather than guess
                    m["check.counter_reset"] = m.get("check.counter_reset", 0) + 1
                m[f"{k}_per_s"] = d / dt if dt > 0 else 0.0
                m[f"{k}_delta"] = d
            m["process_cpu_pct"] = (cpu_ns - p_cpu) / 1e9 / dt * 100 if dt > 0 else 0.0
            if proc and p_proc and proc[0] > p_proc[0]:
                m["steal_pct"] = (proc[1] - p_proc[1]) / (proc[0] - p_proc[0]) * 100
            # Self-check: every sample in a histogram is also counted in its stage's lifetime total,
            # so the two must agree exactly. A mismatch means samples were lost or double-counted.
            mismatch = 0
            for name, total in stage_totals.items():
                expect = total - p_totals.get(name, 0)
                got = hists[name].count if name in hists else 0
                mismatch += abs(expect - got)
            m["check.hist_count_mismatch"] = mismatch
            m["check.gap_s"] = (start_ns - p_end) / 1e9      # 0 when minutes are contiguous
        else:
            m["check.first_minute"] = 1   # rates need a previous minute; the first has none
        audits = ctx.get("audit_rows")
        if audits:
            con.executemany("INSERT INTO book_audit VALUES (?,?,to_timestamp(?),?,?,?,?,?)",
                            [(self.boot_id, self.commit, r[0] / 1000, *r[1:]) for r in audits])
        con.executemany("INSERT INTO minute_metric VALUES (?,?,to_timestamp(?),?,?)",
                        [(self.boot_id, self.commit, minute_s, k, float(v)) for k, v in sorted(m.items())])
        return stage_totals, counters, cpu_ns, end_ns, proc
