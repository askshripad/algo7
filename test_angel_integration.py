import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from nifty_algo.config import config
from nifty_algo.brokers import get_broker
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ====== EDIT THESE ======
CONFIRM_LIVE = True
USE_OPTION = True  # True = option order, False = cash equity

# Cash instrument (used when USE_OPTION = False)
INSTRUMENT = "NSE:SBIN"

# Option details (used when USE_OPTION = True)
OPTION_EXPIRY = "2026-01-27"  # YYYY-MM-DD
OPTION_STRIKE = 25000
OPTION_TYPE = "CE"  # CE or PE

QTY = 65
ORDER_TIMEOUT_SEC = 8
POLL_INTERVAL_SEC = 0.5
# ========================

def _resolve_cash_symbol(broker, symbol: str):
    exchange = "NSE"
    query = symbol.replace("NSE:", "")
    resp = broker.smart_api.searchScrip(exchange=exchange, searchscrip=query)
    data = resp.get("data") if isinstance(resp, dict) else None
    if not data:
        return None, None

    # Prefer EQ series
    for row in data:
        ts = (row.get("tradingsymbol") or "").upper()
        if ts.endswith("-EQ") or row.get("instrumenttype") == "EQ":
            return row.get("tradingsymbol"), row.get("symboltoken")

    # Fallback: exact symbolname match
    for row in data:
        if (row.get("symbolname") or "").upper() == query.upper():
            return row.get("tradingsymbol"), row.get("symboltoken")

    return None, None


def _place_cash_order(broker, symbol: str, side: str, qty: int):
    tradingsymbol, symboltoken = _resolve_cash_symbol(broker, symbol)
    if not tradingsymbol or not symboltoken:
        logger.error("Failed to resolve symboltoken for %s", symbol)
        return None

    orderparams = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": side,
        "exchange": "NSE",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "quantity": str(qty),
    }
    return broker.smart_api.placeOrder(orderparams)


def _place_option_order(broker, expiry: str, strike: int, opt_type: str, side: str, qty: int):
    expiry_dt = datetime.strptime(expiry, "%Y-%m-%d")
    option_symbol = broker.get_option_symbol(expiry_dt,strike, opt_type)
    print("Option symbol:", option_symbol)
    token = broker._get_option_token(option_symbol)
    print("Resolved token:", token)
    return broker.place_order(option_symbol, side, qty)

def _find_order(order_book: dict, order_id: str) -> dict | None:
    data = order_book.get("data") if isinstance(order_book, dict) else None
    if not data:
        return None
    for row in data:
        if str(row.get("orderid")) == str(order_id):
            return row
    return None


def _wait_for_order(broker, order_id: str) -> dict | None:
    end_time = time.time() + ORDER_TIMEOUT_SEC
    while time.time() < end_time:
        try:
            ob = broker.smart_api.orderBook()
            row = _find_order(ob, order_id)
            if row:
                return row
        except Exception as exc:
            logger.warning("orderBook failed: %s", exc)
        time.sleep(POLL_INTERVAL_SEC)
    return None


def main():
    if not CONFIRM_LIVE:
        logger.error("Set CONFIRM_LIVE = True to run live orders.")
        return

    if config.BROKER.lower() != "angel":
        logger.error("Set BROKER=angel in your environment.")
        return

    broker = get_broker(config)
    broker.initialize()

    if USE_OPTION:
        label = f"OPTION {OPTION_EXPIRY} {OPTION_STRIKE} {OPTION_TYPE}"
        logger.info("Placing BUY order for %s qty=%s", label, QTY)
        buy_resp = _place_option_order(
            broker,
            OPTION_EXPIRY,
            OPTION_STRIKE,
            OPTION_TYPE,
            "BUY",
            QTY,
        )
    else:
        logger.info("Placing BUY order for %s qty=%s", INSTRUMENT, QTY)
        buy_resp = _place_cash_order(broker, INSTRUMENT, "BUY", QTY)
    buy_order_id = str(buy_resp) if buy_resp is not None else None
    logger.info("BUY response: %s", buy_resp)

    if not buy_order_id:
        logger.error("No order id returned for BUY.")
        return

    buy_details = _wait_for_order(broker, buy_order_id)
    logger.info("BUY order details: %s", buy_details)

    if USE_OPTION:
        label = f"OPTION {OPTION_EXPIRY} {OPTION_STRIKE} {OPTION_TYPE}"
        logger.info("Placing SELL order for %s qty=%s", label, QTY)
        sell_resp = _place_option_order(
            broker,
            OPTION_EXPIRY,
            OPTION_STRIKE,
            OPTION_TYPE,
            "SELL",
            QTY,
        )
    else:
        logger.info("Placing SELL order for %s qty=%s", INSTRUMENT, QTY)
        sell_resp = _place_cash_order(broker, INSTRUMENT, "SELL", QTY)
    sell_order_id = str(sell_resp) if sell_resp is not None else None
    logger.info("SELL response: %s", sell_resp)

    if not sell_order_id:
        logger.error("No order id returned for SELL.")
        return

    sell_details = _wait_for_order(broker, sell_order_id)
    logger.info("SELL order details: %s", sell_details)


if __name__ == "__main__":
    main()
