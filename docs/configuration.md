# Configuration

All settings are environment variables. Copy `.env.example` to `.env` and set the two required ones. The server reads the environment at startup, so a change needs a restart.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `KALSHI_KEY_ID` | required | Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | required | Path to the private key (RSA or Ed25519 PEM) |
| `BOOK_COUNT` | `25` | Number of markets to hold full order books for |
| `TOP_LIST_BOOKS` | `BOOK_COUNT / 4` | How many of those go to the top rows of the home table |
| `PERSIST_TRADES` | `1` | Set to `0` to stop saving trades to `trades.duckdb` |
| `HEAVY_HITTER_K` | `60` | Counters in the heavy-hitters summary |
| `BOOK_AUDIT_INTERVAL` | `5` | Seconds between audit checks, one book per check. `0` turns the audit off |
| `METRICS` | `1` | Set to `0` to stop writing the per-minute history |
| `METRICS_DB` | `metrics.duckdb` in the repo root | Where that history is written |
| `GIT_COMMIT` | read from the checkout | The commit label stored with each run in the history. Set it when the code is not a Git checkout, as in the container |

The container image sets `PERSIST_TRADES=0` and `METRICS_DB=/data/metrics.duckdb`. Our deployment also runs with `PERSIST_TRADES=0`, because the database writer fell behind during busy hours on a 2 vCPU instance.

`compose.yaml` adds two variables of its own: `KALSHI_PRIVATE_KEY_FILE`, the key file on your machine that is mounted into the container, and `HOST_UID` with `HOST_GID`, so the container runs as you and can read that file.

## Endpoints

The server listens on 127.0.0.1:8000 by default. It has no login of its own.

| Path | What it is |
|---|---|
| `/` | Home dashboard |
| `/market/{ticker}` | Market page (404 if the market has no order book) |
| `/api/market/{ticker}/info` | Kalshi market details as JSON, cached for 60 s |
| `/graph` | The pipeline as a graph, with per-step load and the streaming summaries |
| `/api/graph` | The data behind `/graph` as JSON |
| `/latency` | Latency percentiles per pipeline stage |
| `/debug/mem` | Memory, garbage collector and queue counters |
| `/ws` and `/ws/market/{ticker}` | The WebSockets the pages use for live updates |

## Files the server writes

| File | When | What it holds |
|---|---|---|
| `metrics.duckdb` | `METRICS=1` | Per-minute latency histograms and counters, one row per name and minute, labelled with the run and the commit |
| `trades.duckdb` | `PERSIST_TRADES=1` | Executed trades from the feed |

Both are ignored by Git. Read `metrics.duckdb` from a copy while the server runs, as DuckDB allows one writer.
