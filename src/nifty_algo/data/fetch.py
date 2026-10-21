"""
Option strike data fetching.

What we fetch (combined, deduped):
  1. Algo strikes: ATM and ATM ± (STRIKE_INTERVAL * level) for level 1..NUM_STRIKES_ABOVE_BELOW
     (or FAST_ENTRY_MAX_STRIKES in fast mode). Used for entry logic (e.g. premium ~112).
  2. Chart strangle: same as backtest — CE = ATM + STRANGLE_ATM_OFFSET_POINTS,
     PE = ATM − STRANGLE_ATM_OFFSET_POINTS (ATM = nearest STRIKE_INTERVAL to spot).
  3. Tracker strikes: if algo already set CE/PE, we add those so the chart has prices.

Broker limits:
  - Angel: 1 symbol per request (marketDataFull/ltpData); rate limit applies. Batch quote
    exists (up to 50 symbols) but uses token API; current code uses sequential + delay.
  - Fyers: up to 50 symbols per request (quotes with comma-separated symbols). We batch
    when broker is Fyers to reduce API calls.

Adjust offset via STRANGLE_ATM_OFFSET_POINTS in .env (same key as backtest).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from nifty_algo.config import config

logger = logging.getLogger(__name__)
from nifty_algo.core.strikes import get_option_symbol
from nifty_algo.cache.market_cache import get_cache

try:
    from nifty_algo.brokers.fyers import FyersBroker
except Exception:
    FyersBroker = None

try:
    from nifty_algo.brokers.angel import AngelBroker
except Exception:
    AngelBroker = None

from nifty_algo.brokers.fyers_symbol_format import build_fyers_option_symbol

NIFTY_SYMBOL = getattr(config, "NIFTY_SYMBOL", "NSE:NIFTY50-INDEX")


def _chart_strangle_strike_pair(atm_strike: int) -> tuple[int, int]:
    """CE/PE strikes for chart — matches backtest (ATM ± STRANGLE_ATM_OFFSET_POINTS)."""
    off = int(getattr(config, "STRANGLE_ATM_OFFSET_POINTS", 100))
    return atm_strike + off, atm_strike - off

# Lazy Fyers broker instance (created only if needed)
_fyers_fallback_broker: FyersBroker | None = None
def _get_fyers_fallback_broker() -> FyersBroker:
    """
    Return a shared Fyersbroker isntace for fallback use.
     - Creates and initializes it the first time it's needed.
    - Returns None if Fyers is not available or config is incomplete/invalid.
    """
    global _fyers_fallback_broker
    if FyersBroker is None:
        return None
    # if we already created and initalized one,reuse it
    if _fyers_fallback_broker is not None:
        return _fyers_fallback_broker
    from nifty_algo.config import config # local import to avoid cycles
    client_id = getattr(config, "FYERS_CLIENT_ID", "")
    access_token = getattr(config, "FYERS_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        logger.warning("Fyers fallback broker requested but config is incomplete/invalid. Returning None.")
        return None
    
    try:
        fb = FyersBroker(client_id=client_id, access_token=access_token, log_dir=config.LOG_DIR)
        fb.initialize()
    except Exception as e:
        logger.error("Failed to initialize Fyers fallback broker: %s", e)
        return None

    _fyers_fallback_broker = fb
    logger.info("Initialized Fyers fallback broker")
    return _fyers_fallback_broker


# Track last known spot price for cache updates
_last_spot_price: float = 0.0

def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _build_fyers_symbol_from_expiry(expiry,strike:int,opt_type:str) -> str:
    """Same rules as ``FyersBroker.get_option_symbol`` (compact vs letter-month by config)."""
    return build_fyers_option_symbol(expiry, strike, opt_type)




def _extract_strike_and_type(symbol: str) -> tuple[int | None, str | None]:
    """Extract strike price and option type (CE/PE) from symbol.
    Fyers patterns first (avoid Angel regex matching Fyers ``26MAR23200CE`` as DDMMMYY).
    """
    import re
    # Fyers YYMMM + DD + strike (legacy: e.g. 26MAR3023250 before mar-without-day)
    match = re.search(r'NSE:NIFTY(\d{2})([A-Z]{3})(\d{2})(\d{4,})(CE|PE)$', symbol)
    if match:
        return int(match.group(4)), match.group(5)

    # Fyers monthly/mar: YYMMM + strike only (e.g. NSE:NIFTY26MAR23200CE — no day in ticker)
    match = re.search(r'NSE:NIFTY(\d{2})([A-Z]{3})(\d{4,})(CE|PE)$', symbol)
    if match:
        return int(match.group(3)), match.group(4)

    # Fyers YY + month + DD + strike (month: unpadded 3–9 or padded 01–09 or 10–11–12)
    match = re.search(
        r'NSE:NIFTY(\d{2})(?:0[1-9]|1[0-2]|[1-9])(\d{2})(\d+)(CE|PE)$',
        symbol,
    )
    if match:
        return int(match.group(3)), match.group(4)

    # Angel: DDMMMYY + strike (after Fyers so 26MAR… is not parsed as Angel)
    match = re.search(r'\d{2}[A-Z]{3}\d{2}(\d+)(CE|PE)$', symbol)
    if match:
        return int(match.group(1)), match.group(2)

    # Legacy: type before strike at end
    match = re.search(r'(CE|PE)(\d+)$', symbol)
    if match:
        return int(match.group(2)), match.group(1)

    return None, None

def get_option_ohlcv_realtime(broker, symbol: str, publish_to_cache: bool = True) -> dict[str, Any] | None:
    global _last_spot_price
    
    # Try primary broker first
    raw = broker.get_option_quote(symbol)    
    
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
        # Angel might send volume as "volume" or "tradeVolume" depending on API variant
        data["Volume"] = raw.get("volume") or raw.get("tradeVolume")
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

    # Auto-publish to cache for consumers (API, WebSocket, DB)
    if publish_to_cache and data.get("LTP"):
        strike, opt_type = _extract_strike_and_type(symbol)
        if strike and opt_type:
            data["strike"] = strike
            data["option_type"] = opt_type
            data["symbol"] = symbol
            
            # Use strike as approximate spot if we don't have it
            spot = _last_spot_price if _last_spot_price > 0 else float(strike)
            logger.debug("Auto-publishing to cache: %s strike=%d LTP=%s spot=%.2f", 
                        symbol, strike, data.get("LTP"), spot)
            get_cache().publish_options([data], spot, strike)
        else:
            logger.debug("Failed to extract strike/type from symbol: %s", symbol)

    return data


async def get_option_ohlcv_realtime_async(
    broker, symbol: str, publish_to_cache: bool = True
) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(
            get_option_ohlcv_realtime, broker, symbol, publish_to_cache
        )
    except AttributeError:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: get_option_ohlcv_realtime(broker, symbol, publish_to_cache),
        )


def get_nifty_spot_price(broker) -> float | None:
    global _last_spot_price
    spot = broker.get_spot_price(NIFTY_SYMBOL)
    if spot:
        _last_spot_price = spot
    return spot

def set_last_spot_price(spot: float) -> None:
    """Update the cached spot price (call after using fallback)"""
    global _last_spot_price
    _last_spot_price = spot


def publish_chart_data(
    broker,
    nifty_spot: float,
    atm_strike: int | None = None,
    *,
    include_volume_band: bool = True,
) -> list[dict[str, Any]]:
    """
    Lightweight publish for chart UI — fetches chart strangle + optional volume band, pushes to cache.
    Use at market open and during exit monitoring so /chart and /niftychart keep updating.
    """
    from nifty_algo.core.strikes import round_to_nearest_50
    from nifty_algo.market.expiry import get_nearest_expiry

    if not nifty_spot or nifty_spot <= 0:
        return []

    atm = atm_strike if atm_strike else round_to_nearest_50(nifty_spot)
    expiry = get_nearest_expiry(broker=broker)
    if expiry is None:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _one(sym: str, strike: int, opt_type: str) -> None:
        if sym in seen:
            return
        seen.add(sym)
        data = get_option_ohlcv_realtime(broker, sym, publish_to_cache=False)
        if not data:
            return
        data["option_type"] = opt_type
        data["strike"] = strike
        data["symbol"] = sym
        rows.append(data)

    chart_ce, chart_pe = _chart_strangle_strike_pair(atm)
    _one(get_option_symbol(expiry, chart_ce, "CE", broker), chart_ce, "CE")
    _one(get_option_symbol(expiry, chart_pe, "PE", broker), chart_pe, "PE")

    if include_volume_band:
        interval = int(getattr(config, "STRIKE_INTERVAL", 50))
        levels = int(getattr(config, "NUM_STRIKES_ABOVE_BELOW", 5))
        band_strikes = {atm}
        for level in range(1, levels + 1):
            band_strikes.add(atm + interval * level)
            band_strikes.add(atm - interval * level)
        for strike in sorted(band_strikes):
            for opt_type in ("CE", "PE"):
                sym = get_option_symbol(expiry, strike, opt_type, broker)
                _one(sym, strike, opt_type)

    if rows:
        get_cache().publish_options(rows, nifty_spot, atm)
    return rows


def fetch_strikes_smart(
    broker,
    expiry,
    atm_strike: int,
    nifty_spot: float,
    get_entry_strikes_fn,
    *,
    spot_for_cache: float | None = None,
):
    """nifty_spot: used for entry-strike selection. spot_for_cache: live Nifty for chart/cache (defaults to nifty_spot)."""
    cache_spot = nifty_spot if spot_for_cache is None else spot_for_cache
    all_option_data: list[dict[str, Any]] = []
    option_symbols: list[str] = []
    seen_symbols: set[str] = set()

    max_levels = config.NUM_STRIKES_ABOVE_BELOW
    interval = config.STRIKE_INTERVAL

    def _fetch_symbol(sym: str, strike: int, opt_type: str):
        logger.info(f"Fetching symbol: {sym}, {strike}, {opt_type}")
        if sym in seen_symbols:
            return
        seen_symbols.add(sym)
        option_symbols.append(sym)

        # 1. Try primary broker first
        data = get_option_ohlcv_realtime(broker, sym, publish_to_cache=False)
        if data is None:
            return
        data["option_type"] = opt_type
        data["strike"] = strike
        data["symbol"] = sym

        # 2. If primary broker fails, try fallback
        try:
            vol = data.get("Volume") or data.get("volume")
            needs_volume_fallback = vol is None or (isinstance(vol, (int, float)) and vol <= 0)
        except Exception:
            needs_volume_fallback = True
        
        logger.info("Volume before fallback for %s: %s", sym, vol)
        logger.info("Needs volume fallback for %s: %s", sym, needs_volume_fallback)

        if needs_volume_fallback:
            fb = _get_fyers_fallback_broker()
            if fb is not None:
                try:
                    fyer_symbol = _build_fyers_symbol_from_expiry(expiry, strike, opt_type)
                    logger.info("Fyers fallback symbol: %s", fyer_symbol)
                    raw_fyers = fb.get_option_quote(fyer_symbol)
                    logger.info(f"_fetch_symbol:Fyers fallback broker returned data: {raw_fyers}")
                except Exception as e:
                    logger.error("Failed to get option quote from Fyers fallback broker: %s", e)
                    raw_fyers  = None
                
                if raw_fyers :
                    fallback_vol = raw_fyers.get("volume")
                    logger.info("Volume fallback: Fyers sym=%s", fyer_symbol)
                    if fallback_vol is not None and fallback_vol > 0:
                        data["Volume"] = fallback_vol
                 # patch missing LTP from Fyers if Angel LTP is missing/zero        
                ltp = data.get("LTP")
                if (ltp is None or (isinstance(ltp, (int, float)) and ltp <= 0)
                        and raw_fyers.get("lp") is not None):
                    data["LTP"] = raw_fyers.get("lp")        


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
            # Also fetch chart strangle strikes and tracker strikes so chart has data
            _fetch_chart_strangle_strikes(broker, expiry, atm_strike, _fetch_symbol)
            _fetch_tracker_strikes_if_set(broker, expiry, all_option_data, _fetch_symbol)
            get_cache().publish_options(all_option_data, cache_spot, atm_strike)
            return all_option_data, option_symbols, True

    # Ensure chart strangle strikes and algo tracker strikes are always fetched
    _fetch_chart_strangle_strikes(broker, expiry, atm_strike, _fetch_symbol)
    _fetch_tracker_strikes_if_set(broker, expiry, all_option_data, _fetch_symbol)
    get_cache().publish_options(all_option_data, cache_spot, atm_strike)
    return all_option_data, option_symbols, False


def _build_all_symbols_to_fetch(broker, expiry, atm_strike: int) -> list[tuple[str, int, str]]:
    """
    Build the full list of (symbol, strike, opt_type) we need: algo levels + chart strangle + tracker.
    Deduped by symbol. Use this for batch fetch (e.g. Fyers 50/call) or to know total request count.
    """
    interval = getattr(config, "STRIKE_INTERVAL", 50)
    max_levels = config.NUM_STRIKES_ABOVE_BELOW
    seen: set[str] = set()
    out: list[tuple[str, int, str]] = []

    # Algo strikes: ATM and levels
    for level in range(0, max_levels + 1):
        strikes = [atm_strike] if level == 0 else [
            atm_strike + interval * level,
            atm_strike - interval * level,
        ]
        for strike in strikes:
            for opt_type in ("CE", "PE"):
                sym = get_option_symbol(expiry, strike, opt_type, broker)
                if sym not in seen:
                    seen.add(sym)
                    out.append((sym, strike, opt_type))

    # Chart strangle (same formula as backtest)
    chart_ce, chart_pe = _chart_strangle_strike_pair(atm_strike)
    for strike, opt_type in [(chart_ce, "CE"), (chart_pe, "PE")]:
        sym = get_option_symbol(expiry, strike, opt_type, broker)
        if sym not in seen:
            seen.add(sym)
            out.append((sym, strike, opt_type))

    # Tracker strikes if set (algo-selected CE/PE for chart)
    try:
        from nifty_algo.core.strangle_tracker import get_strangle_tracker
        cur = get_strangle_tracker().get_current()
        if cur and cur.get("ce_strike") is not None and cur.get("pe_strike") is not None:
            ce_s, pe_s = int(cur["ce_strike"]), int(cur["pe_strike"])
            for strike, opt_type in [(ce_s, "CE"), (pe_s, "PE")]:
                sym = cur.get("ce_symbol" if opt_type == "CE" else "pe_symbol") or get_option_symbol(expiry, strike, opt_type, broker)
                if sym not in seen:
                    seen.add(sym)
                    out.append((sym, strike, opt_type))
    except Exception:
        pass

    return out


def _raw_quote_to_option_row(raw: dict | None, symbol: str, strike: int, opt_type: str, broker) -> dict[str, Any] | None:
    """Convert broker raw quote to normalized option row (Fyers/Angel key names). Returns None if raw is None/empty."""
    if not raw:
        return None
    data: dict[str, Any] = {
        "Symbol": symbol,
        "Open": raw.get("open") or raw.get("open_price"),
        "High": raw.get("high") or raw.get("high_price"),
        "Low": raw.get("low") or raw.get("low_price"),
        "Close": raw.get("close") or raw.get("lp"),
        "LTP": raw.get("lp")
        or raw.get("ltp")
        or raw.get("LTP")
        or raw.get("last_price")
        or raw.get("last_traded_price"),
        "Volume": raw.get("volume") or raw.get("Volume") or raw.get("tradeVolume"),
        "Prev_Close": raw.get("previousClose") or raw.get("prev_close_price"),
        "option_type": opt_type,
        "strike": strike,
        "symbol": symbol,
    }
    if data.get("LTP") is None and data.get("Close") is None:
        return None
    return data


def _fetch_chart_strangle_strikes(broker, expiry, atm_strike: int, fetch_fn):
    """
    For chart display only: strangle pair at ATM ± STRANGLE_ATM_OFFSET_POINTS (same as backtest).
    """
    chart_ce_strike, chart_pe_strike = _chart_strangle_strike_pair(atm_strike)
    # Fetch only the two legs of the strangle (CE above, PE below); fetch_fn skips if already in set
    ce_symbol = get_option_symbol(expiry, chart_ce_strike, "CE", broker)
    pe_symbol = get_option_symbol(expiry, chart_pe_strike, "PE", broker)
    fetch_fn(ce_symbol, chart_ce_strike, "CE")
    fetch_fn(pe_symbol, chart_pe_strike, "PE")


def _fetch_tracker_strikes_if_set(broker, expiry, all_option_data: list, fetch_fn):
    """
    If the strangle tracker has algo-selected strikes (CE/PE), ensure we have
    option data for them so the chart gets prices and history updates.
    """
    try:
        from nifty_algo.core.strangle_tracker import get_strangle_tracker
        tracker = get_strangle_tracker()
        cur = tracker.get_current()
        if cur is None:
            return
        ce_s, pe_s = cur.get("ce_strike"), cur.get("pe_strike")
        if ce_s is None or pe_s is None:
            return
        has_ce = any(
            (o.get("strike") == ce_s or o.get("strike") == int(ce_s))
            and (o.get("option_type") or "").upper() == "CE"
            for o in all_option_data
        )
        has_pe = any(
            (o.get("strike") == pe_s or o.get("strike") == int(pe_s))
            and (o.get("option_type") or "").upper() == "PE"
            for o in all_option_data
        )
        if has_ce and has_pe:
            return
        if not has_ce:
            ce_sym = cur.get("ce_symbol") or get_option_symbol(expiry, int(ce_s), "CE", broker)
            fetch_fn(ce_sym, int(ce_s), "CE")
        if not has_pe:
            pe_sym = cur.get("pe_symbol") or get_option_symbol(expiry, int(pe_s), "PE", broker)
            fetch_fn(pe_sym, int(pe_s), "PE")
    except Exception as e:
        logger.debug("Fetch tracker strikes: %s", e)


async def fetch_strikes_smart_async(
    broker,
    expiry,
    atm_strike: int,
    nifty_spot: float,
    get_entry_strikes_fn,
    *,
    spot_for_cache: float | None = None,
):
    cache_spot = nifty_spot if spot_for_cache is None else spot_for_cache
    # Prefer batch fetch when broker supports it (e.g. Fyers up to 50 symbols/request)
    if FyersBroker is not None and isinstance(broker, FyersBroker):
        symbol_tuples = _build_all_symbols_to_fetch(broker, expiry, atm_strike)
        if symbol_tuples:
            symbols = [t[0] for t in symbol_tuples]
            raw_list = await asyncio.to_thread(
                broker.get_option_quotes_batch, symbols, chunk_size=50
            )
            all_option_data = []
            for (sym, strike, opt_type), raw in zip(symbol_tuples, raw_list):
                row = _raw_quote_to_option_row(raw, sym, strike, opt_type, broker)
                if row:
                    all_option_data.append(row)
            if not all_option_data and symbol_tuples:
                logger.warning(
                    "Fyers batch returned no usable LTP for %d symbols (first=%s). "
                    "Check FYERS_OPTION_SYMBOL_STYLE (unpadded=YY+M+DD e.g. 26330, padded=260330, monthly=26MAR...).",
                    len(symbol_tuples),
                    symbol_tuples[0][0],
                )
            option_symbols = symbols
            get_cache().publish_options(all_option_data, cache_spot, atm_strike)
            return all_option_data, option_symbols, get_entry_strikes_fn(all_option_data, nifty_spot) is not None

    # Fallback: per-symbol fetch (Angel or other brokers)
    all_option_data = []
    option_symbols = []
    seen_symbols = set()
    max_levels = config.NUM_STRIKES_ABOVE_BELOW
    interval = config.STRIKE_INTERVAL
    semaphore = asyncio.Semaphore(config.ASYNC_CONCURRENT_LIMIT)

    async def _bounded_fetch(sym: str, strike: int, opt_type: str):
        if sym in seen_symbols:
            return
        seen_symbols.add(sym)
        option_symbols.append(sym)

        async with semaphore:
            data = await get_option_ohlcv_realtime_async(
                broker, sym, publish_to_cache=False
            )

        if data is None:
            return

        data["option_type"] = opt_type
        data["strike"] = strike
        data["symbol"] = sym

        try:
            vol = data.get("Volume") or data.get("volume")
            needs_volume_fallback = vol is None or (isinstance(vol, (int, float)) and vol <= 0)
        except Exception:
            needs_volume_fallback = True

        if needs_volume_fallback:
            fb = _get_fyers_fallback_broker()
            if fb is not None:
                try:
                    fy_sym = _build_fyers_symbol_from_expiry(expiry, strike, opt_type)
                    raw_fyers = fb.get_option_quote(fy_sym)
                except Exception as exc:
                    logger.warning("Async Fyers fallback quote failed for %s: %s", fy_sym, exc)
                    raw_fyers = None

                if raw_fyers:
                    fy_vol = raw_fyers.get("volume")
                    if fy_vol is not None and isinstance(fy_vol, (int, float)) and fy_vol > 0:
                        data["Volume"] = fy_vol
                    ltp = data.get("LTP")
                    fy_ltp = raw_fyers.get("lp")
                    if (ltp is None or (isinstance(ltp, (int, float)) and ltp <= 0)) and fy_ltp:
                        data["LTP"] = fy_ltp

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
            await _fetch_chart_strangle_strikes_async(broker, expiry, atm_strike, _bounded_fetch)
            await _fetch_tracker_strikes_if_set_async(broker, expiry, all_option_data, _bounded_fetch)
            get_cache().publish_options(all_option_data, cache_spot, atm_strike)
            return all_option_data, option_symbols, True

    await _fetch_chart_strangle_strikes_async(broker, expiry, atm_strike, _bounded_fetch)
    await _fetch_tracker_strikes_if_set_async(broker, expiry, all_option_data, _bounded_fetch)
    get_cache().publish_options(all_option_data, cache_spot, atm_strike)
    return all_option_data, option_symbols, False


async def _fetch_chart_strangle_strikes_async(broker, expiry, atm_strike: int, fetch_fn):
    """Chart display only: ATM ± STRANGLE_ATM_OFFSET_POINTS (same as backtest)."""
    chart_ce_strike, chart_pe_strike = _chart_strangle_strike_pair(atm_strike)
    ce_symbol = get_option_symbol(expiry, chart_ce_strike, "CE", broker)
    pe_symbol = get_option_symbol(expiry, chart_pe_strike, "PE", broker)
    await asyncio.gather(
        fetch_fn(ce_symbol, chart_ce_strike, "CE"),
        fetch_fn(pe_symbol, chart_pe_strike, "PE"),
    )


async def _fetch_tracker_strikes_if_set_async(broker, expiry, all_option_data: list, fetch_fn):
    """Async: ensure algo-selected strangle strikes are in data for chart."""
    try:
        from nifty_algo.core.strangle_tracker import get_strangle_tracker
        tracker = get_strangle_tracker()
        cur = tracker.get_current()
        if cur is None:
            return
        ce_s, pe_s = cur.get("ce_strike"), cur.get("pe_strike")
        if ce_s is None or pe_s is None:
            return
        has_ce = any(
            (o.get("strike") == ce_s or o.get("strike") == int(ce_s))
            and (o.get("option_type") or "").upper() == "CE"
            for o in all_option_data
        )
        has_pe = any(
            (o.get("strike") == pe_s or o.get("strike") == int(pe_s))
            and (o.get("option_type") or "").upper() == "PE"
            for o in all_option_data
        )
        if has_ce and has_pe:
            return
        tasks = []
        if not has_ce:
            ce_sym = cur.get("ce_symbol") or get_option_symbol(expiry, int(ce_s), "CE", broker)
            tasks.append(fetch_fn(ce_sym, int(ce_s), "CE"))
        if not has_pe:
            pe_sym = cur.get("pe_symbol") or get_option_symbol(expiry, int(pe_s), "PE", broker)
            tasks.append(fetch_fn(pe_sym, int(pe_s), "PE"))
        if tasks:
            await asyncio.gather(*tasks)
    except Exception as e:
        logger.debug("Fetch tracker strikes (async): %s", e)