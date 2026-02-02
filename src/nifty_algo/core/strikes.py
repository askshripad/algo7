from __future__ import annotations
from typing import Any
from nifty_algo.config import config

def round_to_nearest_50(price: float) -> float:
    return round(price / 50) * 50

def get_strike_prices(spot:float,fast_mode:bool = False) -> dict[str,list[float]]:
    interval = config.STRIKE_INTERVAL
    atm = int(round(spot/interval)) * interval

    max_steps = config.FAST_ENTRY_MAX_STRIKES if fast_mode else config.NUM_STRIKES_ABOVE_BELOW
    strikes_above = [atm + interval * i for i in range(1, max_steps + 1)]
    strikes_below = [atm - interval * i for i in range(1, max_steps + 1)]
    all_strikes = [atm] + strikes_above + strikes_below

    return {
        "atm": atm,
        "strikes_above": strikes_above,
        "strikes_below": strikes_below,
        "all_strikes": all_strikes
    }

def get_option_symbol(expiry_date, strike:int, option_type:str, broker) -> str:
    return broker.get_option_symbol(expiry_date, strike, option_type)

def _extract_ltp(quote:dict) -> float|None:
    for key in ("LTP","ltp","last_price","lastPrice"):
        if key in quote and quote[key] is not None:
            try:
                return float(quote[key])
            except (ValueError, TypeError):
                continue
    return None

def _extract_strike(row: dict) -> int | None:
    for key in ("strike", "strike_price", "strikePrice"):
        if key in row and row[key] is not None:
            try:
                return int(float(row[key]))
            except (TypeError, ValueError):
                continue
    return None

def _extract_symbol(row:dict) -> str | None:
    for key in ("Symbol", "symbol", "tradingsymbol", "tradingsymbol"):
        if key in row and row[key] is not None:
            return str(row[key]).upper()
    return None

def _extract_option_type(row:dict) -> str | None:
    for key in ("Option_Type", "option_type", "optionType"):
        if key in row and row[key] is not None:
            return str(row[key]).upper()
    return None

def find_entry_strikes(
    all_option_data: list[dict[str, Any]],
    nifty_spot: float,
    max_total_premium: float,
    min_premium: float | None = None,
    max_premium: float | None = None,
):
    call_candidates = []
    put_candidates = []

    for row in all_option_data:
        opt_type = _extract_option_type(row)
        strike = _extract_strike(row)
        ltp = _extract_ltp(row)
        symbol = _extract_symbol(row)

        if opt_type is None or strike is None or ltp is None or symbol is None:
            continue

        # Apply per-leg filter only if both min and max provided
        if min_premium is not None and max_premium is not None:
            if not (min_premium <= ltp <= max_premium):
                continue

        item = {
            "strike": strike,
            "ltp": ltp,
            "symbol": symbol,
        }

        if opt_type == "CE":
            call_candidates.append(item)
        elif opt_type == "PE":
            put_candidates.append(item)

    best = None
    best_distance = None

    for call in call_candidates:
        for put in put_candidates:
            total = call["ltp"] + put["ltp"]
            if total > max_total_premium:
                continue

            distance = abs(call["strike"] - nifty_spot) + abs(put["strike"] - nifty_spot)

            if best is None or distance < best_distance:
                best = (call, put)
                best_distance = distance

    if not best:
        return None

    call, put = best
    return (
        call["strike"],
        put["strike"],
        call["ltp"],
        put["ltp"],
        call["symbol"],
        put["symbol"],
    )



