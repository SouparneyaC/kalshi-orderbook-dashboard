"""
Kalshi order book dashboard: FastAPI application shell.

Wires together: kalshi_feed (ingestion), market_state (data), broadcast (browser WS).
"""
import asyncio
import json
from html import escape as html_escape
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from latency import gc_freeze_loop, install_gc_probe, loop_lag_probe, now_ms
from persistence import get_conn, insert_trades_batch
import market_state
import broadcast
import kalshi_feed
import market_page


@asynccontextmanager
async def lifespan(app: FastAPI):
    if kalshi_feed.PERSIST_TRADES:
        get_conn()
        print("  [DB] DuckDB initialized")
    else:
        print("  [DB] Trade persistence OFF (PERSIST_TRADES=0): DuckDB not opened")
    install_gc_probe(market_state.latency)
    tasks = [
        asyncio.create_task(kalshi_feed.kalshi_stream()),
        asyncio.create_task(market_state.analytics_decay_loop()),
        asyncio.create_task(broadcast.broadcast_loop()),
        asyncio.create_task(loop_lag_probe(market_state.latency)),
        asyncio.create_task(market_state.latency_log_loop()),
        asyncio.create_task(market_state.prune_stale_markets_loop()),
        asyncio.create_task(market_state.title_fetch_loop()),
        asyncio.create_task(gc_freeze_loop(market_state.latency)),
    ]
    if kalshi_feed.PERSIST_TRADES:
        tasks.append(asyncio.create_task(kalshi_feed.db_writer_loop()))
    yield
    for t in tasks:
        t.cancel()
    if kalshi_feed.pending_trades:
        try:
            insert_trades_batch(kalshi_feed.pending_trades)
            print(f"  [DB] Saved {len(kalshi_feed.pending_trades)} trades on shutdown")
        except Exception as e:
            print(f"  [DB] Shutdown flush failed: {e}")

app = FastAPI(lifespan=lifespan)


@app.get("/")
async def dashboard():
    html_path = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(html_path.read_text())


NO_BOOK_PAGE = """<!DOCTYPE html><meta charset="utf-8"><title>No order book</title>
<body style="background:#12141a;color:#c9cdd6;font:14px sans-serif;padding:40px">
<p>We don't hold an order book for <b>{ticker}</b> right now (books follow the most traded markets
and change every few seconds).</p><p><a href="/" style="color:#3b82f6">&larr; Back to the dashboard</a></p>"""


@app.get("/market/{ticker}")
async def market_page_html(ticker: str):
    """The full page for one market. Only markets we hold an order book for have one."""
    if ticker not in market_state.books:
        return HTMLResponse(NO_BOOK_PAGE.format(ticker=html_escape(ticker)), status_code=404)
    return HTMLResponse((Path(__file__).parent / "market.html").read_text())


@app.get("/api/market/{ticker}/info")
async def market_info(ticker: str):
    """Rules, expiry, category etc. for the market page's info panel (Kalshi REST, cached 60 s)."""
    try:
        return await market_page.get_info(ticker)
    except Exception as e:
        return JSONResponse({"error": f"Kalshi lookup failed: {e}"}, status_code=502)


@app.websocket("/ws/market/{ticker}")
async def market_ws(ws: WebSocket, ticker: str):
    """Live feed for one market tab: full book, quote, this market's trades."""
    if ticker not in market_state.books or market_page.tab_count() >= market_page.MAX_MARKET_TABS:
        await ws.close(code=4404)
        return
    await ws.accept()
    client = market_page.MarketClient(ws, ticker)
    client.register()
    print(f"  Market tab opened: {ticker} ({market_page.tab_count()} tabs)")
    sender = asyncio.create_task(client.run())
    try:
        while True:
            await ws.receive_text()      # tabs send nothing; this just notices the disconnect
    except Exception:
        pass
    finally:
        sender.cancel()
        await client.close()


@app.get("/latency")
async def latency_report():
    """Latency percentiles per stage (ms) + feed health. Used for the L-04 baseline."""
    now = now_ms()
    return {
        "now_ms": now,
        "feed": {
            **market_state.feed,
            "last_msg_age_ms": now - market_state.feed["last_msg_ms"] if market_state.feed["last_msg_ms"] else None,
        },
        "queue_size": broadcast.update_queue.qsize(),
        **market_state.latency.snapshot(force=True),
    }


def _memory_report() -> dict:
    """Process memory as the OS sees it, plus what the garbage collector has been doing."""
    import gc
    import resource
    import sys
    ru = resource.getrusage(resource.RUSAGE_SELF)
    peak = ru.ru_maxrss / 2**20 if sys.platform == "darwin" else ru.ru_maxrss / 1024  # macOS: bytes, Linux: KiB
    current = None
    try:
        with open("/proc/self/statm") as f:                       # Linux only
            current = int(f.read().split()[1]) * resource.getpagesize() / 2**20
    except OSError:
        pass
    return {
        "rss_mb": round(current, 1) if current is not None else None,
        "peak_rss_mb": round(peak, 1),
        "page_faults_minor": ru.ru_minflt,   # page was in RAM, just not mapped yet
        "page_faults_major": ru.ru_majflt,   # had to wait for disk: these hurt
        "gc_collections": [g["collections"] for g in gc.get_stats()],
        "gc_frozen_objects": gc.get_freeze_count(),
        "gc_tracked_objects": len(gc.get_objects()),
        # cpumemory §4.3/§7.5: huge pages cut TLB misses. Linux only; "[always]" or "[madvise]" is the active mode.
        "transparent_hugepages": _read_first_line("/sys/kernel/mm/transparent_hugepage/enabled"),
    }


def _read_first_line(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return None


@app.get("/debug/mem")
async def debug_mem():
    """Sizes of every long-lived structure, to find what grows (OOM on the 2 GB server)."""
    return {
        # Page faults, current vs peak memory, GC activity (Drepper §7.1, §7.3).
        **_memory_report(),
        "asyncio_tasks": len(asyncio.all_tasks()),
        "persist_trades": kalshi_feed.PERSIST_TRADES,
        "pending_trades": len(kalshi_feed.pending_trades),
        "tickers": len(market_state.tickers),
        "book_seq_gaps": market_state.book_seq.gaps,
        "book_resyncs_in_flight": len(market_state.book_seq.in_flight),
        "book_target": market_state.MAX_ORDERBOOK_MARKETS,
        "top_list_books": market_state.TOP_LIST_BOOKS,
        "heavy_hitter_k": market_state.HEAVY_HITTER_K,
        "books": len(market_state.books),
        "msgs": market_state.feed["msgs"],
        "stats": {k: v for k, v in market_state.stats.items() if k != "start_time"},
        "cpu_ms": {k: round(v / 1e6, 1) for k, v in market_state.cpu_ns.items()},
        "book_levels": sum(b.level_count() for b in market_state.books.values()),
        "market_titles": len(market_state.market_titles),
        "readable_titles": len(market_state.readable_titles),
        "market_recv_ms": len(market_state.market_recv_ms),
        "market_venue_ms": len(market_state.market_venue_ms),
        "dirty_markets": len(market_state.dirty_markets),
        "heavy_hitters": len(market_state.heavy_hitters.counters),
        "recent_dollar_markets": len(market_state.recent_dollars),
        "trades_dropped": market_state.latency.counters.get("trades_dropped", 0),
        "trades_deduped": market_state.latency.counters.get("trades_deduped", 0),
        "seen_trade_ids": len(kalshi_feed._seen_trade_ids),
        "trade_buffer": len(market_state.trade_buffer.trades),
        "browser_clients": len(broadcast.browser_clients),
        "market_tabs": market_page.tab_count(),
        "market_trade_rings": len(market_state.market_trades),
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    client = broadcast.BrowserClient(ws)
    broadcast.browser_clients.add(client)
    print(f"  Browser connected ({len(broadcast.browser_clients)} total)")
    sender = asyncio.create_task(client.run())   # sends the full snapshot first, then deltas
    client.queue.put_nowait(None)                 # wake it now rather than at the next delta

    try:
        while True:
            text = await ws.receive_text()
            # Browser reports how long after server_ts it finished painting
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "render_ack":
                ms = msg.get("send_to_render_ms")
                if isinstance(ms, (int, float)) and 0 <= ms < 600_000:
                    market_state.latency.record("send_to_render", float(ms))
    except Exception:
        pass   # WebSocketDisconnect, or the sender closed the socket after a send timeout
    finally:
        sender.cancel()
        await client.close()


if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  Kalshi order book dashboard: all markets")
    print("  Layer 1: Global Ticker (every market at 1Hz)")
    print("  Layer 2: Global Trade (every trade real-time)")
    print(f"  Layer 3: Orderbook (top {market_state.MAX_ORDERBOOK_MARKETS} by Misra-Gries)")
    print("  Open http://localhost:8000 in your browser")
    print("=" * 60)
    print()
    uvicorn.run(app, host="127.0.0.1", port=8000)
