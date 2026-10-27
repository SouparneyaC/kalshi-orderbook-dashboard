"""
broadcast.enqueue_update throttle: a change that lands inside the 100 ms window must still reach
the queue when the window ends (trailing flush), with at most one flush scheduled.
"""
import asyncio
import unittest

import broadcast
import market_state


class ThrottleTrailingFlushTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.builds = 0
        self._orig_build = market_state.build_delta_payload
        self._orig_interval = broadcast.ENQUEUE_MIN_INTERVAL

        def fake_build():
            self.builds += 1
            return {"markets_changed": [{"ticker": f"T{self.builds}"}], "markets_removed": [],
                    "recent_trades": []}

        market_state.build_delta_payload = fake_build
        broadcast.ENQUEUE_MIN_INTERVAL = 0.05
        broadcast.browser_clients.add("client")
        broadcast._last_enqueue_time = 0.0
        broadcast._trailing_handle = None
        broadcast._trailing_trigger_ns = None
        while not broadcast.update_queue.empty():
            broadcast.update_queue.get_nowait()

    async def asyncTearDown(self):
        if broadcast._trailing_handle is not None:
            broadcast._trailing_handle.cancel()
        broadcast._trailing_handle = None
        broadcast._trailing_trigger_ns = None
        broadcast.browser_clients.discard("client")
        market_state.build_delta_payload = self._orig_build
        broadcast.ENQUEUE_MIN_INTERVAL = self._orig_interval
        while not broadcast.update_queue.empty():
            broadcast.update_queue.get_nowait()

    async def test_suppressed_enqueue_is_flushed_after_window(self):
        broadcast.enqueue_update(100)            # leading edge: sent now
        self.assertEqual(broadcast.update_queue.qsize(), 1)
        broadcast.enqueue_update(200)            # inside the window: suppressed
        self.assertEqual(broadcast.update_queue.qsize(), 1)
        self.assertIsNotNone(broadcast._trailing_handle)
        await asyncio.sleep(0.12)                # past the window
        self.assertEqual(broadcast.update_queue.qsize(), 2)
        broadcast.update_queue.get_nowait()
        _, trigger, _ = broadcast.update_queue.get_nowait()
        self.assertEqual(trigger, 200)
        self.assertIsNone(broadcast._trailing_handle)

    async def test_only_one_trailing_flush_and_earliest_trigger(self):
        broadcast.enqueue_update(100)
        broadcast.enqueue_update(300)
        handle = broadcast._trailing_handle
        broadcast.enqueue_update(250)
        broadcast.enqueue_update(None)
        self.assertIs(broadcast._trailing_handle, handle)   # still the same single flush
        await asyncio.sleep(0.12)
        self.assertEqual(self.builds, 2)                     # leading + exactly one trailing
        broadcast.update_queue.get_nowait()
        _, trigger, _ = broadcast.update_queue.get_nowait()
        self.assertEqual(trigger, 250)                       # earliest suppressed trigger

    async def test_leading_send_cancels_pending_trailing(self):
        broadcast.enqueue_update(100)
        broadcast.enqueue_update(150)                        # schedules a trailing flush
        broadcast._last_enqueue_time = 0.0                   # pretend the window already ended
        broadcast.enqueue_update(400)                        # leading send covers the change
        self.assertIsNone(broadcast._trailing_handle)
        await asyncio.sleep(0.12)
        self.assertEqual(self.builds, 2)                     # no extra trailing build
        broadcast.update_queue.get_nowait()
        _, trigger, _ = broadcast.update_queue.get_nowait()
        self.assertEqual(trigger, 150)                       # earliest change still stamped

    async def test_no_clients_no_work(self):
        broadcast.browser_clients.discard("client")
        broadcast.enqueue_update(100)
        broadcast.enqueue_update(200)
        self.assertEqual(self.builds, 0)
        self.assertIsNone(broadcast._trailing_handle)


class ThrottleNoLoopTest(unittest.TestCase):
    def test_suppressed_call_without_running_loop_does_not_crash(self):
        orig = market_state.build_delta_payload
        market_state.build_delta_payload = lambda: {"markets_changed": [], "markets_removed": [],
                                                    "recent_trades": []}
        broadcast.browser_clients.add("client")
        try:
            broadcast._last_enqueue_time = 0.0
            broadcast._trailing_handle = None
            broadcast.enqueue_update(1)
            broadcast.enqueue_update(2)                      # suppressed, no loop: just returns
            self.assertIsNone(broadcast._trailing_handle)
        finally:
            broadcast.browser_clients.discard("client")
            broadcast._trailing_trigger_ns = None
            market_state.build_delta_payload = orig


if __name__ == "__main__":
    unittest.main()
