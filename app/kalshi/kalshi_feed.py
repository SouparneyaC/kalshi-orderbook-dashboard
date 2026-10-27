"""
Kalshi WebSocket feed: connects, authenticates, routes messages to market_state.
"""
import asyncio
import json
import os
import ssl
import time

import certifi
import orjson
import websockets

from app.kalshi.auth import load_private_key, make_auth_headers
from app.core.latency import perf_ns, now_ms, ns_to_ms
from app.core.orderbook import OrderBook
from app.storage.persistence import TradeColumns, insert_trades_batch
from app.core import market_state
from app.server import market_page
from app.server import broadcast

from collections import deque

KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
PROMOTE_CHECK_INTERVAL = 10  # seconds between tier promotion checks

# Trade deduplication: Kalshi sends overlapping data on reconnects.
# A set answers "seen it?", a ring remembers arrival order so the oldest id can
# be forgotten. Same behaviour as the old OrderedDict, without its per-entry linked-list node.
_seen_trade_ids: set[str] = set()
_seen_order: deque[str] = deque()
MAX_SEEN_TRADE_IDS = 50_000

# Persistence: ingest appends here (already as DB columns), db_writer_loop drains it
pending_trades = TradeColumns()
DB_FLUSH_INTERVAL = 0.25      # seconds between writes
MAX_PENDING_TRADES = 100_000  # if the writer falls this far behind, drop the oldest (counted) instead of OOM
# PERSIST_TRADES=0 turns off saving trades to DuckDB (the live feed, tape and books are unaffected).
PERSIST_TRADES = os.environ.get("PERSIST_TRADES", "1") != "0"


# --- Tier promotion: Misra-Gries decides which markets get full orderbooks ---

async def promote_markets(ws, msg_id_ref: list[int]):
    """Check heavy hitters and promote/demote orderbook subscriptions."""
    desired = set(market_state.book_candidates(market_state.MAX_ORDERBOOK_MARKETS))
    current = set(market_state.books.keys())

    to_add = desired - current
    to_remove = current - desired

    if not to_add and not to_remove:
        return

    # Remove demoted markets
    for ticker in to_remove:
        del market_state.books[ticker]
        market_state.book_seq.forget(ticker)
        market_page.notify(ticker)      # an open market tab learns its book is gone

    # Add promoted markets
    market_state.stats["book_promotions"] += len(to_add)
    for ticker in to_add:
        market_state.books[ticker] = OrderBook()
        market_state.books[ticker].ticker = ticker

    # Update the subscription if we have one
    if market_state.book_sub_sid is not None:
        if to_remove:
            await ws.send(json.dumps({
                "id": msg_id_ref[0], "cmd": "update_subscription",
                "params": {
                    "sid": market_state.book_sub_sid,
                    "action": "delete_markets",
                    "market_tickers": list(to_remove),
                },
            }))
            msg_id_ref[0] += 1

        if to_add:
            await ws.send(json.dumps({
                "id": msg_id_ref[0], "cmd": "update_subscription",
                "params": {
                    "sid": market_state.book_sub_sid,
                    "action": "add_markets",
                    "market_tickers": list(to_add),
                },
            }))
            msg_id_ref[0] += 1

    if to_add or to_remove:
        added = ", ".join(list(to_add)[:5])
        removed = ", ".join(list(to_remove)[:5])
        if to_add:
            print(f"  [PROMOTE] +{len(to_add)} books: {added}{'...' if len(to_add) > 5 else ''}")
        if to_remove:
            print(f"  [DEMOTE]  -{len(to_remove)} books: {removed}{'...' if len(to_remove) > 5 else ''}")


# Text frame → dict. One name so the benchmark measures exactly what the feed uses.
# orjson builds the same dicts as json.loads, several times faster.
decode = orjson.loads


# --- Message routing (sync, so tests and the benchmark can drive it without a socket) ---

# Throttle state that used to be locals inside kalshi_stream
_ticker_batch_count = 0
_last_broadcast = 0.0
_last_promote_check = 0.0


def reset_stream_state():
    """Called on every (re)connect."""
    global _ticker_batch_count, _last_broadcast, _last_promote_check
    _ticker_batch_count = 0
    _last_broadcast = time.time()
    _last_promote_check = time.time()


def handle_message(data: dict, recv_ns: int, recv_ms: int) -> bool:
    """Apply one parsed Kalshi message to market_state.

    Returns True when it is time to run the order book promotion check, which needs the
    socket (async) and so is done by the caller.
    """
    global _ticker_batch_count, _last_broadcast, _last_promote_check
    msg_type = data.get("type")

    # Seq-gap check: one counter per book subscription (book_seq.py). A gap means a lost
    # message on that sid, market unknown, so every book on it gets a fresh snapshot.
    book_sid = market_state.book_sub_sid
    if book_sid is not None and data.get("sid") == book_sid:
        seq = data.get("seq")
        if seq is not None and market_state.book_seq.observe(seq):
            market_state.latency.incr("book_seq_gaps")
            print(f"  [SEQ] gap on sid {book_sid} at seq {seq}; "
                  f"resyncing {len(market_state.books)} books")
            market_state.book_seq.request(market_state.books.keys())

    if msg_type == "subscribed":
        channel = data.get("msg", {}).get("channel")
        sid = data.get("msg", {}).get("sid")
        print(f"  Subscribed to {channel} (sid={sid})")
        if channel == "orderbook_delta":
            market_state.book_sub_sid = sid
            market_state.book_seq.reset()
        return False

    # --- Layer 1: Ticker updates ---
    if msg_type == "ticker":
        msg = data["msg"]
        ticker = msg.get("market_ticker", "")
        if ticker:
            market_state.update_ticker(ticker, msg)   # hot fields only, parsed once
            market_state.dirty_markets.add(ticker)
            market_state.stats["ticker_updates"] += 1
            _ticker_batch_count += 1
            market_state.mark_market(ticker, msg, "ticker", recv_ms)
            market_page.notify(ticker)

            # Extract title from additional_metadata if available
            if ticker not in market_state.market_titles:
                market_state.market_titles[ticker] = ticker
            market_state.latency.record("recv_to_hub", ns_to_ms(perf_ns() - recv_ns))

            # Broadcast every 50 ticker updates or every 2 seconds
            now = time.time()
            if _ticker_batch_count >= 50 or now - _last_broadcast >= 2.0:
                broadcast.enqueue_update(recv_ns)
                _ticker_batch_count = 0
                _last_broadcast = now
        return False

    # --- Layer 2: Trade stream ---
    if msg_type == "trade":
        msg = data["msg"]
        ticker = msg.get("market_ticker", "")
        trade_id = msg.get("trade_id")

        # Dedup: skip trades we've already seen (overlapping data on reconnects)
        if trade_id:
            if trade_id in _seen_trade_ids:
                market_state.latency.incr("trades_deduped")
                return False
            _seen_trade_ids.add(trade_id)
            _seen_order.append(trade_id)
            if len(_seen_order) > MAX_SEEN_TRADE_IDS:
                _seen_trade_ids.discard(_seen_order.popleft())

        count = float(msg.get("count_fp", "1"))
        market_state.stats["trade_count"] += 1

        # Feed streaming algorithms. Heavy hitters (which markets get order books) are ranked by
        # DOLLARS traded, not contracts: penny parlays trade huge contract counts worth almost
        # nothing (70,482 contracts = $72 in one live minute) and took 167 of 200 books.
        try:
            yes_px = float(msg.get("yes_price_dollars") or 0)
        except (TypeError, ValueError):
            yes_px = 0.0
        paid = yes_px if msg.get("taker_outcome_side") != "no" else 1.0 - yes_px
        market_state.heavy_hitters.add(ticker, weight=count * paid)
        market_state.add_recent_dollars(ticker, count * paid)
        market_state.concentration.add(ticker, weight=count)
        market_state.distinct_markets.add(ticker)

        # Store in buffer
        trade_data = {
            "ticker": ticker,
            "title": market_state.market_titles.get(ticker, ticker),
            "price": msg.get("yes_price_dollars"),
            "count": msg.get("count_fp"),
            "side": msg.get("taker_outcome_side"),
            "book_side": msg.get("taker_book_side"),
            "ts": msg.get("ts_ms"),
            "recv_ts": recv_ms,
            "trade_id": trade_id,
        }
        market_state.trade_buffer.add(trade_data)
        market_state.record_market_trade(ticker, trade_data)
        market_page.notify(ticker)
        market_state.mark_market(ticker, msg, "trade", recv_ms)
        market_state.dirty_markets.add(ticker)
        market_state.latency.record("recv_to_hub", ns_to_ms(perf_ns() - recv_ns))

        # Persisted by db_writer_loop (one writer, off the event loop). Straight into DB columns,
        # numbers parsed here once.
        price, count_fp = trade_data["price"], trade_data["count"]
        if PERSIST_TRADES:
            pending_trades.append(trade_data["ts"], ticker, float(price) if price else None,
                                  float(count_fp) if count_fp else None, trade_data["side"],
                                  trade_data["book_side"], recv_ms, trade_id)

        # Periodically check for tier promotions
        now = time.time()
        if now - _last_promote_check >= PROMOTE_CHECK_INTERVAL:
            _last_promote_check = now
            return True
        return False

    # --- Layer 3: Orderbook data ---
    if msg_type == "orderbook_snapshot":
        msg = data["msg"]
        ticker = msg.get("market_ticker", "")
        if ticker not in market_state.books:
            market_state.books[ticker] = OrderBook()
        market_state.books[ticker].load_snapshot(msg, ticker=ticker)
        market_state.book_seq.snapshot_received(ticker)
        market_page.notify(ticker)
        market_state.dirty_markets.add(ticker)
        market_state.stats["book_deltas"] += 1
        market_state.stats["book_snapshots"] += 1
        market_state.mark_market(ticker, msg, "book", recv_ms)
        market_state.latency.record("recv_to_hub", ns_to_ms(perf_ns() - recv_ns))
        broadcast.enqueue_update(recv_ns)
        return False

    if msg_type == "orderbook_delta":
        msg = data["msg"]
        ticker = msg.get("market_ticker", "")
        if ticker in market_state.books:
            market_state.books[ticker].apply_delta(msg)
            market_page.notify(ticker)
            market_state.dirty_markets.add(ticker)
            market_state.stats["book_deltas"] += 1
            market_state.mark_market(ticker, msg, "book", recv_ms)
            market_state.latency.record("recv_to_hub", ns_to_ms(perf_ns() - recv_ns))
            # Broadcast every 20 total deltas across all markets
            if market_state.stats["book_deltas"] % 20 == 0:
                broadcast.enqueue_update(recv_ns)
        return False

    # --- Lifecycle: extract titles ---
    if msg_type == "market_lifecycle_v2":
        msg = data["msg"]
        ticker = msg.get("market_ticker", "")
        meta = msg.get("additional_metadata", {})
        if meta.get("title"):
            market_state.market_titles[ticker] = meta["title"]
            market_state.dirty_markets.add(ticker)

        event_type = msg.get("event_type")
        if event_type == "determined":
            market_state.books.pop(ticker, None)
            market_page.notify(ticker)
            market_state.tickers.pop(ticker, None)
            market_state.market_titles.pop(ticker, None)
            market_state.readable_titles.pop(ticker, None)
            market_state.market_recv_ms.pop(ticker, None)
            market_state.market_venue_ms.pop(ticker, None)
            market_state.dirty_markets.discard(ticker)
        return False

    if msg_type == "error":
        err = data.get("msg", {})
        code = err.get("code")
        print(f"  ERROR (code={code}): {err}")
        if code == 25:
            print("  Buffer overflow! Reducing orderbook subscriptions...")
            # Halve the number of orderbook markets
            current_books = list(market_state.books.keys())
            half = len(current_books) // 2
            for t in current_books[half:]:
                del market_state.books[t]
        return False

    return False


async def _request_book_snapshots(ws, msg_id_ref: list[int]):
    """Send get_snapshot for the books queued by a seq gap (de-duplicated in book_seq)."""
    sid = market_state.book_sub_sid
    tickers = [t for t in market_state.book_seq.take_pending() if t in market_state.books]
    if sid is None or not tickers:
        return
    await ws.send(json.dumps({
        "id": msg_id_ref[0], "cmd": "update_subscription",
        "params": {"sid": sid, "action": "get_snapshot", "market_tickers": tickers},
    }))
    msg_id_ref[0] += 1
    market_state.latency.incr("book_resync_requests")


async def _book_subscription_check(ws, msg_id_ref: list[int]):
    """First time: create the orderbook subscription. After that: promote/demote."""
    if market_state.book_sub_sid is None and market_state.recent_dollars:
        top_tickers = market_state.book_candidates(market_state.MAX_ORDERBOOK_MARKETS)
        for t in top_tickers:
            market_state.books[t] = OrderBook()
            market_state.books[t].ticker = t
        await ws.send(json.dumps({
            "id": msg_id_ref[0], "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": top_tickers,
            },
        }))
        msg_id_ref[0] += 1
        print(f"  [INIT] Orderbook sub for {len(top_tickers)} heavy hitters")
    else:
        await promote_markets(ws, msg_id_ref)


# --- Main Kalshi ingestion loop ---

async def kalshi_stream():
    while True:
        market_state.feed["connected"] = False
        try:
            private_key = load_private_key()
            ws_headers = make_auth_headers(private_key, "GET", "/trade-api/ws/v2")
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            market_state.stats["start_time"] = time.time()

            msg_id = 1
            msg_id_ref = [msg_id]
            market_state.book_sub_sid = None
            market_state.book_seq.reset()

            print("Connecting to Kalshi WebSocket...")

            async with websockets.connect(
                KALSHI_WS_URL,
                additional_headers=ws_headers,
                ssl=ssl_ctx,
                max_size=64 * 1024 * 1024,
            ) as ws:
                print("Connected! Subscribing to global streams...")
                market_state.feed["connected"] = True

                # Layer 1: global ticker, all markets at 1 Hz
                await ws.send(json.dumps({
                    "id": msg_id_ref[0], "cmd": "subscribe",
                    "params": {"channels": ["ticker"]},
                }))
                msg_id_ref[0] += 1

                # Layer 2: global trade, all trades
                await ws.send(json.dumps({
                    "id": msg_id_ref[0], "cmd": "subscribe",
                    "params": {"channels": ["trade"]},
                }))
                msg_id_ref[0] += 1

                # Lifecycle for market metadata
                await ws.send(json.dumps({
                    "id": msg_id_ref[0], "cmd": "subscribe",
                    "params": {"channels": ["market_lifecycle_v2"]},
                }))
                msg_id_ref[0] += 1

                reset_stream_state()

                # --- Message loop ---
                async for raw_msg in ws:
                    # Stamp before parsing: this is the moment the bytes reached us
                    recv_ns = perf_ns()
                    recv_ms = now_ms()
                    market_state.feed["last_msg_ms"] = recv_ms
                    market_state.feed["msgs"] += 1

                    data = decode(raw_msg)
                    promote = handle_message(data, recv_ns, recv_ms)
                    kind = data.get("type") or "other"
                    cpu = market_state.cpu_ns
                    cpu[kind] = cpu.get(kind, 0) + perf_ns() - recv_ns
                    if promote:
                        t0 = perf_ns()
                        await _book_subscription_check(ws, msg_id_ref)
                        cpu["promotion_check"] = cpu.get("promotion_check", 0) + perf_ns() - t0
                    if market_state.book_seq.pending:
                        await _request_book_snapshots(ws, msg_id_ref)

        except Exception as e:
            market_state.feed["connected"] = False
            print(f"Kalshi connection error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


# --- DB writer ---

async def db_writer_loop():
    """The only DuckDB writer: one write in flight, batch size grows with the backlog."""
    while True:
        await asyncio.sleep(DB_FLUSH_INTERVAL)
        if pending_trades:
            batch = pending_trades.take()
            t0 = perf_ns()
            try:
                await asyncio.to_thread(insert_trades_batch, batch)
            except Exception as e:
                print(f"  [DB] Flush error ({len(batch)} trades put back): {e}")
                pending_trades.put_back(batch)
            market_state.latency.record("db_flush", ns_to_ms(perf_ns() - t0))
        overflow = len(pending_trades) - MAX_PENDING_TRADES
        if overflow > 0:
            pending_trades.drop_oldest(overflow)
            market_state.latency.incr("trades_dropped", overflow)
            print(f"  [DB] Writer behind: dropped {overflow} oldest unsaved trades")
