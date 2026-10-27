"""
Browser broadcast: manages WebSocket connections to dashboard viewers.
"""
import asyncio
import time

import orjson

from app.core.latency import now_ms, perf_ns, ns_to_ms
from app.core import market_state

# --- Browser clients ---
browser_clients: set = set()   # BrowserClient objects (tests may add plain markers)
update_queue: asyncio.Queue = asyncio.Queue(maxsize=50)

# Per-browser sending. broadcast_loop used to await each browser's send in turn, so
# one stuck tab (laptop asleep, network gone: the send blocks until TCP gives up, minutes) froze
# every browser and filled update_queue (97,018 dropped, p99 recv_to_send 692 s on 2026-10-25).
CLIENT_QUEUE_MAX = 20       # deltas buffered per browser before it is resynced with a full snapshot
SEND_TIMEOUT_S = 5.0        # a single send slower than this means the browser is gone: close it


class BrowserClient:
    """One dashboard tab: its own queue and sender task, so it can never block the others."""

    def __init__(self, ws):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        self.needs_full = True          # first thing every browser gets is a full snapshot
        self.closed = False

    def offer(self, message: str, trigger_recv_ns: int | None) -> None:
        """Non-blocking hand-off from broadcast_loop."""
        if self.closed:
            return
        try:
            self.queue.put_nowait((message, trigger_recv_ns))
        except asyncio.QueueFull:
            # Too far behind: deltas can't be dropped (rows would go stale), so throw the backlog
            # away and send one full snapshot instead.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.needs_full = True
            self.queue.put_nowait(None)     # wake the sender
            market_state.latency.incr("client_resyncs")

    async def run(self) -> None:
        """Sender task: full snapshot first (and after any overflow), then deltas in order."""
        try:
            while not self.closed:
                item = await self.queue.get()
                if self.needs_full:
                    self.needs_full = False
                    while not self.queue.empty():   # the snapshot covers anything queued so far
                        self.queue.get_nowait()
                    full = market_state.build_full_payload()
                    full["server_ts"] = now_ms()
                    await self._send(encode(full))
                    continue
                if item is None:
                    continue
                message, trigger_recv_ns = item
                await self._send(message)
                if trigger_recv_ns is not None:
                    market_state.latency.record("recv_to_send", ns_to_ms(perf_ns() - trigger_recv_ns))
        except Exception:
            pass
        finally:
            await self.close()

    async def _send(self, text: str) -> None:
        try:
            await asyncio.wait_for(self.ws.send_text(text), SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            market_state.latency.incr("client_send_timeouts")
            raise

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        browser_clients.discard(self)
        try:
            await asyncio.wait_for(self.ws.close(), 1.0)
        except Exception:
            pass
        print(f"  Browser disconnected ({len(browser_clients)} total)")

_last_enqueue_time: float = 0.0
ENQUEUE_MIN_INTERVAL: float = 0.1  # 100ms

# Trailing edge of the throttle: a call that lands inside the window schedules ONE flush for when
# the window ends, so the last change of a burst is not stranded in dirty_markets until some later
# Kalshi message happens to trigger another enqueue.
_trailing_handle: asyncio.TimerHandle | None = None
_trailing_trigger_ns: int | None = None  # earliest suppressed trigger, so recv_to_send stays honest


def encode(payload: dict) -> str:
    """Payload dict → JSON text. One name so the benchmark measures exactly what we send.
    orjson writes the bytes in one C pass; json.dumps builds many small strings."""
    return orjson.dumps(payload).decode()


def enqueue_update(trigger_recv_ns: int | None = None):
    """Build a delta payload and put it on the queue. Drops if queue is full (never blocks ingestion).

    trigger_recv_ns: perf stamp of the Kalshi message that caused this push, for recv_to_send.
    """
    global _last_enqueue_time, _trailing_handle, _trailing_trigger_ns
    if not browser_clients:
        return
    now = time.monotonic()
    wait = ENQUEUE_MIN_INTERVAL - (now - _last_enqueue_time)
    if wait > 0:
        if trigger_recv_ns is not None and (_trailing_trigger_ns is None or trigger_recv_ns < _trailing_trigger_ns):
            _trailing_trigger_ns = trigger_recv_ns
        if _trailing_handle is None:
            try:
                _trailing_handle = asyncio.get_running_loop().call_later(wait, _flush_trailing)
            except RuntimeError:
                pass  # no running loop (tests, bench): nothing to schedule on
        return
    _last_enqueue_time = now
    # This send covers everything a pending trailing flush would have sent.
    if _trailing_handle is not None:
        _trailing_handle.cancel()
        _trailing_handle = None
    if _trailing_trigger_ns is not None:
        if trigger_recv_ns is None or _trailing_trigger_ns < trigger_recv_ns:
            trigger_recv_ns = _trailing_trigger_ns
        _trailing_trigger_ns = None
    try:
        t0 = perf_ns()
        payload = market_state.build_delta_payload()
        t1 = perf_ns()
        market_state.latency.record("build", ns_to_ms(t1 - t0))
        market_state.cpu_ns["payload_build"] = market_state.cpu_ns.get("payload_build", 0) + t1 - t0
        # Skip if nothing changed and no recent trades
        if not payload["markets_changed"] and not payload["markets_removed"] and not payload["recent_trades"]:
            return
        update_queue.put_nowait((payload, trigger_recv_ns, t1))
        market_state.latency.record("queue_depth", update_queue.qsize())
    except asyncio.QueueFull:
        market_state.latency.incr("payloads_dropped")


def _flush_trailing():
    """Runs once at the end of a throttle window in which at least one enqueue was suppressed."""
    global _trailing_handle, _trailing_trigger_ns
    _trailing_handle = None
    trigger, _trailing_trigger_ns = _trailing_trigger_ns, None
    enqueue_update(trigger)


async def broadcast_loop():
    """Encodes each delta once and hands it to every browser's own queue. Never waits on a browser."""
    while True:
        payload, trigger_recv_ns, enqueued_ns = await update_queue.get()
        dequeued_ns = perf_ns()
        market_state.latency.record("queue_wait", ns_to_ms(dequeued_ns - enqueued_ns))
        payload["server_ts"] = now_ms()
        if not browser_clients:
            continue
        message = encode(payload)
        for client in list(browser_clients):
            if isinstance(client, BrowserClient):
                client.offer(message, trigger_recv_ns)
        market_state.latency.record("serialize_send", ns_to_ms(perf_ns() - dequeued_ns))
