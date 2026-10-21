# Architecture and Data Flow: Broker to WebSocket

This document describes the high-level architecture, how data flows from the broker to the WebSocket chart, and a detailed guide to the `main()` function so you know where to change behavior.

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│  MAIN THREAD (algo process)                                                          │
│                                                                                      │
│  ┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐                   │
│  │   Broker    │────▶│  data/fetch.py   │────▶│  MarketCache     │                   │
│  │ (Fyers/     │     │ get_option_quote  │     │  publish_options │                   │
│  │  Angel)    │     │ get_spot_price    │     │  _options_data   │                   │
│  └─────────────┘     └──────────────────┘     │  _spot_price    │                   │
│         │                      │               │  _subscribers    │                   │
│         │                      │               └────────┬────────┘                   │
│         │                      │                        │                           │
│         │                      │                        │ 1. tracker.update()      │
│         │                      │                        │ 2. _write_queue (DB)     │
│         │                      │                        │ 3. for cb in _subscribers │
│         │                      │                        ▼                           │
│         │                      │               ┌─────────────────┐                   │
│         │                      │               │ _enqueue_update │ (subscriber)      │
│         │                      │               │ (sync callback) │                   │
│         │                      │               └────────┬────────┘                   │
│         │                      │                        │ call_soon_threadsafe     │
│         │                      │                        ▼                           │
│         │                      │               ┌─────────────────┐                   │
│         │                      │               │ asyncio.Queue   │ (_update_queue)   │
│         │                      │               └────────┬────────┘                   │
└─────────┼──────────────────────┼───────────────────────┼───────────────────────────┘
          │                      │                        │
          │                      │                        │ (cross-thread)
          │                      │                        ▼
┌─────────┼──────────────────────┼─────────────────────────────────────────────────────┐
│  API SERVER THREAD (uvicorn)   │                                                        │
│                                │               ┌─────────────────┐                    │
│  ┌─────────────────────────────▼──────────────│ _broadcast_worker │                    │
│  │  FastAPI app (server.py)    │               │ await queue.get │                    │
│  │  • GET /chart → HTML       │               │ manager.        │                    │
│  │  • GET /health, /options    │               │   broadcast(msg)│                    │
│  │  • WebSocket /ws/live       │               └────────┬────────┘                    │
│  │  • ConnectionManager      │                        │                             │
│  │    active_connections[]    │◀───────────────────────┘                             │
│  └─────────────────────────────┬───────────────────────────────────────────────────┘
│                                  │ send_json(message)                                 │
│                                  ▼                                                    │
│  ┌───────────────────────────────────────────────────────────────────────────────┐   │
│  │  WebSocket clients (browser chart page)                                         │   │
│  │  ws.onmessage → updateStats(strangle), addChartPoint(strangle_snapshot)       │   │
│  └───────────────────────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**Two threads:**

- **Main thread:** Runs `main()` in `main.py` — broker calls, fetch, strategy, cache publish, trade entry/exit. Same process holds the **MarketCache** singleton and the **StrangleTracker**.
- **API server thread:** Runs uvicorn/FastAPI in a daemon thread; has its own asyncio event loop. The cache is passed into the app via `app.state.cache` so the server uses the **same** cache instance. The broadcast worker runs on the server’s event loop and sends messages to all WebSocket connections.

---

## 2. Component Overview

| Component | Location | Role |
|-----------|----------|------|
| **Broker** | `brokers/base.py`, `fyers.py`, `angel.py` | Authentication, `get_spot_price()`, `get_option_quote(symbol)`, `place_order()`, symbols, expiry. |
| **Data fetch** | `data/fetch.py` | `get_option_ohlcv_realtime(broker, symbol, publish_to_cache)`, `get_nifty_spot_price(broker)`, `fetch_strikes_smart` / `fetch_strikes_smart_async`. Normalizes broker response and can publish to cache. |
| **MarketCache** | `cache/market_cache.py` | In-memory store (`_options_data`, `_spot_price`). `publish_options(data, spot, atm)` updates cache, strangle tracker, DB queue, and **subscribers**. Throttled by `UI_UPDATE_INTERVAL`. |
| **StrangleTracker** | `core/strangle_tracker.py` | Computes current strangle (CE/PE strikes, prices, total premium) and keeps history for the chart. Fed by cache in `publish_options()`. |
| **DB writer** | `cache/market_cache.py` | Background thread; drains `_write_queue`, writes to SQLite, commits. Started in `main()`, stopped in `finally`. |
| **API server** | `api/server.py` | FastAPI app: REST (`/health`, `/options`, `/chart`, `/strangle/*`), WebSocket `/ws/live`. On startup: creates `_update_queue`, subscribes `_enqueue_update` to cache, starts `_broadcast_worker`. |
| **ConnectionManager** | `api/server.py` | Holds `active_connections: list[WebSocket]`. `broadcast(message)` sends to all. |
| **_enqueue_update** | `api/server.py` | Subscriber callback (sync). Called from main thread when cache publishes. Builds message (options, strangle, strangle_snapshot), puts it on `_update_queue` via `call_soon_threadsafe`. |
| **_broadcast_worker** | `api/server.py` | Async task. `await _update_queue.get()` → `manager.broadcast(message)`. Runs until app shutdown. |
| **Chart page** | `api/templates/chart.html` | Opens WebSocket to `/ws/live`, receives `initial` / `refresh` / `update`; updates stats and Chart.js. |

---

## 3. Data Flow: Broker → WebSocket (Step by Step)

### 3.1 Where data enters the system

- **Broker API** is the only source of live prices. No WebSocket from exchange in this design; the algo **polls** the broker (or uses broker’s own WebSocket if added later).
- Entry points that eventually push to cache/WebSocket:
  1. **Bulk fetch (main path):** `main()` → `_fetch_option_data()` → `fetch_strikes_smart()` or `fetch_strikes_smart_async()` → at the end (and when entry found) calls **`get_cache().publish_options(all_option_data, nifty_spot, atm_strike)`**.
  2. **Single-symbol fetch (optional):** `data/fetch.py`’s `get_option_ohlcv_realtime(..., publish_to_cache=True)` can call `get_cache().publish_options([data], spot, strike)`. Used only when that fetch path is invoked with `publish_to_cache=True` (e.g. some retry or alternate flow). The **exit monitoring** in `core/trade.py` uses `trade.get_option_ohlcv_realtime` (broker only), so it does **not** publish to cache.

So in the standard run, **all WebSocket updates come from the bulk publish in fetch_strikes_smart / fetch_strikes_smart_async**.

### 3.2 Flow in order

1. **Main thread:** `main()` runs; if `ENABLE_API_SERVER` is True, `_start_api_server(cache)` starts uvicorn in a background thread and sets `app.state.cache = cache`.
2. **API server thread:** On startup, FastAPI lifespan runs: creates `_update_queue`, gets cache via `_get_active_cache()` (from `app.state` or singleton), calls **`cache.subscribe(_enqueue_update)`**, starts **`_broadcast_worker()`** task.
3. **Main thread:** Broker is initialized; `_fetch_option_data(broker, strategy, nifty_spot)` runs. Inside fetch:
   - For each strike level, it fetches CE/PE via `get_option_ohlcv_realtime(broker, sym, publish_to_cache=False)` (no per-symbol publish).
   - At the end (and when entry found) it calls **`get_cache().publish_options(all_option_data, nifty_spot, atm_strike)`**.
4. **MarketCache.publish_options** (main thread):
   - Throttles: if last publish was less than `UI_UPDATE_INTERVAL` ago, returns without doing anything.
   - Updates in-memory cache (`_options_data`, `_spot_price`, `_spot_timestamp`).
   - Calls **`tracker.update(all_data, nifty_spot)`** (strangle tracker).
   - Puts a batch on **`_write_queue`** for the DB writer thread.
   - Calls every **subscriber**: **`_enqueue_update(data, nifty_spot)`**.
5. **_enqueue_update** (main thread):
   - Builds a message: `type: "update"`, cleaned options, `strangle` from `tracker.get_current()`, `strangle_snapshot` from `tracker.get_history(limit=1)`.
   - Schedules **`_update_queue.put_nowait(message)`** on the **API server’s event loop** via **`_event_loop.call_soon_threadsafe(...)`**. So the message is put into the asyncio queue from the main thread without blocking.
6. **API server thread (event loop):** The scheduled `put_nowait` runs; the message is in `_update_queue`. **`_broadcast_worker`** was blocked on **`await _update_queue.get()`**; it now gets the message.
7. **_broadcast_worker:** Calls **`manager.broadcast(message)`** → for each WebSocket in `active_connections`, **`await conn.send_json(message)`**.
8. **Browser:** Chart page’s `ws.onmessage` receives the JSON. If `type === "update"`, it calls `updateStats(data.strangle)` and `addChartPoint(data.strangle_snapshot)` to update the UI and chart.

### 3.3 Initial and refresh (no broker in between)

- When a client connects to **WebSocket `/ws/live`**, the **websocket_endpoint** runs on the API server thread. It:
  - Gets cache from `_get_active_cache()` and **tracker** from `get_strangle_tracker()`.
  - Optionally seeds the tracker from cache: `tracker.update(options, spot)` so the chart has data even if no recent publish.
  - Sends one **`type: "initial"`** message with `strangle`, `strangle_history`, options, spot.
- If the client sends the text **`"refresh"`**, the endpoint again reads from cache and tracker and sends **`type: "refresh"`** with the same shape. So “initial” and “refresh” are server-side snapshot from cache/tracker; no new broker call in that moment.

---

## 4. Main Function: Detailed Explanation and Where to Change

`main()` is in **`src/nifty_algo/main.py`**. It runs in the **main thread** and drives the entire algo: config, DB, cache, API server, broker, strategy, fetch, entry, and exit monitoring.

### 4.1 Structure at a glance

```text
main():
  ├── Setup: logging, DB, cache, optional DB writer, optional API server
  ├── try:
  │     ├── Parse run mode (--dry-run / --live)
  │     ├── Validate config
  │     ├── Load strategy, init broker, AlgoTrade
  │     ├── Handle existing positions (optional early return)
  │     ├── Get Nifty spot
  │     ├── Fetch option data (bulk) → publish_options
  │     ├── Optional: snapshot to DB (non–fast mode)
  │     ├── Build context, check entry condition, maybe retry entry search
  │     └── Enter trade (if entry_data) and monitor exit
  └── finally: broker logout, stop_db_writer, end-of-day summary, close DB
```

### 4.2 Block-by-block with “what to change”

---

#### Block 1: Setup (logging, DB, cache)

```python
logger = setup_logging(config.LOG_DIR, config.DRY_RUN)
db_conn = setup_database(config)
cache = get_cache()
broker = None
api_thread = None
```

- **What it does:** Configures logging, creates/opens SQLite DB, gets the **singleton** MarketCache. `broker` and `api_thread` are for cleanup later.
- **To change:**  
  - Log directory or dry-run behavior → `config.LOG_DIR`, `config.DRY_RUN` or `setup_logging` in `logging_config`.  
  - DB path/schema → `config` and `data/db.py` `setup_database`.

---

#### Block 2: Start DB writer (optional)

```python
if db_conn and config.USE_DATABASE:
    from pathlib import Path
    db_path = str(Path(config.LOG_DIR) / config.DATABASE_NAME)
    cache.start_db_writer(db_path, interval=config.DATA_SNAPSHOT_INTERVAL)
```

- **What it does:** Starts the cache’s background thread that drains `_write_queue` and writes options/spot to SQLite every `DATA_SNAPSHOT_INTERVAL` seconds.
- **To change:**  
  - Disable DB writes → `USE_DATABASE = False` or don’t pass `db_conn`.  
  - Snapshot frequency → `config.DATA_SNAPSHOT_INTERVAL`.

---

#### Block 3: Start API server (optional)

```python
if getattr(config, 'ENABLE_API_SERVER', False):
    try:
        api_thread = _start_api_server(cache, getattr(config, 'API_SERVER_PORT', 8000))
    except Exception as e:
        logger.warning("Failed to start API server: %s", e)
```

- **What it does:** Starts uvicorn in a **daemon thread**, with `app.state.cache = cache` so the FastAPI app uses this cache. The server’s lifespan will subscribe `_enqueue_update` and start `_broadcast_worker` when the app starts.
- **To change:**  
  - Enable/disable API and chart → `ENABLE_API_SERVER` (and optionally `API_SERVER_PORT`).  
  - Bind address → change `_start_api_server` (e.g. host in uvicorn config).

---

#### Block 4: Run mode and config validation

```python
try:
    run_mode = _parse_run_mode(sys.argv[1:])
    if run_mode is not None:
        object.__setattr__(config, "DRY_RUN", run_mode)
    config_error = _validate_required_config(config)
    if config_error:
        logger.error(config_error)
        return
```

- **What it does:** `--dry-run` / `--live` override `config.DRY_RUN`. Then validates broker-related config (tokens, keys, etc.).
- **To change:**  
  - Add flags → extend `_parse_run_mode` and set more `config` attributes.  
  - Stricter/looser validation → edit `_validate_required_config`.

---

#### Block 5: Strategy and broker

```python
strategy = get_strategy(config.STRATEGY)
broker = _initialize_broker(config, logger)
if not broker:
    return
algo_trade = AlgoTrade()
```

- **What it does:** Loads the strategy (e.g. strangle), creates and initializes the broker (Fyers/Angel), creates the trade state object.
- **To change:**  
  - Different strategy → `config.STRATEGY` and `strategies/`.  
  - Different broker or auth → `config` and `_initialize_broker` / `brokers/`.

---

#### Block 6: Existing positions

```python
if _handle_existing_positions(broker, algo_trade, logger):
    return
```

- **What it does:** If there are open positions, restores algo state and runs **exit monitoring only** (no new entry), then returns so the rest of main (new entry) is skipped.
- **To change:**  
  - Skip position check or change behavior → modify `_handle_existing_positions` in `main.py` or `core/trade.py` (`check_existing_positions`, `monitor_exit_condition`).

---

#### Block 7: Nifty spot

```python
nifty_spot = _get_nifty_spot_with_market_check(broker, logger)
if nifty_spot is None:
    return
```

- **What it does:** Ensures market is open (or uses pre-open price if configured), gets spot from broker, computes ATM strike, logs.
- **To change:**  
  - Pre-open value → `config.NIFTY_PREOPEN_PRICE`.  
  - Market hours → `market/hours.py` (`is_market_open`, `wait_for_market_open`).

---

#### Block 8: Fetch option data (and first cache/WebSocket publish)

```python
(
    all_option_data,
    option_symbols,
    atm_strike,
    all_strikes,
    expiry_date,
) = _fetch_option_data(broker, strategy, nifty_spot)
```

- **What it does:** Gets expiry, strike list, then:
  - **Sync:** `fetch_strikes_smart()` — for each level, fetches CE/PE via `get_option_ohlcv_realtime(..., publish_to_cache=False)`, then at the end (or when entry found) calls **`get_cache().publish_options(all_option_data, nifty_spot, atm_strike)`**.  
  - **Async:** Same idea with `fetch_strikes_smart_async()`, same `publish_options` at the end.  
  So this is the **first (and main) place** where data flows: broker → fetch → cache → subscribers → queue → broadcast worker → WebSocket.
- **To change:**  
  - Strike range/levels → `config.NUM_STRIKES_ABOVE_BELOW`, `config.STRIKE_INTERVAL`, and the fetch loop.  
  - Throttle of UI/DB/cache updates → `config.UI_UPDATE_INTERVAL` in cache.  
  - What gets published → `data/fetch.py` (`publish_options` call sites and any `publish_to_cache` usage).

---

#### Block 9: Non–fast mode snapshot

```python
if not config.FAST_ENTRY_MODE:
    _snapshot_non_fast(db_conn, all_option_data, nifty_spot, atm_strike)
```

- **What it does:** In non–fast mode, writes one snapshot to DB (options + spot).
- **To change:**  
  - Skip or add snapshots → remove/call this or change `_snapshot_non_fast`.

---

#### Block 10: Entry condition and entry data

```python
ctx = { "all_option_data": all_option_data, "nifty_spot": nifty_spot, "atm_strike": atm_strike, "config": config }
condition_met = strategy.should_enter(ctx)
if algo_trade.trade_completed:
    condition_met = False
entry_data = None
if condition_met:
    entry_data = strategy.get_entry_strikes(all_option_data, nifty_spot, config)
    if entry_data is None:
        entry_data = _retry_entry_search(broker, strategy, nifty_spot, expiry_date, get_entry_strikes_fn, ...)
```

- **What it does:** Asks the strategy whether to enter; if yes, gets entry strikes (CE/PE). If not found, retries with fresh broker data (and possibly more `publish_options` from fetch) until found or max retries.
- **To change:**  
  - Entry rules → strategy’s `should_enter` and `get_entry_strikes` (e.g. `strategies/strangle_0915.py`).  
  - Retry count/interval → `config.STRIKE_SEARCH_MAX_RETRIES`, `STRIKE_SEARCH_RETRY_INTERVAL*`, and `_retry_entry_search`.

---

#### Block 11: Enter trade and monitor exit

```python
_enter_and_monitor_trade(
    broker, algo_trade, entry_data, db_conn,
    all_option_data, nifty_spot, atm_strike,
)
```

- **What it does:** If `entry_data` is not None: sets algo strikes for chart, calls `algo_trade.enter_trade(broker, ...)`, optionally saves a snapshot, then runs **`monitor_exit_condition(broker, algo_trade, ...)`** in a loop until position is closed or interrupted. Exit monitoring uses **broker quotes only** (no cache publish).
- **To change:**  
  - Order size → `config.ORDER_QTY`.  
  - Exit logic → `core/trade.py` (`check_exit`, `monitor_exit_condition`).  
  - Polling interval during exit → `config.API_POLLING_INTERVAL`.

---

#### Block 12: Cleanup (finally)

```python
finally:
    if broker and hasattr(broker, 'logout'):
        try: broker.logout()
        except BaseException: pass
    cache.stop_db_writer()
    if db_conn:
        log_end_of_day_summary(db_conn, date_str)
        db_conn.close()
```

- **What it does:** Logs out broker, stops the cache’s DB writer (drains queue and joins thread), writes end-of-day summary, closes DB connection.
- **To change:**  
  - Add cleanup (e.g. close API server) → add in `finally` or in a shutdown hook.  
  - DB writer timeout → `cache.stop_db_writer(wait_seconds=...)` in `market_cache.py`.

---

## 5. Summary Table: “I want to change…”

| Goal | Where to look |
|------|----------------|
| Broker or auth | `config`, `brokers/`, `_initialize_broker` |
| When/how often data is fetched | `_fetch_option_data`, `data/fetch.py` (`fetch_strikes_smart*`), throttle in `cache` (`UI_UPDATE_INTERVAL`) |
| What gets sent to the chart | `cache.publish_options` (and what’s in `all_option_data`), `_enqueue_update` (message shape), `strangle_tracker` |
| Entry/exit rules | Strategy in `strategies/`, `core/trade.py` (entry/exit, `monitor_exit_condition`) |
| DB writes | `config.USE_DATABASE`, `DATA_SNAPSHOT_INTERVAL`, `cache.start_db_writer`, `cache/market_cache.py` DB writer loop |
| Enable/disable API and chart | `ENABLE_API_SERVER`, `API_SERVER_PORT`, `_start_api_server` |
| WebSocket message format | `api/server.py` (`_enqueue_update` message dict, websocket_endpoint initial/refresh), `chart.html` (`onmessage`) |

---

## 6. File Reference

| File | Purpose |
|------|---------|
| `main.py` | Entry point; `main()` orchestrates everything. |
| `config.py` | All config (broker, strategy, intervals, DB, API server, etc.). |
| `brokers/base.py`, `fyers.py`, `angel.py` | Broker interface and implementations. |
| `data/fetch.py` | Broker → normalized option/spot data; calls `get_cache().publish_options()`. |
| `cache/market_cache.py` | Cache, throttle, DB queue, subscriber notification. |
| `core/strangle_tracker.py` | Strangle state and history for chart. |
| `api/server.py` | FastAPI app, lifespan, `_enqueue_update`, `_broadcast_worker`, WebSocket, REST. |
| `api/templates/chart.html` | Chart page; WebSocket client and Chart.js. |

This should give you a single place to understand architecture, broker-to-WebSocket flow, and exactly where to change behavior in `main()` and related code.
