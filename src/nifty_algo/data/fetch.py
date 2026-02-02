from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from nifty_algo.config import config
from nifty_algo.core.strikes import get_option_symbol

try:
    from nifty_algo.brokers.fyers import FyersBroker
except Exception:
    FyersBroker = None

try:
    from nifty_algo.brokers.angel import AngelBroker
except Exception:
    AngelBroker = None

NIFTY_SYMBOL = getattr(config, "NIFTY_SYMBOL", "NSE:NIFTY50-INDEX")

def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")

def get_option_ohlcv_realtime(broker, symbol: str) -> dict[str, Any] | None:
    raw = broker.get_option_quote(symbol)
    if not raw:
        return None

    data: dict[str, Any] = {
        "Symbol": symbol,
        "Open": None,
        "High": None,
        "Low": None,
        "Close": None,
        "LTP": None,
        "Volume": None,
        "Prev_Close": None,
        "Delta": None,
        "Gamma": None,
        "Theta": None,
        "Vega": None,
        "IV": None,
        "Timestamp": _now_ts(),
    }

    if FyersBroker is not None and isinstance(broker, FyersBroker):
        data["Open"] = raw.get("open_price")
        data["High"] = raw.get("high_price")
        data["Low"] = raw.get("low_price")
        data["Close"] = raw.get("lp")  # Fyers uses lp for last price
        data["LTP"] = raw.get("lp")
        data["Volume"] = raw.get("volume")
        data["Prev_Close"] = raw.get("prev_close_price")

        # Greeks (if present)
        data["Delta"] = raw.get("delta")
        data["Gamma"] = raw.get("gamma")
        data["Theta"] = raw.get("theta")
        data["Vega"] = raw.get("vega")
        data["IV"] = raw.get("iv")

    elif AngelBroker is not None and isinstance(broker, AngelBroker):
        data["Open"] = raw.get("open")
        data["High"] = raw.get("high")
        data["Low"] = raw.get("low")
        data["Close"] = raw.get("close")
        data["LTP"] = raw.get("ltp")
        data["Volume"] = raw.get("volume")
        data["Prev_Close"] = raw.get("previousClose")
        # Greeks not provided; keep None

    else:
        # Generic fallback: try common keys
        data["Open"] = raw.get("open") or raw.get("open_price")
        data["High"] = raw.get("high") or raw.get("high_price")
        data["Low"] = raw.get("low") or raw.get("low_price")
        data["Close"] = raw.get("close") or raw.get("lp")
        data["LTP"] = raw.get("ltp") or raw.get("lp")
        data["Volume"] = raw.get("volume")
        data["Prev_Close"] = raw.get("previousClose") or raw.get("prev_close_price")

    return data


async def get_option_ohlcv_realtime_async(broker, symbol: str) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(get_option_ohlcv_realtime, broker, symbol)
    except AttributeError:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, get_option_ohlcv_realtime, broker, symbol)


def get_nifty_spot_price(broker) -> float | None:
    return broker.get_spot_price(NIFTY_SYMBOL)

def fetch_strikes_smart(
    broker,
    expiry,
    atm_strike: int,
    nifty_spot: float,
    get_entry_strikes_fn,
):
    all_option_data: list[dict[str, Any]] = []
    option_symbols: list[str] = []
    seen_symbols: set[str] = set()

    max_levels = config.NUM_STRIKES_ABOVE_BELOW
    interval = config.STRIKE_INTERVAL

    def _fetch_symbol(sym: str, strike: int, opt_type: str):
        if sym in seen_symbols:
            return
        seen_symbols.add(sym)
        option_symbols.append(sym)

        data = get_option_ohlcv_realtime(broker, sym)
        if data is None:
            return
        data["option_type"] = opt_type
        data["strike"] = strike
        data["symbol"] = sym
        all_option_data.append(data)

    for level in range(0, max_levels + 1):
        strikes = [atm_strike] if level == 0 else [
            atm_strike + interval * level,
            atm_strike - interval * level,
        ]

        for strike in strikes:
            ce_symbol = get_option_symbol(expiry, strike, "CE", broker)
            pe_symbol = get_option_symbol(expiry, strike, "PE", broker)
            _fetch_symbol(ce_symbol, strike, "CE")
            _fetch_symbol(pe_symbol, strike, "PE")

        found = get_entry_strikes_fn(all_option_data, nifty_spot)
        if found is not None:
            return all_option_data, option_symbols, True

    return all_option_data, option_symbols, False


async def fetch_strikes_smart_async(
    broker,
    expiry,
    atm_strike: int,
    nifty_spot: float,
    get_entry_strikes_fn,
):
    all_option_data: list[dict[str, Any]] = []
    option_symbols: list[str] = []
    seen_symbols: set[str] = set()

    max_levels = config.NUM_STRIKES_ABOVE_BELOW
    interval = config.STRIKE_INTERVAL
    semaphore = asyncio.Semaphore(config.ASYNC_CONCURRENT_LIMIT)

    async def _bounded_fetch(sym: str, strike: int, opt_type: str):
        if sym in seen_symbols:
            return
        seen_symbols.add(sym)
        option_symbols.append(sym)

        async with semaphore:
            data = await get_option_ohlcv_realtime_async(broker, sym)

        if data is None:
            return
        data["option_type"] = opt_type
        data["strike"] = strike
        data["symbol"] = sym
        all_option_data.append(data)

    for level in range(0, max_levels + 1):
        strikes = [atm_strike] if level == 0 else [
            atm_strike + interval * level,
            atm_strike - interval * level,
        ]

        tasks = []
        for strike in strikes:
            ce_symbol = get_option_symbol(expiry, strike, "CE", broker)
            pe_symbol = get_option_symbol(expiry, strike, "PE", broker)
            tasks.append(_bounded_fetch(ce_symbol, strike, "CE"))
            tasks.append(_bounded_fetch(pe_symbol, strike, "PE"))

        if tasks:
            await asyncio.gather(*tasks)

        found = get_entry_strikes_fn(all_option_data, nifty_spot)
        if found is not None:
            return all_option_data, option_symbols, True

    return all_option_data, option_symbols, False