from __future__ import annotations

import logging
import os

try:
    from fyers_apiv3 import fyersModel
    FYERS_AVAILABLE = True
except Exception:
    fyersModel = None
    FYERS_AVAILABLE = False

try:
    from fyers_apiv3 import data_ws
except Exception:
    data_ws = None

from nifty_algo.brokers.base import BrokerInterface
from nifty_algo.config import config

logger = logging.getLogger(__name__)


class FyersBroker(BrokerInterface):
    """Fyers API broker implementation"""

    def __init__(self, client_id: str, access_token: str, log_dir: str | None = None):
        self.client_id = client_id
        self.access_token = access_token
        self.model = None
        self.clean_token = None
        self.log_dir = log_dir or os.getcwd()

    def initialize(self) -> bool:
        """Initialize Fyers API connection"""
        if not FYERS_AVAILABLE:
            raise ImportError("fyers-apiv3 package not installed. Install with: pip install fyers-apiv3")

        if not self.client_id or not self.access_token:
            raise ValueError("FYERS_CLIENT_ID and FYERS_ACCESS_TOKEN must be set in .env file")

        if ":" in self.access_token:
            token_parts = self.access_token.split(":", 1)
            self.clean_token = token_parts[1] if len(token_parts) == 2 else self.access_token
        else:
            self.clean_token = self.access_token

        self.model = fyersModel.FyersModel(
            client_id=self.client_id,
            token=self.clean_token,
            is_async=False,
            log_path=self.log_dir,
        )

        profile = self.get_profile()
        if profile.get("code") == 200 or profile.get("s") == "ok":
            return True

        raise Exception(f"Fyers API Login Failed: {profile.get('message', 'Unknown error')}")

    def get_profile(self) -> dict:
        """Get user profile"""
        return self.model.get_profile()

    def get_spot_price(self, symbol: str) -> float | None:
        """Get Nifty spot price"""
        try:
            data = {"symbols": symbol}
            response = self.model.quotes(data=data)
            if response.get("s") == "ok" and response.get("d"):
                return float(response["d"][0]["v"]["lp"])
            return None
        except Exception as exc:
            logger.error("Error fetching spot price: %s", exc)
            return None

    def get_option_quote(self, symbol: str) -> dict | None:
        """Get option quote"""
        try:
            data = {"symbols": symbol}
            response = self.model.quotes(data=data)
            if response.get("s") == "ok" and response.get("d"):
                return response["d"][0].get("v", {})
            return None
        except Exception:
            return None

    def get_positions(self) -> dict | None:
        """Get positions"""
        try:
            return self.model.positions()
        except Exception as exc:
            logger.error("Error fetching positions: %s", exc)
            return None

    def place_order(self, symbol: str, side: str, qty: int, order_type: str = "MARKET", price: float = 0.0):
        """Place order"""
        try:
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "type": 2 if order_type == "LIMIT" else 1,
                "side": 1 if side == "BUY" else -1,
                "productType": "INTRADAY",
                "limitPrice": price,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": "False",
            }
            return self.model.place_order(data=order_data)
        except Exception as exc:
            logger.error("Error placing order: %s", exc)
            return None

    def get_option_symbol(self, expiry_date, strike_price, option_type: str = "CE") -> str:
        """Generate Fyers option symbol (YYMDD format)"""
        year = expiry_date.strftime("%y")
        month = str(expiry_date.month)
        day = expiry_date.strftime("%d")
        expiry_formatted = f"{year}{month}{day}"
        return f"NSE:NIFTY{expiry_formatted}{int(strike_price)}{option_type}"

    def connect_websocket(self, symbols: list[str], handler):
        """Connect to Fyers WebSocket for real-time data"""
        if not config.USE_WEBSOCKET:
            logger.info("WebSocket disabled via USE_WEBSOCKET=false. Will use API polling instead.")
            return None, None

        if data_ws is None:
            logger.warning("Fyers WebSocket library not available. Will use API polling instead.")
            return None, None

        try:
            fyers_ws = data_ws.FyersDataSocket(
                access_token=self.clean_token,
                write_to_file=False,
                log_path="",
                on_connect=handler.on_open,
                on_close=handler.on_close,
                on_error=handler.on_error,
                on_message=handler.on_message,
            )
            fyers_ws.connect()
            fyers_ws.subscribe(symbols=symbols, data_type="symbolUpdate")
            return fyers_ws, handler
        except Exception as exc:
            logger.error("Error connecting Fyers WebSocket: %s", exc)
            logger.info("Falling back to API polling due to WebSocket connection failure.")
            return None, None
