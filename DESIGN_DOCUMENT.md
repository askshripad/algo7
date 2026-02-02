# Nifty Options Algo — Design Document

High-level design for the Nifty options algo: architecture, modules, data flow, and main design decisions.  
Use this together with `ALGO_STRATEGY.md` and `REWRITE_STEPS.md`.

---

## 1. System Overview

### 1.1 Purpose

- **Automated Nifty options strategy**: Enter a long straddle/strangle (one CE + one PE) when a volume-based entry condition is met and exit when combined profit reaches a target.
- **Broker-agnostic core**: Supports Fyers and Angel One via a common `BrokerInterface`.
- **Observability**: Logging, SQLite storage, optional CSV snapshots, and end-of-day summary.

### 1.2 High-Level Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              MAIN ORCHESTRATION                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  1. Config, logging, DB, broker init                                         │
│  2. Existing positions? → Restore state → Monitor exit → [Optionally log]    │
│  3. Wait for market open, get Nifty spot                                     │
│  4. ATM + strikes, expiry, fetch OHLCV (sync/async, smart or full)           │
│  5. Entry condition (volume or bypass) → find_entry_strikes                  │
│  6. Enter trade → Monitor exit (WebSocket or polling)                        │
│  7. [Optional] Continue logging after trade completion                       │
│  8. finally: end-of-day summary, close DB                                    │
└─────────────────────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
   ┌──────────┐        ┌──────────┐        ┌──────────┐
   │ Brokers  │        │ Strategy │        │   Data   │
   │ (Fyers,  │        │(volume,  │        │ (fetch,  │
   │  Angel)  │        │strikes,  │        │   DB)    │
   └──────────┘        │  trade)  │        └──────────┘
                       └──────────┘
```

---

## 2. Architecture

### 2.1 Layered Structure

| Layer        | Role                                                                 | Key components                              |
|--------------|----------------------------------------------------------------------|---------------------------------------------|
| **Config**   | Env + defaults, logging setup                                        | `config`, `setup_logging`                    |
| **Broker**   | Abstract broker API; Fyers/Angel implementations                     | `BrokerInterface`, `FyersBroker`, `AngelBroker`, `get_broker` |
| **Market**   | Market hours, expiry                                                 | `is_market_open`, `wait_for_market_open`, `get_nearest_expiry` |
| **Strategy** | Strikes, volume, entry condition, trade state, exit monitoring       | `strikes`, `volume`, `trade` (incl. `AlgoTrade`) |
| **Data**     | Option OHLCV fetch (sync/async), DB writes, snapshot logic           | `fetch`, `db`                               |
| **Main**     | Orchestration only; no business logic                                | `main()`                                    |

- **Main** depends on Config, Broker, Market, Strategy, Data.
- **Strategy** and **Data** depend on Config; Data uses Broker for quotes; Strategy uses Broker only for `place_order` and (indirectly) for symbol format via `get_option_symbol(..., broker)`.
- **Broker** and **Market** do not depend on Strategy or Data.

### 2.2 Package Layout (Target)

```
src/nifty_algo/
├── __init__.py
├── config.py
├── brokers/
│   ├── __init__.py      # get_broker
│   ├── base.py          # BrokerInterface
│   ├── fyers.py         # FyersBroker, FyersWebSocketHandler
│   └── angel.py         # AngelBroker
├── strategy/
│   ├── __init__.py
│   ├── volume.py        # calculate_otm_volumes, check_entry_condition
│   ├── strikes.py       # round_to_nearest_50, get_strike_prices, get_option_symbol, find_entry_strikes
│   └── trade.py         # AlgoTrade, check_existing_positions, monitor_exit_condition
├── data/
│   ├── __init__.py
│   ├── db.py            # setup_database, save_*_to_db, should_take_snapshot, log_end_of_day_summary
│   └── fetch.py         # get_option_ohlcv_realtime, _async, fetch_strikes_smart, _async, get_nifty_spot_price
├── market/
│   ├── __init__.py
│   ├── hours.py         # is_market_open, wait_for_market_open
│   └── expiry.py        # get_nearest_expiry
└── main.py
```

---

## 3. Module Design

### 3.1 Config (`config.py`)

- **Responsibilities**: Load `.env`, define defaults, parse types (int, float, bool), expose a single `config` object or `Config` dataclass.
- **No side effects**: No logger or DB; only values.
- **Key groups**:
  - Broker selection and credentials (Fyers, Angel).
  - Strategy: `MIN/MAX_PREMIUM`, `MAX_TOTAL_PREMIUM`, `TARGET_PROFIT_POINTS`, `VOLUME_RATIO_THRESHOLD`, `BYPASS_ENTRY_CONDITION`.
  - Strikes: `STRIKE_INTERVAL`, `NUM_STRIKES_ABOVE_BELOW`, `FAST_ENTRY_MODE`, `FAST_ENTRY_MAX_STRIKES`.
  - Fetch: `USE_ASYNC_FETCHING`, `ASYNC_CONCURRENT_LIMIT`; retry: `STRIKE_SEARCH_RETRY_INTERVAL`, `STRIKE_SEARCH_MAX_RETRIES`.
  - Execution: `DRY_RUN`, `USE_WEBSOCKET`, `API_POLLING_INTERVAL`, `DATA_SNAPSHOT_INTERVAL`, `USE_DATABASE`, `DATABASE_NAME`, `LOG_DIR`, `NIFTY_PREOPEN_PRICE`, `CONTINUE_LOGGING_AFTER_TRADE`, `MAX_TRADES_PER_DAY` (if used).

---

### 3.2 Brokers (`brokers/`)

#### 3.2.1 `BrokerInterface` (base.py)

Abstract interface used by Strategy and Data. All broker-specific formats are hidden behind it.

| Method | Returns | Description |
|--------|---------|-------------|
| `initialize()` | `bool` | Auth, session, optional master/token setup. Raises on failure. |
| `get_profile()` | `dict` | User profile for login check. |
| `get_spot_price(symbol)` | `float \| None` | LTP for Nifty index. |
| `get_option_quote(symbol)` | `dict \| None` | Raw quote; keys differ by broker (Fyers: `lp`, `open_price`…; Angel: `ltp`, `open`…). |
| `get_positions()` | `dict \| None` | Raw positions (Fyers: `netPositions`; Angel: `data`). |
| `place_order(symbol, side, qty, order_type, price)` | broker-defined | Place order; order id extraction is broker-specific. |
| `get_option_symbol(expiry, strike, option_type)` | `str` | Broker-specific symbol string. |
| `connect_websocket(symbols, handler)` | `(ws, handler) \| (None, None)` | If not supported or disabled, return `(None, None)`. |

- `get_option_quote` is **not** normalized here; normalization happens in `data.fetch.get_option_ohlcv_realtime`.

#### 3.2.2 FyersBroker (`fyers.py`)

- **Symbol**: `NSE:NIFTY{YY}{M}{DD}{strike}{CE|PE}` (month no leading zero, e.g. `2611326200`).
- **Quote mapping**: `lp`→LTP, `open_price`, `high_price`, `low_price`, `volume`, `prev_close_price`, and Greeks if provided.
- **WebSocket**: `FyersDataSocket`, `on_message` → `handler.on_message`; handler maintains `realtime_data[symbol]` with `LTP`, `Open`, `High`, `Low`, `Volume`. `connect_websocket` returns `(fyers_ws, handler)` or `(None, None)` when WS is off or lib missing.

#### 3.2.3 AngelBroker (`angel.py`)

- **Symbol**: `NSE:NIFTY{DD}{MMM}{YY}{strike}{CE|PE}` (e.g. `13JAN26`).
- **Auth**: `SmartConnect`, TOTP, `generateSession` with MPIN.
- **Option quote**: Prefer `marketDataFull(exchange="NFO", tradingsymbol)`. Fallback: `ltpData` with `symboltoken` from `_get_option_token` (contract master first, then `searchScrip`). `_get_exchange_for_symbol`: NFO for CE/PE, else NSE.
- **Token resolution**: `_load_contract_master` (cached file), `_get_token_from_master`, `_get_option_token` with `symbol_token_cache` and `failed_lookups`; rate limiting (e.g. 100ms) between API calls.
- **WebSocket**: `connect_websocket` returns `(None, None)`; monitoring uses polling.

#### 3.2.4 Factory

- `get_broker(config) -> BrokerInterface`: `config.BROKER in ('fyers','angel')` → corresponding implementation; else `ValueError`.

---

### 3.3 Strategy (`strategy/`)

#### 3.3.1 Volume (`volume.py`)

- **`calculate_otm_volumes(all_option_data, atm_strike)`**  
  - OTM calls: `Option_Type=='CE'` and `Strike > atm_strike`.  
  - OTM puts: `Option_Type=='PE'` and `Strike < atm_strike`.  
  - Returns `(otm_call_volume, otm_put_volume)`.

- **`check_entry_condition(otm_call_volume, otm_put_volume)`**  
  - `True, "CALL_VOLUME_DOMINANT"` if `otm_call_volume >= VOLUME_RATIO_THRESHOLD * otm_put_volume`.  
  - `True, "PUT_VOLUME_DOMINANT"` if `otm_put_volume >= VOLUME_RATIO_THRESHOLD * otm_call_volume`.  
  - Else `False, "NO_CONDITION"`.

#### 3.3.2 Strikes (`strikes.py`)

- **`round_to_nearest_50(price)`**  
  - `round(price / STRIKE_INTERVAL) * STRIKE_INTERVAL`.

- **`get_strike_prices(spot, fast_mode)`**  
  - ATM = `round_to_nearest_50(spot)`.  
  - `num = FAST_ENTRY_MAX_STRIKES` if `fast_mode` else `NUM_STRIKES_ABOVE_BELOW`.  
  - `strikes_above`, `strikes_below`, `all_strikes`; return `(atm_strike, strikes_above, strikes_below, all_strikes)`.

- **`get_option_symbol(expiry, strike, option_type, broker)`**  
  - `broker.get_option_symbol(expiry, strike, option_type)`.

- **`find_entry_strikes(all_option_data, nifty_spot)`**  
  - CE/PE with `MIN_PREMIUM <= LTP <= MAX_PREMIUM`; build `call_candidates` and `put_candidates` with `distance = |strike - nifty_spot|`.  
  - Among pairs with `call_premium + put_premium <= MAX_TOTAL_PREMIUM`, choose minimum `call_distance + put_distance`.  
  - Return `(call_strike, put_strike, call_price, put_price, call_symbol, put_symbol)` or `None`.

#### 3.3.3 Trade (`trade.py`)

- **`AlgoTrade`**  
  - State: `entry_*`, `is_position_open`, `call_symbol`, `put_symbol`, `trade_completed`, `call_order_id`, `put_order_id`.  
  - `restore_from_positions(call_symbol, put_symbol, call_strike, put_strike, call_price, put_price)`: set state from existing positions.  
  - `enter_trade(..., broker)`: set state; if not DRY_RUN, `_place_order` for CE and PE; interpret broker response for order ids.  
  - `check_exit(call_ltp, put_ltp)`: `(call_ltp + put_ltp) - entry_total_price >= TARGET_PROFIT_POINTS`.  
  - `exit_trade(exit_call_price, exit_put_price, profit, broker)`: if `is_position_open`, log, place SELLs if not DRY_RUN, set `is_position_open=False`, `trade_completed=True`.

- **`check_existing_positions(broker)`**  
  - Parse `get_positions()` for Nifty CE/PE with `netQty > 0` (or Angel equivalent).  
  - Resolve strike from symbol string; get LTP from position or `get_option_quote`.  
  - Return `(call_symbol, put_symbol, call_strike, put_strike, call_price, put_price)` or `None`.

- **`monitor_exit_condition(broker, algo_trade, handler, check_interval)`**  
  - Loop while `algo_trade.is_position_open`:  
    - LTP from `handler.get_realtime_ohlcv` if `handler` else 0.  
    - If either LTP 0: `get_option_ohlcv_realtime(broker, call/put_symbol)`.  
    - If both > 0 and `algo_trade.check_exit(...)`: `algo_trade.exit_trade(...)`, break.  
    - `time.sleep(check_interval)` (or 0.1 when using handler).  
  - Handle `KeyboardInterrupt` and `Exception` to avoid hard crash.

---

### 3.4 Data (`data/`)

#### 3.4.1 Fetch (`fetch.py`)

- **`get_option_ohlcv_realtime(broker, symbol)`**  
  - `quote = broker.get_option_quote(symbol)`.  
  - Branch on `isinstance(broker, FyersBroker)` vs `AngelBroker)` to map to a **common dict**:  
    `Symbol, Open, High, Low, Close, LTP, Volume, Prev_Close, Delta, Gamma, Theta, Vega, IV, Timestamp`.  
  - Return `None` on missing/invalid quote.

- **`get_option_ohlcv_realtime_async(broker, symbol)`**  
  - `asyncio.to_thread(get_option_ohlcv_realtime, broker, symbol)` (or `run_in_executor`).

- **`get_nifty_spot_price(broker)`**  
  - `broker.get_spot_price(NIFTY_SYMBOL)`.

- **`fetch_strikes_smart(broker, expiry, atm_strike, nifty_spot)`**  
  - Levels 0..`FAST_ENTRY_MAX_STRIKES`; level 0 = ATM, then ±1, ±2, …  
  - For each new strike at that level: CE and PE via `get_option_symbol` + `get_option_ohlcv_realtime`; append to `all_option_data` and `option_symbols`.  
  - If both a CE and PE in [MIN_PREMIUM, MAX_PREMIUM] exist and `find_entry_strikes(all_option_data, nifty_spot)` is not `None`, stop.  
  - Return `(all_option_data, option_symbols, found_matching_premiums)`.

- **`fetch_strikes_smart_async(...)`**  
  - Same logic; per level, `asyncio.gather` with `Semaphore(ASYNC_CONCURRENT_LIMIT)` around `get_option_ohlcv_realtime_async`.  
  - Brokers remain sync; only the HTTP/IO is concurrent.

#### 3.4.2 Database (`db.py`)

- **`setup_database(config)`**  
  - If not `USE_DATABASE`, return `None`.  
  - `sqlite3.connect(LOG_DIR / DATABASE_NAME)`, create `options_data`, `nifty_spot`, `volume_analysis` and indexes.  
  - `_migrate_database_schema(cursor)` to add Greeks, etc., if missing.  
  - Return `conn`.

- **`save_options_data_to_db(conn, all_option_data, timestamp_str, date_str)`**  
  - Insert into `options_data`; support schema with and without Greek columns.

- **`save_nifty_spot_to_db(conn, nifty_spot, atm_strike, timestamp_str, date_str)`**

- **`save_volume_analysis_to_db(conn, atm_strike, otm_call_volume, otm_put_volume, condition_met, condition_type, timestamp_str, date_str)`**

- **`should_take_snapshot()`**  
  - Uses `last_snapshot_time` and `DATA_SNAPSHOT_INTERVAL`; returns `bool` and updates `last_snapshot_time` when True.

- **`get_nifty_close_price(conn, date_str)`**  
  - Last `nifty_spot` for that date.

- **`log_end_of_day_summary(conn, date_str)`**  
  - Logs Nifty close and last volume analysis row.

---

### 3.5 Market (`market/`)

- **`is_market_open()`**: 9:15–15:30 IST, Mon–Fri; `pytz` preferred, fallback to naive with warning.  
- **`wait_for_market_open()`**: if already open, return; else sleep in steps until 9:15 IST; after 15:30, next trading day.  
- **`get_nearest_expiry()`**: next Tuesday; if today is Tuesday and >= 15:00, next Tuesday.

---

### 3.6 Main (`main.py`)

- Load `config`, `setup_logging`, `setup_database`, `get_broker`, `broker.initialize()`.  
- `algo_trade = AlgoTrade()`.  
- **Step 0**: `check_existing_positions` → restore → `monitor_exit_condition` → optional continue-logging or return.  
- **Steps 1–4**: `wait_for_market_open` if needed; Nifty spot (with `NIFTY_PREOPEN_PRICE` fallback); `get_strike_prices`, `get_nearest_expiry`; fetch via `fetch_strikes_smart` / `fetch_strikes_smart_async` or full loop; DB/CSV in non–fast mode.  
- **Steps 5–6**: Entry condition (volume or `BYPASS_ENTRY_CONDITION`); `find_entry_strikes` with retry loop; `enter_trade`; WebSocket if Fyers and enabled; `monitor_exit_condition`.  
- **Step 7**: If `trade_completed` and `CONTINUE_LOGGING_AFTER_TRADE`, loop: sleep `DATA_SNAPSHOT_INTERVAL`, fetch spot and options, `save_*_to_db`.  
- **`finally`**: `log_end_of_day_summary`, `conn.close()`.

---

## 4. Data Flow

### 4.1 Common Option Record (internal)

After `get_option_ohlcv_realtime`, every option is represented as:

```python
{
    'Symbol': str,
    'Open': float, 'High': float, 'Low': float, 'Close': float,
    'LTP': float, 'Volume': int, 'Prev_Close': float,
    'Delta'|'Gamma'|'Theta'|'Vega'|'IV': float|None,
    'Timestamp': str,
    'Strike': int,        # set by fetch / strike logic
    'Option_Type': 'CE'|'PE'
}
```

- `Strategy` and `find_entry_strikes` rely on `Option_Type`, `Strike`, `LTP`, `Volume`, `Symbol`.  
- `db` maps this to `options_data` columns (with or without Greeks).

### 4.2 Symbol Format by Broker

- **Fyers**: `NSE:NIFTY{YY}{M}{DD}{strike}{CE|PE}`.  
- **Angel**: `NSE:NIFTY{DD}{MMM}{YY}{strike}{CE|PE}`.  
- `get_option_symbol(..., broker)` ensures the correct format for orders and quotes.

### 4.3 WebSocket vs Polling

- **Fyers**: `connect_websocket` → `FyersDataSocket`; handler’s `on_message` fills `realtime_data[symbol]`; `get_realtime_ohlcv(symbol)` used in `monitor_exit_condition`.  
- **Angel**: `connect_websocket` → `(None, None)`; `handler` is always `None` in `monitor_exit_condition` → polling with `get_option_ohlcv_realtime` at `API_POLLING_INTERVAL`.

---

## 5. Configuration (Design)

- **Source**: `.env` via `python-dotenv`; one `config` (or `Config` dataclass) built at import or in `main` before any use.  
- **Broker-specific**: Fyers vs Angel credentials and toggles (e.g. `ANGEL_LOAD_CONTRACT_MASTER`) only loaded when that broker is selected.  
- **Strategy**: All thresholds (premium, total, target profit, volume ratio, bypass) in config so they can change without touching code.  
- **Feature flags**: `DRY_RUN`, `USE_WEBSOCKET`, `USE_DATABASE`, `USE_ASYNC_FETCHING`, `FAST_ENTRY_MODE`, `BYPASS_ENTRY_CONDITION`, `CONTINUE_LOGGING_AFTER_TRADE` drive branches in `main` and lower layers.

---

## 6. Error Handling and Resilience

- **Broker init**: `ImportError`, `ValueError`, `Exception` → log, print user-facing message, exit.  
- **Quotes / spot**: On failure return `None`; callers (`get_nifty_spot_price`, `get_option_ohlcv_realtime`, `find_entry_strikes`, `monitor_exit_condition`) handle `None` or empty data (retry, skip, or abort as in current behavior).  
- **Orders**: `place_order` catches, logs, returns broker response; `enter_trade` / `exit_trade` check response and log success/failure; no automatic retries on order failure in the base design.  
- **DB**: `save_*` and migration in `try/except`; log and continue so DB issues don’t stop the algo.  
- **`monitor_exit_condition`**: `KeyboardInterrupt` breaks the loop; other `Exception` log and `sleep` to avoid tight error loops.  
- **Main**: `try` around the whole run; `finally` for `log_end_of_day_summary` and `conn.close()`.

---

## 7. Security Considerations

- **Secrets**: All in `.env`; never in code or logs.  
- **Tokens**: Access token, JWT, TOTP secret only in env and broker internals.  
- **DB**: `algo_trading_data.db` under `LOG_DIR`; ensure `LOG_DIR` is not web-accessible.  
- **Orders**: With `DRY_RUN=True`, `place_order` is never called; `enter_trade`/`exit_trade` still execute all logic except the actual API call.

---

## 8. Possible Extensions

- **More brokers**: New `BrokerInterface` implementation and `get_broker` branch; `get_option_ohlcv_realtime` and `check_existing_positions` get an `isinstance` branch or a small adapter.  
- **Stop loss**: `AlgoTrade.check_exit` could also return True when `profit <= -stop_loss_points`; `exit_trade` would need an extra `exit_reason` or similar.  
- **Multiple trades per day**: `MAX_TRADES_PER_DAY` in config; in `main`, after `exit_trade` increment a counter and, if >= max, skip further `enter_trade` and optionally only run continue-logging.  
- **Backtest**: `get_historical_option_data` (Fyers) or similar in a `data/history.py`; a separate `backtest` script would use `strategy` and `data` with a mock broker that returns historical bars.  
- **Angel WebSocket**: Implement `connect_websocket` in `AngelBroker` and a handler that fills `realtime_data`; then `monitor_exit_condition` can use `handler` for Angel as well.

---

## 9. Glossary

| Term | Meaning |
|------|---------|
| **ATM** | At-the-money; strike closest to Nifty spot (rounded to 50). |
| **OTM Call** | Call with strike &gt; ATM. |
| **OTM Put** | Put with strike &lt; ATM. |
| **Entry condition** | OTM call volume ≥ 2× OTM put volume or the reverse (or bypass for testing). |
| **Entry strikes** | One CE and one PE with premium in [43,57] and total ≤ MAX_TOTAL_PREMIUM. |
| **Target profit** | Exit when (call_ltp + put_ltp) − entry_total ≥ TARGET_PROFIT_POINTS. |
| **Smart fetch** | Expand from ATM level by level and stop when a valid entry pair is found. |
| **Fast entry mode** | Use smart fetch and, if enabled, async concurrent quote fetches. |

---

## 10. References

- **Strategy**: `ALGO_STRATEGY.md`  
- **Brokers**: `BROKER_SWITCH_GUIDE.md`  
- **Implementation steps**: `REWRITE_STEPS.md`  
- **Current mono-file**: `nifty_options_algo.py` (for backward compatibility and migration)
