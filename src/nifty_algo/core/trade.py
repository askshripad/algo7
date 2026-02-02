from __future__ import annotations

import time
from dataclasses import dataclass
import logging

from nifty_algo.config import config

logger = logging.getLogger(__name__)

def _strike_from_symbol(sym: str | None) -> int | None:
    if not sym:
        return None
    import re
    m = re.search(r"(\d+)(CE|PE)$", sym)
    return int(m.group(1)) if m else None

def _extract_ltp(data: dict | None) -> float | None:
    if not data:
        return None
    for key in ("ltp", "LTP", "last_price", "lastPrice", "close", "lp"):
        if key in data and data[key] is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return None

def get_option_ohlcv_realtime(broker, symbol: str) -> dict | None:
    # Uses broker quote method as a generic OHLCV source
    return broker.get_option_quote(symbol)

@dataclass
class AlgoTrade:
    entry_price: float | None = None
    entry_time: str | None = None
    is_position_open: bool = False
    call_symbol: str | None = None
    put_symbol: str | None = None
    call_strike: int | None = None
    put_strike: int | None = None
    trade_completed: bool = False

    call_order_id: str | None = None
    put_order_id: str | None = None

    call_entry_price: float | None = None
    put_entry_price: float | None = None

    def restore_from_positions(
        self,
        call_symbol: str,
        put_symbol: str,
        call_price: float,
        put_price: float,
    ) -> None:
        self.call_symbol = call_symbol
        self.put_symbol = put_symbol
        self.call_entry_price = call_price
        self.put_entry_price = put_price
        self.entry_price = (call_price or 0) + (put_price or 0)
        self.call_strike = _strike_from_symbol(call_symbol)
        self.put_strike = _strike_from_symbol(put_symbol)
        self.is_position_open = True
        self.trade_completed = False

    def _place_order(self, broker, symbol: str, side: str, qty: int, order_type: str = "MARKET", price: float = 0.0):
        if config.DRY_RUN:
            return "DRY_RUN"
        return broker.place_order(symbol, side, qty, order_type=order_type, price=price)

    def enter_trade(
        self,
        broker,
        call_symbol: str,
        put_symbol: str,
        qty: int,
    ) -> bool:
        self.call_symbol = call_symbol
        self.put_symbol = put_symbol

        if config.DRY_RUN:
            self.call_order_id = "DRY_RUN"
            self.put_order_id = "DRY_RUN"
            self.is_position_open = True
            return True

        call_resp = self._place_order(
            broker,
            call_symbol,
            "BUY",
            qty,
            order_type="MARKET",
            price=0.0,
        )
        put_resp = self._place_order(
            broker,
            put_symbol,
            "BUY",
            qty,
            order_type="MARKET",
            price=0.0,
        )

        # Store order IDs if available
        self.call_order_id = str(call_resp) if call_resp is not None else None
        self.put_order_id = str(put_resp) if put_resp is not None else None

        # Use post-order LTP as entry reference (fill price may not be available)
        # call_ltp = _extract_ltp(get_option_ohlcv_realtime(broker, call_symbol))
        # put_ltp = _extract_ltp(get_option_ohlcv_realtime(broker, put_symbol))
        # Use actual average fill price from orderBook
        call_avg = _get_avg_fill_price_from_orderbook(broker, self.call_order_id)
        put_avg = _get_avg_fill_price_from_orderbook(broker, self.put_order_id)

        if call_avg is not None and put_avg is not None:
            self.entry_price = call_avg + put_avg
        else:
            # Fallback if not found
            call_ltp = _extract_ltp(get_option_ohlcv_realtime(broker, call_symbol))
            put_ltp = _extract_ltp(get_option_ohlcv_realtime(broker, put_symbol))
            if call_ltp is not None and put_ltp is not None:
                self.entry_price = call_ltp + put_ltp

        self.is_position_open = True
        return True

    def check_exit(self, call_ltp: float, put_ltp: float) -> bool:
        if self.entry_price is None:
            return False

        current_total = (call_ltp or 0) + (put_ltp or 0)
        target_total = self.entry_price + config.TARGET_PROFIT_POINTS
        return current_total >= target_total

    def exit_trade(self, broker, qty: int) -> bool:
        if not self.is_position_open:
            return False

        if config.DRY_RUN:
            self.is_position_open = False
            self.trade_completed = True
            return True

        # Buy back both legs
        broker.place_order(self.call_symbol, "SELL", qty)
        broker.place_order(self.put_symbol, "SELL", qty)

        self.is_position_open = False
        self.trade_completed = True
        return True

def check_existing_positions(broker):
    positions = broker.get_positions()
    if not positions:
        return None

    data = positions.get("data") if isinstance(positions, dict) else positions
    if not data:
        return None

    call_leg = None
    put_leg = None

    for row in data:
        symbol = row.get("tradingsymbol") or row.get("symbol")
        net_qty = float(row.get("netQty", 0))
        avg_price = float(row.get("avgPrice", 0))

        if not symbol or net_qty <= 0:
            continue

        if "CE" in symbol:
            call_leg = (symbol, avg_price)
        elif "PE" in symbol:
            put_leg = (symbol, avg_price)

    if not call_leg or not put_leg:
        return None

    call_symbol, call_price = call_leg
    put_symbol, put_price = put_leg

    def _strike_from_symbol(sym: str) -> int | None:
        import re
        m = re.search(r"(\d+)(CE|PE)$", sym)
        return int(m.group(1)) if m else None

    call_strike = _strike_from_symbol(call_symbol)
    put_strike = _strike_from_symbol(put_symbol)

    return (
        call_symbol,
        put_symbol,
        call_strike,
        put_strike,
        call_price,
        put_price,
    )

def monitor_exit_condition(broker, algo_trade: AlgoTrade, handler=None, check_interval: int | None = None):
    interval = check_interval if check_interval is not None else config.API_POLLING_INTERVAL

    try:
        logger.info(
            "Monitoring exit | Target profit: %s | Entry total: %s | Target exit: %s",
            config.TARGET_PROFIT_POINTS,
            algo_trade.entry_price,
            (algo_trade.entry_price + config.TARGET_PROFIT_POINTS) if algo_trade.entry_price is not None else None,
        )
        if handler:
            logger.info("Monitoring exit | Using WebSocket handler")
        else:
            logger.info("Monitoring exit | Using API polling every %ss", interval)
        while algo_trade.is_position_open:
            if handler:
                if hasattr(handler, "get_realtime_ohlcv"):
                    call_data = handler.get_realtime_ohlcv(algo_trade.call_symbol)
                    put_data = handler.get_realtime_ohlcv(algo_trade.put_symbol)
                else:
                    call_data = handler.get_latest(algo_trade.call_symbol)
                    put_data = handler.get_latest(algo_trade.put_symbol)
            else:
                call_data = get_option_ohlcv_realtime(broker, algo_trade.call_symbol)
                put_data = get_option_ohlcv_realtime(broker, algo_trade.put_symbol)

            call_ltp = _extract_ltp(call_data)
            put_ltp = _extract_ltp(put_data)

            if call_ltp is not None and put_ltp is not None:
                current_total = (call_ltp or 0) + (put_ltp or 0)
                pnl = (
                    current_total - algo_trade.entry_price
                    if algo_trade.entry_price is not None
                    else None
                )
                logger.info(
                    "Exit monitor | Call: %.2f (%s) | Put: %.2f (%s) | Total: %.2f | PnL: %.2f",
                    call_ltp,
                    algo_trade.call_strike,
                    put_ltp,
                    algo_trade.put_strike,
                    current_total,
                    pnl,
                )
                if algo_trade.check_exit(call_ltp, put_ltp):
                    algo_trade.exit_trade(broker, qty=1)
                    break
            else:
                logger.warning(
                    "Exit monitor | Missing prices | Call: %s | Put: %s",
                    call_ltp,
                    put_ltp,
                )

            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("Exit monitor interrupted by user.")
        return
    except Exception as exc:
        logger.exception("Exit monitor failed: %s", exc)
        return

def _get_avg_fill_price_from_orderbook(broker, order_id: str, timeout_sec: int = 5, poll_interval: float = 0.5) -> float | None:
    """
    Poll Angel orderBook() and return average fill price for order_id.
    """
    import time

    if not hasattr(broker, "smart_api"):
        return None

    end_time = time.time() + timeout_sec
    while time.time() < end_time:
        try:
            ob = broker.smart_api.orderBook()
            data = ob.get("data") if isinstance(ob, dict) else None
            if data:
                for row in data:
                    if str(row.get("orderid")) == str(order_id):
                        avg = row.get("averageprice") or row.get("avgprc")
                        status = (row.get("orderstatus") or "").lower()
                        if avg is not None:
                            return float(avg)
                        if "complete" in status or "filled" in status:
                            # Sometimes avg comes later; keep polling
                            pass
        except Exception:
            pass
        time.sleep(poll_interval)

    return None

    
