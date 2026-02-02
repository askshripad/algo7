from __future__ import annotations

from datetime import datetime
import sys
import logging

from nifty_algo.config import config
from nifty_algo.logging_config import setup_logging
from nifty_algo.brokers import get_broker
from nifty_algo.strategies import get_strategy
from nifty_algo.core.trade import AlgoTrade, check_existing_positions, monitor_exit_condition
from nifty_algo.data.fetch import get_nifty_spot_price
from nifty_algo.market.hours import is_market_open, wait_for_market_open
from nifty_algo.core.strikes import round_to_nearest_50
from nifty_algo.core.strikes import get_strike_prices, get_option_symbol
from nifty_algo.market.expiry import get_nearest_expiry
from nifty_algo.data.fetch import (
    fetch_strikes_smart,
    fetch_strikes_smart_async,
    get_option_ohlcv_realtime,
)
from nifty_algo.data.db import (
    should_take_snapshot,
    save_options_data_to_db,
    save_nifty_spot_to_db,
)
import asyncio
import time

# TODO: replace with your actual DB helpers
from nifty_algo.data.db import setup_database, log_end_of_day_summary  # adjust if different


def _parse_run_mode(argv: list[str]) -> bool | None:
    if "--dry-run" in argv:
        return True
    if "--live" in argv:
        return False
    return None


def _validate_required_config(cfg) -> str | None:
    broker = (cfg.BROKER or "").lower().strip()
    if broker == "fyers":
        if not cfg.FYERS_CLIENT_ID or not cfg.FYERS_ACCESS_TOKEN:
            return "FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN are required for fyers."
    if broker == "angel":
        missing = []
        if not cfg.ANGEL_API_KEY:
            missing.append("ANGEL_API_KEY")
        if not cfg.ANGEL_CLIENT_CODE:
            missing.append("ANGEL_CLIENT_CODE")
        if not cfg.ANGEL_MPIN:
            missing.append("ANGEL_MPIN (or ANGEL_PASSWORD)")
        if not cfg.ANGEL_TOTP_SECRET:
            missing.append("ANGEL_TOTP_SECRET")
        if missing:
            return "Missing required Angel config: " + ", ".join(missing)
    return None


def _initialize_broker(cfg, logger):
    try:
        broker = get_broker(cfg)
        broker.initialize()
    except (ImportError, ValueError, Exception) as e:
        logger.error("Broker initialization failed: %s", e)
        return None
    try:
        profile = broker.get_profile()
        if isinstance(profile, dict):
            status = profile.get("status")
            code = profile.get("code")
            ok = profile.get("s")
            if status is False or ok == "error" or (code is not None and code != 200):
                logger.error("Broker profile check failed: %s", profile)
                return None
    except Exception as e:
        logger.warning("Broker profile check skipped: %s", e)
    return broker


def _handle_existing_positions(broker, algo_trade, logger) -> bool:
    existing = check_existing_positions(broker)
    if not existing:
        logger.info("No existing positions found. Continuing...")
        return False

    call_symbol, put_symbol, call_strike, put_strike, call_price, put_price = existing
    algo_trade.restore_from_positions(call_symbol, put_symbol, call_price, put_price)

    _ = get_nifty_spot_price(broker) or 0

    handler = None
    if config.USE_WEBSOCKET:
        handler = None

    monitor_exit_condition(
        broker,
        algo_trade,
        handler=handler,
        check_interval=config.API_POLLING_INTERVAL,
    )

    if config.CONTINUE_LOGGING_AFTER_TRADE:
        pass
    return True


def _get_nifty_spot_with_market_check(broker, logger) -> float | None:
    if not is_market_open():
        wait_for_market_open()

    if config.NIFTY_PREOPEN_PRICE and not is_market_open():
        nifty_spot = config.NIFTY_PREOPEN_PRICE
    else:
        nifty_spot = get_nifty_spot_price(broker)
        if nifty_spot is None and config.NIFTY_PREOPEN_PRICE:
            nifty_spot = config.NIFTY_PREOPEN_PRICE

    if nifty_spot is None:
        logger.error("Failed to get Nifty spot price.")
        return None

    atm_strike = round_to_nearest_50(nifty_spot)
    logger.info("Nifty spot: %s | ATM: %s", nifty_spot, atm_strike)
    return nifty_spot


def _fetch_option_data(broker, strategy, nifty_spot):
    logger = logging.getLogger(__name__)
    logger.info("STEP 2: Calculating ATM and Strike Prices")
    strike_info = get_strike_prices(nifty_spot, fast_mode=config.FAST_ENTRY_MODE)
    atm_strike = strike_info["atm"]
    all_strikes = strike_info["all_strikes"]
    logger.info("ATM Strike: %s", atm_strike)
    logger.info("Strikes Above ATM: %s", strike_info["strikes_above"])
    logger.info("Strikes Below ATM: %s", strike_info["strikes_below"])
    logger.info("All Strikes: %s", all_strikes)
    logger.info("STEP 3: Getting Option Expiry Date")
    expiry_date = get_nearest_expiry()
    logger.info("Nearest Expiry Date: %s", expiry_date.strftime("%d-%b-%Y"))
    try:
        example_symbol = get_option_symbol(expiry_date, atm_strike, "CE", broker)
        logger.info("Example ATM Symbol: %s", example_symbol)
    except Exception as exc:
        logger.debug("Failed to build example symbol: %s", exc)
    logger.info("STEP 4: Downloading OHLCV Data for All Strikes")

    get_entry_strikes_fn = lambda d, s: strategy.get_entry_strikes(d, s, config)
    all_option_data = []
    option_symbols = []

    if config.FAST_ENTRY_MODE and config.USE_ASYNC_FETCHING:
        all_option_data, option_symbols, _ = asyncio.run(
            fetch_strikes_smart_async(
                broker, expiry_date, atm_strike, nifty_spot, get_entry_strikes_fn
            )
        )
    elif config.FAST_ENTRY_MODE:
        all_option_data, option_symbols, _ = fetch_strikes_smart(
            broker, expiry_date, atm_strike, nifty_spot, get_entry_strikes_fn
        )
    else:
        for strike in all_strikes:
            ce_symbol = get_option_symbol(expiry_date, strike, "CE", broker)
            pe_symbol = get_option_symbol(expiry_date, strike, "PE", broker)

            ce_data = get_option_ohlcv_realtime(broker, ce_symbol)
            pe_data = get_option_ohlcv_realtime(broker, pe_symbol)

            if ce_data:
                ce_data["symbol"] = ce_symbol
                ce_data["strike"] = strike
                ce_data["option_type"] = "CE"
                all_option_data.append(ce_data)
                option_symbols.append(ce_symbol)

            if pe_data:
                pe_data["symbol"] = pe_symbol
                pe_data["strike"] = strike
                pe_data["option_type"] = "PE"
                all_option_data.append(pe_data)
                option_symbols.append(pe_symbol)
            _maybe_rate_limit_pause(broker)

    ltp_count = sum(1 for row in all_option_data if row.get("LTP") not in (None, 0))
    logger.info("Fetched option rows: %s | LTP > 0: %s", len(all_option_data), ltp_count)
    if all_option_data:
        sample_rows = all_option_data[:6]
        logger.info("Sample option data (first %s rows):", len(sample_rows))
        for row in sample_rows:
            logger.info(
                "Symbol: %s | Type: %s | Strike: %s | LTP: %s",
                row.get("symbol") or row.get("Symbol"),
                row.get("option_type"),
                row.get("strike"),
                row.get("LTP"),
            )
    if not all_option_data:
        logger.warning("No option data fetched. Check broker quote API/symbols.")

    return all_option_data, option_symbols, atm_strike, all_strikes, expiry_date


def _snapshot_non_fast(db_conn, all_option_data, nifty_spot, atm_strike):
    if db_conn and should_take_snapshot():
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        date_str = datetime.now().strftime("%Y-%m-%d")
        save_options_data_to_db(db_conn, all_option_data, timestamp_str, date_str)
        save_nifty_spot_to_db(db_conn, nifty_spot, atm_strike, timestamp_str, date_str)


def _retry_entry_search(
    broker,
    strategy,
    nifty_spot,
    expiry_date,
    get_entry_strikes_fn,
):
    logger = logging.getLogger(__name__)
    retry_count = 0
    entry_data = None
    while entry_data is None:
        if config.STRIKE_SEARCH_MAX_RETRIES > 0 and retry_count >= config.STRIKE_SEARCH_MAX_RETRIES:
            logger.info("Entry search retries exhausted after %s attempts.", retry_count)
            break

        interval_ms = int(config.STRIKE_SEARCH_RETRY_INTERVAL * 1000)
        logger.info(
            "Retrying entry search | Attempt %s | Interval %sms",
            retry_count + 1,
            interval_ms,
        )
        try:
            time.sleep(config.STRIKE_SEARCH_RETRY_INTERVAL)
        except KeyboardInterrupt:
            break

        nifty_spot = get_nifty_spot_price(broker) or nifty_spot
        strike_info = get_strike_prices(nifty_spot, fast_mode=config.FAST_ENTRY_MODE)
        atm_strike = strike_info["atm"]
        all_strikes = strike_info["all_strikes"]

        if config.FAST_ENTRY_MODE and config.USE_ASYNC_FETCHING:
            all_option_data, option_symbols, _ = asyncio.run(
                fetch_strikes_smart_async(
                    broker, expiry_date, atm_strike, nifty_spot, get_entry_strikes_fn
                )
            )
        elif config.FAST_ENTRY_MODE:
            all_option_data, option_symbols, _ = fetch_strikes_smart(
                broker, expiry_date, atm_strike, nifty_spot, get_entry_strikes_fn
            )
        else:
            all_option_data = []
            option_symbols = []
            for strike in all_strikes:
                ce_symbol = get_option_symbol(expiry_date, strike, "CE", broker)
                pe_symbol = get_option_symbol(expiry_date, strike, "PE", broker)
                ce_data = get_option_ohlcv_realtime(broker, ce_symbol)
                pe_data = get_option_ohlcv_realtime(broker, pe_symbol)
                if ce_data:
                    ce_data["symbol"] = ce_symbol
                    ce_data["strike"] = strike
                    ce_data["option_type"] = "CE"
                    all_option_data.append(ce_data)
                    option_symbols.append(ce_symbol)
                if pe_data:
                    pe_data["symbol"] = pe_symbol
                    pe_data["strike"] = strike
                    pe_data["option_type"] = "PE"
                    all_option_data.append(pe_data)
                    option_symbols.append(pe_symbol)
                _maybe_rate_limit_pause(broker)

        ltp_count = sum(1 for row in all_option_data if row.get("LTP") not in (None, 0))
        logger.info(
            "Retry fetch result | Option rows: %s | LTP > 0: %s",
            len(all_option_data),
            ltp_count,
        )
        if all_option_data:
            sample_rows = all_option_data[:6]
            logger.info("Retry sample option data (first %s rows):", len(sample_rows))
            for row in sample_rows:
                logger.info(
                    "Symbol: %s | Type: %s | Strike: %s | LTP: %s",
                    row.get("symbol") or row.get("Symbol"),
                    row.get("option_type"),
                    row.get("strike"),
                    row.get("LTP"),
                )
        entry_data = strategy.get_entry_strikes(all_option_data, nifty_spot, config)
        if entry_data is None:
            logger.info(
                "No valid entry pair (premium <= %s) after attempt %s.",
                config.MAX_TOTAL_PREMIUM,
                retry_count + 1,
            )
        retry_count += 1
    return entry_data


def _enter_and_monitor_trade(broker, algo_trade, entry_data, db_conn, all_option_data, nifty_spot, atm_strike):
    logger = logging.getLogger(__name__)
    if not entry_data:
        logger.info("No entry data found. Skipping trade entry/exit monitoring.")
        return

    call_strike, put_strike, call_price, put_price, call_symbol, put_symbol = entry_data
    total_premium = (call_price or 0) + (put_price or 0)
    logger.info(
        "Entry strikes selected | Call: %s @ %s (%s) | Put: %s @ %s (%s)",
        call_strike,
        call_price,
        call_symbol,
        put_strike,
        put_price,
        put_symbol,
    )
    logger.info("Total premium Given: %s", total_premium)
    algo_trade.entry_price = total_premium
    algo_trade.call_strike = call_strike
    algo_trade.put_strike = put_strike
    ORDER_QTY = config.ORDER_QTY
    algo_trade.enter_trade(broker, call_symbol, put_symbol, qty=ORDER_QTY)

    if db_conn and config.FAST_ENTRY_MODE and should_take_snapshot():
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        date_str = datetime.now().strftime("%Y-%m-%d")
        save_options_data_to_db(db_conn, all_option_data, timestamp_str, date_str)
        save_nifty_spot_to_db(db_conn, nifty_spot, atm_strike, timestamp_str, date_str)

    handler = None
    if config.USE_WEBSOCKET:
        handler = None

    logger.info("STEP 7: MONITORING EXIT CONDITION")
    monitor_exit_condition(
        broker,
        algo_trade,
        handler=handler,
        check_interval=config.API_POLLING_INTERVAL,
    )


def _maybe_rate_limit_pause(broker) -> None:
    name = broker.__class__.__name__.lower()
    if "angel" in name:
        time.sleep(0.1)


def main():
    logger = setup_logging(config.LOG_DIR, config.DRY_RUN)
    logger.info("Python executable: %s", sys.executable)
    db_conn = setup_database(config)

    try:
        run_mode = _parse_run_mode(sys.argv[1:])
        if run_mode is not None:
            object.__setattr__(config, "DRY_RUN", run_mode)
            logger.info("Run mode override: DRY_RUN=%s", config.DRY_RUN)

        config_error = _validate_required_config(config)
        if config_error:
            logger.error(config_error)
            return

        strategy = get_strategy(config.STRATEGY)

        broker = _initialize_broker(config, logger)
        if not broker:
            return

        algo_trade = AlgoTrade()

        logger.info("STEP 0: Checking for Existing Positions")
        if _handle_existing_positions(broker, algo_trade, logger):
            return

        logger.info("STEP 1: Downloading Nifty Spot Price")
        nifty_spot = _get_nifty_spot_with_market_check(broker, logger)
        if nifty_spot is None:
            return

        (
            all_option_data,
            option_symbols,
            atm_strike,
            all_strikes,
            expiry_date,
        ) = _fetch_option_data(broker, strategy, nifty_spot)

        if not config.FAST_ENTRY_MODE:
            _snapshot_non_fast(db_conn, all_option_data, nifty_spot, atm_strike)

        ctx = {
            "all_option_data": all_option_data,
            "nifty_spot": nifty_spot,
            "atm_strike": atm_strike,
            "config": config,
        }

        logger.info("STEP 5: Real-time Data Monitoring")
        logger.info("STEP 6: ALGO TRADING LOGIC")
        condition_met = strategy.should_enter(ctx)
        logger.info("Entry condition met: %s", condition_met)
        if algo_trade.trade_completed:
            condition_met = False

        entry_data = None
        if condition_met:
            entry_data = strategy.get_entry_strikes(all_option_data, nifty_spot, config)
            logger.info("Entry strikes raw: %s", entry_data)
            if entry_data is None:
                get_entry_strikes_fn = lambda d, s: strategy.get_entry_strikes(d, s, config)
                entry_data = _retry_entry_search(
                    broker,
                    strategy,
                    nifty_spot,
                    expiry_date,
                    get_entry_strikes_fn,
                )
                logger.info("Entry strikes after retry: %s", entry_data)
        else:
            logger.info("Entry condition not met. Trade entry skipped.")

        _enter_and_monitor_trade(
            broker,
            algo_trade,
            entry_data,
            db_conn,
            all_option_data,
            nifty_spot,
            atm_strike,
        )

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.exception("Unhandled error in main: %s", e)
    finally:
        if db_conn:
            date_str = datetime.now().strftime("%Y-%m-%d")
            log_end_of_day_summary(db_conn, date_str)
            db_conn.close()

