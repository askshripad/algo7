# Nifty Options Algo — Rewrite Steps Guide

A **step-by-step guide** for an intermediate Python developer to rewrite `nifty_options_algo.py`.  
The layout is **strategy-pluggable**: you implement one strategy first (**Strangle_0915**), and **reuse** `brokers/`, `data/`, `market/`, and `core/` when adding new strategies.

---

## Strategy: Strangle_0915 (this rewrite)

- **Entry**: Take a trade when **total premium (Call + Put) ≤ 112**. No volume check.
- **Strike selection**: Any CE + any PE whose LTP sum ≤ `MAX_TOTAL_PREMIUM` (112); prefer strikes closer to Nifty spot. Per-leg premium range (e.g. 43–57) is **not** used.
- **Exit**: When combined profit ≥ `TARGET_PROFIT_POINTS` (e.g. 7).

---

## Prerequisites

- **Python**: 3.9+ (for `asyncio.to_thread`, pathlib, etc.)
- **Concepts**: Classes, `abc`, `asyncio`, env vars, SQLite, logging
- **Setup**: `.env` with broker credentials, `requirements.txt` installed
- **Reference**: `BROKER_SWITCH_GUIDE.md`

---

## Phase 0: Project Setup & Structure

### Step 0.1 — Create a New Project Layout

Use a **reusable** layout: **core** (shared) + **strategies** (pluggable). New strategies reuse `brokers/`, `data/`, `market/`, and `core/`.

```
algo7/
├── src/
│   └── nifty_algo/
│       ├── __init__.py
│       ├── config.py              # All configuration (from .env + defaults)
│       ├── brokers/               # SHARED: broker abstraction
│       │   ├── __init__.py
│       │   ├── base.py            # BrokerInterface (ABC)
│       │   ├── fyers.py           # FyersBroker
│       │   └── angel.py           # AngelBroker
│       ├── data/                  # SHARED: fetch, DB
│       │   ├── __init__.py
│       │   ├── db.py              # SQLite, save_*_to_db, migrations
│       │   └── fetch.py           # get_option_ohlcv, fetch_strikes_smart(_async)
│       ├── market/                # SHARED: hours, expiry
│       │   ├── __init__.py
│       │   ├── hours.py           # is_market_open, wait_for_market_open
│       │   └── expiry.py          # get_nearest_expiry
│       ├── core/                  # SHARED: reusable logic for any strategy
│       │   ├── __init__.py
│       │   ├── strikes.py         # round_to_nearest_50, get_strike_prices, get_option_symbol,
│       │   │                       # find_entry_strikes(max_total, min_premium=None, max_premium=None)
│       │   ├── trade.py           # AlgoTrade, check_existing_positions, monitor_exit_condition
│       │   └── volume.py          # (optional) calculate_otm_volumes, check_entry_condition
│       │                           # — for strategies that use volume (e.g. future VolumeRatio_0915)
│       ├── strategies/            # PLUGGABLE: one module per strategy
│       │   ├── __init__.py        # get_strategy(name), STRATEGIES registry
│       │   ├── base.py            # Strategy (ABC): name, should_enter(ctx), get_entry_strikes(ctx)
│       │   └── strangle_0915.py   # Strangle_0915: total premium ≤ 112 only, no volume
│       └── main.py                # main() — loads strategy by config.STRATEGY, generic flow
├── algo_logs/
├── .env
├── requirements.txt
└── run.py                         # Entry: from nifty_algo.main import main; main()
```

**Action**: Create the folders and empty `__init__.py` / placeholder modules. Import `main` from `src.nifty_algo.main` in `run.py` and run it (it can just `print("ok")` for now).

---

### Step 0.2 — Move Configuration to `config.py`

**Goal**: Single source of config: env + defaults. Some keys are used only by certain strategies.

1. Create `src/nifty_algo/config.py`.
2. Use `python-dotenv` and `os.getenv()` for:
   - **Strategy selection**: `STRATEGY` (default `strangle_0915`)
   - **Broker**: `BROKER`, `DRY_RUN`, `LOG_DIR`, `USE_DATABASE`, `DATABASE_NAME`
   - **Strangle_0915 (and reusable)**: `MAX_TOTAL_PREMIUM` (default `112`), `TARGET_PROFIT_POINTS` (e.g. `7`), `STRIKE_INTERVAL`, `NUM_STRIKES_ABOVE_BELOW`, `FAST_ENTRY_MODE`, `FAST_ENTRY_MAX_STRIKES`
   - **Other strategies (optional)**: `MIN_PREMIUM`, `MAX_PREMIUM`, `VOLUME_RATIO_THRESHOLD`, `BYPASS_ENTRY_CONDITION` — not used by Strangle_0915; keep for future strategies.
   - **Fetch/execution**: `USE_ASYNC_FETCHING`, `ASYNC_CONCURRENT_LIMIT`, `USE_WEBSOCKET`, `API_POLLING_INTERVAL`, `DATA_SNAPSHOT_INTERVAL`, `NIFTY_PREOPEN_PRICE`, `STRIKE_SEARCH_RETRY_INTERVAL`, `STRIKE_SEARCH_MAX_RETRIES`
   - **Fyers**: `FYERS_CLIENT_ID`, `FYERS_ACCESS_TOKEN`
   - **Angel**: `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_MPIN`, `ANGEL_TOTP_SECRET`, `ANGEL_LOAD_CONTRACT_MASTER`
3. Parse types: `int()`, `float()`, `bool` (e.g. `'true'.lower() == 'true'`).
4. Export a `Config` dataclass or a module-level `config` object.

**Example for Strangle_0915**: `STRATEGY=strangle_0915`, `MAX_TOTAL_PREMIUM=112`.

**Check**: Print `config.STRATEGY`, `config.MAX_TOTAL_PREMIUM` in `run.py` and confirm they match `.env`.

---

### Step 0.3 — Centralize Logging

1. In `config.py` or a new `logging_config.py`, define `setup_logging(log_dir, dry_run)` that:
   - Creates `log_dir` and a daily log file.
   - Uses `logging.basicConfig(..., force=True)` with one `FileHandler` and one `StreamHandler`.
   - Logs session start and DRY_RUN vs LIVE.
2. Return the root or app logger; store in a module variable if needed (e.g. `logger = setup_logging(...)` in `main`).
3. Call `setup_logging` once at the very start of `main()`.

**Check**: Run `run.py` and see one log file under `algo_logs/` and matching console output.

---

## Phase 1: Broker Abstraction

### Step 1.1 — Define `BrokerInterface` (ABC)

1. Create `src/nifty_algo/brokers/base.py`.
2. Define an abstract class `BrokerInterface(ABC)` with:

   ```python
   @abstractmethod
   def initialize(self) -> bool: ...
   @abstractmethod
   def get_profile(self) -> dict: ...
   @abstractmethod
   def get_spot_price(self, symbol: str) -> float | None: ...
   @abstractmethod
   def get_option_quote(self, symbol: str) -> dict | None: ...
   @abstractmethod
   def get_positions(self) -> dict | None: ...
   @abstractmethod
   def place_order(self, symbol, side, qty, order_type="MARKET", price=0): ...
   @abstractmethod
   def get_option_symbol(self, expiry_date, strike_price, option_type: str) -> str: ...
   @abstractmethod
   def connect_websocket(self, symbols, handler) -> tuple: ...  # (ws, handler) or (None, None)
   ```

3. Add short docstrings for each. Use `Optional` or `| None` in type hints if you like.

**Check**: Import `BrokerInterface` and ensure it cannot be instantiated.

---

### Step 1.2 — Implement `FyersBroker`

1. Create `src/nifty_algo/brokers/fyers.py`.
2. Optional imports: `try/except ImportError` for `fyers_apiv3` and `FyersWebsocket.data_ws`; set `FYERS_AVAILABLE`, `FYERS_WEBSOCKET_AVAILABLE`.
3. Implement `FyersBroker(BrokerInterface)`:
   - `__init__`: read `config.FYERS_CLIENT_ID`, `config.FYERS_ACCESS_TOKEN`; handle token with/without `:` prefix.
   - `initialize`: build `FyersModel`, call `get_profile`, raise on failure.
   - `get_spot_price`, `get_option_quote`, `get_positions`, `place_order`: wrap `model.quotes`, `model.positions`, `model.place_order`; normalize keys (e.g. `lp` → `ltp` in a common format if you introduce one later).
   - `get_option_symbol`: `NSE:NIFTY{YY}{M}{DD}{strike}{CE|PE}` (month without leading zero).
   - `connect_websocket`: if `USE_WEBSOCKET` and websocket lib available, create `FyersDataSocket`, connect, subscribe; return `(fyers_ws, handler)`, else `(None, None)`.
4. Keep response parsing (e.g. `s == 'ok'`, `d`) inside Fyers-specific code.

**Check**: With `BROKER=fyers` and valid `.env`, call `broker.initialize()`, `get_spot_price(NIFTY_SYMBOL)`, and `get_option_quote(one_option_symbol)`. Assert no crash and that you get numbers.

---

### Step 1.3 — Implement `AngelBroker`

1. Create `src/nifty_algo/brokers/angel.py`.
2. Optional import for `SmartConnect` and `pyotp`; set `ANGEL_AVAILABLE`.
3. Implement `AngelBroker(BrokerInterface)`:
   - `__init__`: api_key, client_code, mpin, totp_secret; `symbol_token_cache`, `failed_lookups`, `contract_master`, `master_cache_file`, `_load_contract_master` logic.
   - `initialize`: `SmartConnect`, TOTP, `generateSession` with mpin; `getfeedToken`, `_load_contract_master` if enabled.
   - `get_spot_price`: use `ltpData` with Nifty index token (e.g. `99926000` or from a small mapping).
   - `get_option_quote`: prefer `marketDataFull`; fallback to `ltpData` with `_get_option_token`. Use `_get_exchange_for_symbol` (NFO for options). Rate limiting and `_get_option_token` (master + `searchScrip`) as in the current code.
   - `get_option_symbol`: `NSE:NIFTY{DD}{MMM}{YY}{strike}{CE|PE}`.
   - `get_positions`, `place_order`: delegate to `smart_api.position()`, `smart_api.placeOrder` with correct `exchange` and `symboltoken`.
   - `connect_websocket`: return `(None, None)` with a log that Angel WS is not implemented; the rest of the app will use polling.
4. Copy over `_get_token_from_master`, `_get_option_token`, and the `searchScrip` fallback logic, keeping them inside `angel.py`.

**Check**: With `BROKER=angel` and valid `.env`, run `initialize()`, `get_spot_price`, `get_option_quote` for one Nifty option. Fix any symbol/format issues against Angel’s docs.

---

### Step 1.4 — Broker Factory

1. In `brokers/__init__.py` (or a `factory.py`), implement `get_broker(config) -> BrokerInterface`:
   - `config.BROKER == 'fyers'` → `FyersBroker()`
   - `config.BROKER == 'angel'` → `AngelBroker()`
   - else `raise ValueError(...)`.
2. Ensure both brokers receive config (via constructor or a shared `config` module).

**Check**: `get_broker(config)` returns the correct type for `BROKER=fyers` and `BROKER=angel`.

---

## Phase 2: Market, Core (Reusable), and Strategy Layer

### Step 2.1 — Market Hours and Expiry (shared)

1. **`market/hours.py`**:
   - `is_market_open()`: 9:15–15:30 IST, Mon–Fri; use `pytz` if available, else naive `datetime` and a warning.
   - `wait_for_market_open()`: if already open, return; else sleep in a loop (e.g. 10s) until 9:15 IST; handle post-15:30 → next trading day.
2. **`market/expiry.py`**:
   - `get_nearest_expiry()`: next Tuesday; if today is Tuesday and >= 15:00, take the next Tuesday.

**Check**: Unit tests or a small script that prints `is_market_open()`, `get_nearest_expiry()` for a few fixed dates/times.

---

### Step 2.2 — Core: Strikes and `find_entry_strikes` (reusable)

1. **`core/strikes.py`**:
   - `round_to_nearest_50(price)` → `round(price/50)*50`.
   - `get_strike_prices(spot, fast_mode=False)`: ATM, `strikes_above`, `strikes_below`, `all_strikes`; when `fast_mode` use `FAST_ENTRY_MAX_STRIKES` else `NUM_STRIKES_ABOVE_BELOW`.
   - `get_option_symbol(expiry_date, strike, option_type, broker)` → `broker.get_option_symbol(...)`.
   - **`find_entry_strikes(all_option_data, nifty_spot, max_total_premium, min_premium=None, max_premium=None)`**:
     - Build `call_candidates`: CE with `LTP` in `[min_premium, max_premium]` if both are not `None`, else **any** CE.
     - Build `put_candidates`: PE with the same filter.
     - Among pairs with `call_premium + put_premium <= max_total_premium`, pick the one with **minimum total distance from spot** (prefer strikes closer to Nifty).
     - Return `(call_strike, put_strike, call_price, put_price, call_symbol, put_symbol)` or `None`.
2. Use `config` for `STRIKE_INTERVAL`, `NUM_STRIKES_ABOVE_BELOW`, `FAST_ENTRY_MAX_STRIKES`.

**Reuse**: Strangle_0915 passes `min_premium=None, max_premium=None` (only total matters). A future strategy can pass 43 and 57 for per-leg filter.

**Check**: With `min_premium=None, max_premium=None` and two CE+PE with total 100, `find_entry_strikes(..., 112, None, None)` returns that pair. With `min_premium=43, max_premium=57`, it filters by per-leg LTP.

---

### Step 2.3 — Core: Trade (reusable)

1. **`core/trade.py`**:
   - `AlgoTrade`: `entry_*`, `is_position_open`, `call_symbol`, `put_symbol`, `trade_completed`, `call_order_id`, `put_order_id`; `restore_from_positions`, `enter_trade`, `_place_order`, `check_exit` (profit >= `TARGET_PROFIT_POINTS`), `exit_trade`.
   - `check_existing_positions(broker)`: parse `get_positions()` for Nifty CE/PE with `netQty > 0`; return `(call_symbol, put_symbol, call_strike, put_strike, call_price, put_price)` or `None`.
   - `monitor_exit_condition(broker, algo_trade, handler=None, check_interval=None)`: loop while `is_position_open`; get LTP from handler or `get_option_ohlcv_realtime`; if `algo_trade.check_exit(...)`, call `exit_trade` and break.

**Check**: `AlgoTrade`, `enter_trade` (DRY_RUN), `exit_trade`, and `monitor_exit_condition` behave as before.

---

### Step 2.4 — (Optional) Core: Volume helpers for other strategies

1. **`core/volume.py`** (not used by Strangle_0915; add when you build a volume-based strategy):
   - `calculate_otm_volumes(all_option_data, atm_strike)` → `(otm_call_volume, otm_put_volume)`.
   - `check_entry_condition(otm_call_volume, otm_put_volume)` → `(bool, str)` e.g. `(True, "CALL_VOLUME_DOMINANT")` when call_vol >= 2×put_vol or the reverse.

**Check**: Skip for Strangle_0915; test when implementing e.g. VolumeRatio_0915.

---

### Step 2.5 — Strategy interface and Strangle_0915

1. **`strategies/base.py`** — abstract `Strategy`:
   ```python
   class Strategy(ABC):
       name: str
       @abstractmethod
       def should_enter(self, ctx) -> bool: ...       # ctx: all_option_data, nifty_spot, atm_strike, config, etc.
       @abstractmethod
       def get_entry_strikes(self, all_option_data, nifty_spot, config) -> tuple | None: ...
       # Optional: get_volume_analysis(ctx) -> dict | None for DB; Strangle_0915 returns None
   ```

2. **`strategies/strangle_0915.py`** — `Strangle0915Strategy(Strategy)`:
   - `name = "Strangle_0915"`.
   - **`should_enter(ctx) -> True`** always (no volume or other filter; we only need a pair with total ≤ 112).
   - **`get_entry_strikes(all_option_data, nifty_spot, config)`**:
     - Call **`find_entry_strikes(all_option_data, nifty_spot, config.MAX_TOTAL_PREMIUM, min_premium=None, max_premium=None)`** from `core.strikes`.
     - Return the 6-tuple or `None`.
   - No `get_volume_analysis` (or return `None`); no volume DB writes.

3. **`strategies/__init__.py`**:
   - `STRATEGIES = {"strangle_0915": Strangle0915Strategy}`.
   - `get_strategy(name: str) -> Strategy`; raise `ValueError` if unknown.

**Check**: `get_strategy("strangle_0915")` returns `Strangle0915Strategy`. For `all_option_data` with one CE+PE totaling 100, `strategy.get_entry_strikes(..., config)` returns that pair; `strategy.should_enter(ctx)` is `True`.

---

## Phase 3: Data Fetching & DB

### Step 3.1 — Option OHLCV and Normalization

1. **`data/fetch.py`**:
   - `get_option_ohlcv_realtime(broker, symbol)`:
     - Call `broker.get_option_quote(symbol)`.
     - If Fyers: map `open_price`, `high_price`, `low_price`, `lp`, `volume`, `prev_close_price`, and Greeks if present.
     - If Angel: map `open`, `high`, `low`, `ltp`, `volume`, `previousClose`; Greeks can be `None`.
     - Return a **common dict** e.g. `Symbol, Open, High, Low, Close, LTP, Volume, Prev_Close, Delta, Gamma, Theta, Vega, IV, Timestamp`. Use `isinstance(broker, FyersBroker)` or `AngelBroker)` only here (or in a `BrokerAdapter` if you prefer).
   - `get_option_ohlcv_realtime_async(broker, symbol)`: run `get_option_ohlcv_realtime` in `asyncio.to_thread` or `run_in_executor`.
2. **`get_nifty_spot_price(broker)`**: `broker.get_spot_price(NIFTY_SYMBOL)`.

**Check**: For one CE and one PE, `get_option_ohlcv_realtime` returns the common shape; async version returns the same.

---

### Step 3.2 — Fetch Strikes (Smart and Async)

1. In `data/fetch.py`:
   - **`fetch_strikes_smart(broker, expiry, atm_strike, nifty_spot, get_entry_strikes_fn)`**:
     - `get_entry_strikes_fn(all_option_data, nifty_spot) -> tuple | None` — provided by the **strategy** (e.g. `lambda d, s: strategy.get_entry_strikes(d, s, config)`).
     - Expand level 0 (ATM), 1, 2…; at each level fetch CE/PE for new strikes.
     - **Stop when** `get_entry_strikes_fn(all_option_data, nifty_spot)` returns a pair (not `None`).
     - Return `(all_option_data, option_symbols, found)` where `found = (get_entry_strikes_fn(...) is not None)`.
   - **`fetch_strikes_smart_async`**: same contract, with `asyncio.gather` and `Semaphore(ASYNC_CONCURRENT_LIMIT)` around `get_option_ohlcv_realtime_async`.
2. Reuse `get_option_symbol` from `core.strikes`. The strategy’s `get_entry_strikes` typically calls `core.strikes.find_entry_strikes`.

**Reuse**: For Strangle_0915, `get_entry_strikes_fn` uses total ≤ 112 only. For a volume+premium strategy, it would use `find_entry_strikes(..., min_premium=43, max_premium=57)` and only be called when `should_enter` is True.

**Check**: With a strategy and mocks: `fetch_strikes_smart(..., get_entry_strikes_fn=...)` stops when the strategy’s `get_entry_strikes` returns a pair.

---

### Step 3.3 — Database Layer

1. **`data/db.py`**:
   - `setup_database(config)`:
     - If not `USE_DATABASE`, return `None`.
     - `sqlite3.connect(LOG_DIR / DATABASE_NAME)`, create `options_data`, `nifty_spot`, `volume_analysis` and indexes; run `_migrate_database_schema(cursor)` to add Greeks etc. if missing.
   - `save_options_data_to_db(conn, all_option_data, timestamp_str, date_str)`: insert into `options_data`; support both schemas with/without Greeks.
   - `save_nifty_spot_to_db(conn, nifty_spot, atm_strike, timestamp_str, date_str)`.
   - `save_volume_analysis_to_db(conn, atm_strike, otm_call_volume, otm_put_volume, condition_met, condition_type, timestamp_str, date_str)` — **optional**: only call when the strategy provides volume analysis (e.g. `strategy.get_volume_analysis(ctx)` returns data). Strangle_0915 does not.
   - `should_take_snapshot()`: use a module-level or injected `last_snapshot_time` and `DATA_SNAPSHOT_INTERVAL`.
   - `get_nifty_close_price(conn, date_str)`, `log_end_of_day_summary(conn, date_str)`.
2. Use `config` for paths, intervals, and `USE_DATABASE`.

**Check**: Call `setup_database`, `save_nifty_spot_to_db`, `save_options_data_to_db` once; inspect SQLite and `log_end_of_day_summary` output.

---

## Phase 4: Main Orchestration

### Step 4.1 — Skeleton of `main()`

1. In `src/nifty_algo/main.py`:
   - Load `config`, `setup_logging(config.LOG_DIR, config.DRY_RUN)`, `db_conn = setup_database(config)`.
   - `strategy = get_strategy(config.STRATEGY)`, `broker = get_broker(config)`, `broker.initialize()`; on failure (ImportError, ValueError, etc.) log and return.
   - `algo_trade = AlgoTrade()` (from `core.trade`).
   - In a `try:` block run the steps below; in `finally:` call `log_end_of_day_summary(db_conn, date_str)` if `db_conn` and `logger`, then `db_conn.close()` if `db_conn`.
2. Implement step by step in the next substeps; start with “no existing positions” path only.

**Check**: `main()` runs without error up to broker init, `get_strategy("strangle_0915")`, and `AlgoTrade()`.

---

### Step 4.2 — “Existing Positions” Path

1. **Step 0**: `existing = check_existing_positions(broker)` (from `core.trade`).
2. If `existing`:
   - Unpack `(call_symbol, put_symbol, call_strike, put_strike, call_price, put_price)`.
   - `algo_trade.restore_from_positions(...)`.
   - `nifty_spot = get_nifty_spot_price(broker) or 0`.
   - Optionally build `option_symbols = [call_symbol, put_symbol]` and connect WebSocket if `USE_WEBSOCKET` and Fyers; else `handler = None`.
   - `monitor_exit_condition(broker, algo_trade, handler, check_interval=API_POLLING_INTERVAL)`.
   - If `CONTINUE_LOGGING_AFTER_TRADE`: you can jump to the “continue logging” loop (Step 4.6) or `return` for now.
   - `return`.
3. Else: log “No existing positions”, continue.

**Check**: With manually opened CE+PE, run `main`; it should restore, monitor, and (in DRY_RUN with mocks or live) exit when target is hit.

---

### Step 4.3 — Market Open and Nifty Spot

1. If not `is_market_open()`: `wait_for_market_open()` then recheck.
2. **Step 1**: Nifty spot:
   - If `NIFTY_PREOPEN_PRICE` and not `is_market_open()`: use it.
   - Else: `nifty_spot = get_nifty_spot_price(broker)`; if `None` and `NIFTY_PREOPEN_PRICE`: use preopen; else log error and return.
3. Log Nifty spot and, if needed, ATM.

**Check**: Run before/after 9:15; with `NIFTY_PREOPEN_PRICE` set, it should use it when market is closed.

---

### Step 4.4 — ATM, Expiry, Fetch Option Data

1. **Step 2**: `atm_strike, strikes_above, strikes_below, all_strikes = get_strike_prices(nifty_spot, fast_mode=FAST_ENTRY_MODE)` (from `core.strikes`).
2. **Step 3**: `expiry_date = get_nearest_expiry()`.
3. **Step 4** — define **`get_entry_strikes_fn = lambda d, s: strategy.get_entry_strikes(d, s, config)`**:
   - If `FAST_ENTRY_MODE` and `USE_ASYNC_FETCHING`: `all_option_data, option_symbols, _ = asyncio.run(fetch_strikes_smart_async(broker, expiry_date, atm_strike, nifty_spot, get_entry_strikes_fn))`.
   - Elif `FAST_ENTRY_MODE`: `all_option_data, option_symbols, _ = fetch_strikes_smart(broker, expiry_date, atm_strike, nifty_spot, get_entry_strikes_fn)`.
   - Else: loop over `all_strikes`, `get_option_symbol` and `get_option_ohlcv_realtime` for CE/PE, append to `all_option_data` and `option_symbols`; respect Angel rate limiting.
4. `timestamp_str`, `date_str` and, in non–fast mode, DB snapshot + CSV summary (reuse `should_take_snapshot`, `save_*_to_db`).

**Check**: You get `all_option_data` and `option_symbols`; in fast mode, fetch stops when `strategy.get_entry_strikes(...)` returns a pair (for Strangle_0915: total ≤ 112).

---

### Step 4.5 — Entry and Strike Search (strategy-driven)

1. **Entry condition**: Build `ctx = {"all_option_data": all_option_data, "nifty_spot": nifty_spot, "atm_strike": atm_strike, "config": config}` (or a subset). `condition_met = strategy.should_enter(ctx)`. For **Strangle_0915** this is always `True` (no volume check).  
   - **Optional for other strategies**: if the strategy has `get_volume_analysis(ctx)` and it returns data, call `save_volume_analysis_to_db(...)`. Strangle_0915 does not.
2. If `algo_trade.trade_completed`: set `condition_met = False`.
3. If `condition_met`:
   - `entry_data = strategy.get_entry_strikes(all_option_data, nifty_spot, config)`.
   - **Retry loop**: while not `entry_data`:
     - If `STRIKE_SEARCH_MAX_RETRIES > 0` and `retry_count >= STRIKE_SEARCH_MAX_RETRIES`: break.
     - `time.sleep(STRIKE_SEARCH_RETRY_INTERVAL)`; refresh `nifty_spot`, `atm_strike`, `all_strikes`; re-fetch `all_option_data` for `all_strikes`; `entry_data = strategy.get_entry_strikes(all_option_data, nifty_spot, config)`.
     - On `KeyboardInterrupt`: break.
   - If `entry_data`:
     - `algo_trade.enter_trade(..., broker)`.
     - In fast mode: deferred `save_nifty_spot_to_db`, `save_options_data_to_db` on snapshot.
     - Connect WebSocket if desired (same as in Step 4.2) for `handler`.
     - `monitor_exit_condition(broker, algo_trade, handler, API_POLLING_INTERVAL)`.

**Check**: For Strangle_0915, `should_enter` is always True; with `all_option_data` that has a CE+PE pair with total ≤ 112, `get_entry_strikes` returns it and you enter, monitor, and exit.

---

### Step 4.6 — WebSocket vs Polling and “Continue Logging”

1. After the entry/exit block:
   - If `algo_trade.is_position_open` and WebSocket wasn’t used for exit yet: connect WS if `USE_WEBSOCKET` and Fyers, then `monitor_exit_condition(..., handler)`; on failure or Angel, use `handler=None` and polling.
2. **Continue logging** (when `algo_trade.trade_completed` and `CONTINUE_LOGGING_AFTER_TRADE`):
   - Loop: `time.sleep(DATA_SNAPSHOT_INTERVAL)`; `get_nifty_spot_price`, `get_strike_prices`, fetch CE/PE for `all_strikes`, `save_nifty_spot_to_db`, `save_options_data_to_db`; until `KeyboardInterrupt` or error.

**Check**: After a completed trade, the script keeps logging; Ctrl+C stops it. `finally` runs `log_end_of_day_summary` and closes DB.

---

### Step 4.7 — Historical and Cleanup

1. **Optional**: `get_historical_option_data` is Fyers-specific and used for backtest/analysis. Move to `brokers/fyers.py` or `data/history.py` and call only when needed; you can omit it in the first rewrite and add later.
2. Remove any global `logger`/`db_conn` that are now passed or obtained from `main`; ensure only `main` and `setup_logging` / `setup_database` create them.
3. In `run.py`, keep:

   ```python
   if __name__ == '__main__':
       try:
           main()
       finally:
           # log_end_of_day_summary and db close are inside main's finally
           pass
   ```

   or delegate everything to `main()`’s `finally`.

**Check**: A full run (DRY_RUN, with or without pre-opened positions) goes through all branches you care about; logs and DB match the original behavior.

---

## Phase 5: Hardening & Optional Improvements

### Step 5.1 — Error Handling and Logging

- In broker methods: `try/except`, log with `logger.error`/`logger.debug`, return `None` or raise only when it’s a hard failure (e.g. init).
- In `main` and `monitor_exit_condition`: catch `KeyboardInterrupt` and `Exception`, log, and exit or `continue` as in the current design.
- Avoid `print` in library code; use `logger.info`/`logger.warning` and let `main` or a thin CLI layer do `print` if you want to keep console output.

### Step 5.2 — Config and Env

- Validate required env vars at startup (e.g. if `BROKER=fyers` then `FYERS_CLIENT_ID` and `FYERS_ACCESS_TOKEN` must be set); fail fast with a clear message.
- Consider a `--dry-run` / `--live` override from `sys.argv` that overrides `config.DRY_RUN` for that run.

### Step 5.3 — Tests (Optional but Recommended)

- **Unit**: `round_to_nearest_50`, `get_strike_prices`, `find_entry_strikes` (with `min_premium=None, max_premium=None` and with 43/57), `Strangle0915Strategy.should_enter`, `get_entry_strikes`. For volume-based strategies: `calculate_otm_volumes`, `check_entry_condition`.
- **Integration**: Mock `BrokerInterface` and run `main` up to `enter_trade` and `monitor_exit_condition` to `exit_trade`.

### Step 5.4 — Type Hints and Docs

- Add type hints to `BrokerInterface`, `get_broker`, `find_entry_strikes`, `AlgoTrade`’s public methods, and `main`.
- One- or two-line docstrings for each public function and class; mention `ALGO_STRATEGY.md` in `strategy` and `config` in `config.py`.

---

## Quick Reference: Original vs New Locations

| Original (single file)         | New location(s) (reusable)                    |
|--------------------------------|-----------------------------------------------|
| Top-level config, `load_dotenv`| `config.py`                                   |
| `BrokerInterface`, `FyersBroker`, `AngelBroker` | `brokers/base.py`, `fyers.py`, `angel.py` |
| `get_broker`                   | `brokers/__init__.py`                         |
| `setup_logging`                | `config` or `logging_config` + `main`         |
| `setup_database`, `save_*_to_db`, `_migrate_*` | `data/db.py`                         |
| `round_to_nearest_50`, `get_strike_prices`, `get_option_symbol`, `find_entry_strikes` | `core/strikes.py`        |
| `calculate_otm_volumes`, `check_entry_condition` | `core/volume.py` (optional; for volume-based strategies) |
| `AlgoTrade`, `check_existing_positions`, `monitor_exit_condition` | `core/trade.py`        |
| `get_option_ohlcv_realtime`, `get_option_ohlcv_realtime_async`, `fetch_strikes_smart*`, `get_nifty_spot_price` | `data/fetch.py`   |
| `get_nearest_expiry`           | `market/expiry.py`                            |
| `is_market_open`, `wait_for_market_open` | `market/hours.py`                      |
| `FyersWebSocketHandler`, `connect_websocket` (Fyers) | `brokers/fyers.py`                  |
| Entry rule “total ≤ 112, no volume” | `strategies/strangle_0915.py`         |
| Strategy registry              | `strategies/__init__.py` (`get_strategy`)     |
| `main()`                       | `main.py`                                     |

---

## Tips for Intermediate Python

1. **Imports**: Prefer `from nifty_algo.config import config` and `from nifty_algo.brokers import get_broker` to avoid circular imports; put `config` and `logging` setup at the top of `main`.
2. **Async**: `asyncio.run(fetch_strikes_smart_async(...))` is enough if the rest of `main` is sync; you don’t need to make `main` async.
3. **Broker branching**: Prefer `isinstance(broker, FyersBroker)` only in `get_option_ohlcv_realtime` and `check_existing_positions` (and possibly `_place_order` for response shape); keep the rest broker-agnostic.
4. **DB**: Reuse one `conn` per process; pass it into `save_*` and `log_end_of_day_summary`; close in `main`’s `finally`.
5. **Incremental**: After each phase, run the script (or a small `test_phase_x.py`) and fix errors before moving on. Prefer smaller commits per step.
6. **Reuse for new strategies**: Implement only `strategies/my_strategy.py` and register it; `main`, `core/`, `data/`, `brokers/`, `market/` stay unchanged. Use `core.strikes.find_entry_strikes` (with your `min_premium`/`max_premium` or `None` for Strangle-style) and `core.volume` if needed.

---

## Adding a New Strategy (reuse core + data + brokers)

1. Create `strategies/my_strategy.py` with a class that implements `Strategy` (from `strategies.base`):
   - `name = "my_strategy"`
   - `should_enter(ctx) -> bool` — e.g. volume ratio, or always `True` for premium-only.
   - `get_entry_strikes(all_option_data, nifty_spot, config) -> tuple | None` — use `core.strikes.find_entry_strikes(..., max_total_premium, min_premium, max_premium)` with your rules. Use `core.volume.calculate_otm_volumes` and `check_entry_condition` if needed.
   - Optional: `get_volume_analysis(ctx) -> dict | None` for `save_volume_analysis_to_db`.
2. Register in `strategies/__init__.py`: `STRATEGIES["my_strategy"] = MyStrategy`.
3. Set `STRATEGY=my_strategy` in `.env` (or default in `config`).
4. `main` already uses `get_strategy(config.STRATEGY)` and calls `should_enter`, `get_entry_strikes`; no change to `main` or to `brokers/`, `data/`, `market/`, `core/`.

---

## Done When

- [ ] All phases 0–5 are implemented.
- [ ] `run.py` runs without import errors.
- [ ] **Strangle_0915**: With `STRATEGY=strangle_0915`, `MAX_TOTAL_PREMIUM=112`, `DRY_RUN=True`, and live broker: full flow (no existing positions → spot → strikes → fetch → entry when total ≤ 112 → monitor → exit at 7 points) completes. No volume check.
- [ ] With `DRY_RUN=True` and existing CE+PE positions, restore → monitor → exit works.
- [ ] `BROKER=angel` and `BROKER=fyers` both work for init, spot, option quote, and (for Fyers) WebSocket-based monitoring.
- [ ] `get_strategy("strangle_0915")` returns `Strangle0915Strategy`; a new strategy can be added via `strategies/` and `STRATEGY` without changing `main`, `core/`, `data/`, or `brokers/`.

Remaining gaps to fully match nifty_options_algo.py:
Fyers WebSocket handler + connection + fallback to polling.
CONTINUE_LOGGING_AFTER_TRADE loop.
CSV summary / dataframe export (if you want that behavior).
If you want, I can implement those next (they’re a bit larger but straightforward).
