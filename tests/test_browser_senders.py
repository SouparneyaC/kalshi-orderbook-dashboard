"""
Per-browser senders (broadcast.BrowserClient): one stuck tab must not delay the others, a tab that
falls too far behind gets a fresh full snapshot instead of a backlog, and a send that hangs closes
that tab.
"""
import asyncio
import json
import os
import unittest

os.environ.setdefault("KALSHI_KEY_ID", "test")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "unused")

from app.server import broadcast
from app.core import market_state


class FakeWS:
    def __init__(self, hang=False):
        self.sent, self.hang, self.closed = [], hang, False

    async def send_text(self, text):
        if self.hang:
            await asyncio.sleep(3600)     # a dead TCP connection: the send never completes
        self.sent.append(json.loads(text))

    async def close(self):
        self.closed = True


class BrowserSenderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = (broadcast.SEND_TIMEOUT_S, broadcast.CLIENT_QUEUE_MAX, market_state.build_full_payload)
        broadcast.SEND_TIMEOUT_S = 0.2
        market_state.build_full_payload = lambda: {"type": "full_state", "markets": []}
        broadcast.browser_clients.clear()
        # update_queue is module-level and binds to the first event loop that uses it; each
        # IsolatedAsyncioTestCase test runs its own loop, so give every test a fresh queue.
        self._orig_queue = broadcast.update_queue
        broadcast.update_queue = asyncio.Queue(maxsize=50)
        self.loop_task = asyncio.create_task(broadcast.broadcast_loop())

    async def asyncTearDown(self):
        self.loop_task.cancel()
        for c in list(broadcast.browser_clients):
            if isinstance(c, broadcast.BrowserClient):
                await c.close()
        broadcast.browser_clients.clear()
        broadcast.update_queue = self._orig_queue
        broadcast.SEND_TIMEOUT_S, broadcast.CLIENT_QUEUE_MAX, market_state.build_full_payload = self._orig

    def connect(self, ws):
        c = broadcast.BrowserClient(ws)
        broadcast.browser_clients.add(c)
        t = asyncio.create_task(c.run())
        c.queue.put_nowait(None)
        return c, t

    async def publish(self, i):
        await broadcast.update_queue.put(({"type": "delta", "n": i, "markets_changed": []}, None, 0))
        await asyncio.sleep(0)

    async def test_stuck_tab_does_not_delay_a_healthy_one(self):
        good, bad = FakeWS(), FakeWS(hang=True)
        self.connect(bad)
        self.connect(good)
        await asyncio.sleep(0.01)                  # let the healthy tab get its full snapshot first
        for i in range(5):
            await self.publish(i)
        await asyncio.sleep(0.05)                  # far less than the stuck send would take
        self.assertEqual(good.sent[0]["type"], "full_state")
        self.assertEqual([m["n"] for m in good.sent[1:]], [0, 1, 2, 3, 4])

    async def test_hung_send_closes_that_tab(self):
        bad = FakeWS(hang=True)
        c, t = self.connect(bad)
        await asyncio.sleep(0.4)                   # > SEND_TIMEOUT_S
        self.assertTrue(c.closed)
        self.assertTrue(bad.closed)
        self.assertNotIn(c, broadcast.browser_clients)

    async def test_overflow_resyncs_with_a_full_snapshot(self):
        ws = FakeWS()
        c = broadcast.BrowserClient(ws)            # no sender running yet: it cannot keep up
        c.needs_full = False
        for i in range(broadcast.CLIENT_QUEUE_MAX + 5):
            c.offer(json.dumps({"type": "delta", "n": i}), None)
        self.assertTrue(c.needs_full)
        self.assertEqual(market_state.latency.counters.get("client_resyncs", 0) >= 1, True)
        t = asyncio.create_task(c.run())
        await asyncio.sleep(0.05)
        # One full snapshot, and none of the stale deltas it replaces
        self.assertEqual([m["type"] for m in ws.sent], ["full_state"])
        c.offer(json.dumps({"type": "delta", "n": 99}), None)   # back to normal deltas afterwards
        await asyncio.sleep(0.02)
        self.assertEqual(ws.sent[-1], {"type": "delta", "n": 99})
        t.cancel()


if __name__ == "__main__":
    unittest.main()
