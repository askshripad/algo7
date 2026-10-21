from __future__ import annotations

import logging
import os
from datetime import date, timedelta

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
from nifty_algo.brokers.fyers_symbol_format import build_fyers_option_symbol
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
            if response.get("s") != "ok" or not response.get("d"):
                return None
            return self._fyers_quote_v_from_response_row(response["d"][0])
        except Exception:
            return None

    @staticmethod
    def _fyers_quote_v_from_response_row(row: dict) -> dict:
        """Map one ``d[]`` element to the quote value dict (``v``), with per-row status logging."""
        if not isinstance(row, dict):
            return {}
        status = row.get("s")
        if status and str(status).lower() not in ("ok", "200", "1"):
            logger.debug(
                "Fyers quote row not ok: n=%s s=%s msg=%s",
                row.get("n"),
                status,
                row.get("message") or row.get("emsg") or row.get("message"),
            )
        v = row.get("v")
        if isinstance(v, dict):
            return v
        return {}

    def get_option_quotes_batch(self, symbols: list[str], chunk_size: int = 50) -> list[dict | None]:
        """
        Get quotes for multiple symbols. Fyers allows up to 50 symbols per request.
        Returns list of quote dicts (or None for failed/missing) in same order as symbols.
        """
        if not symbols:
            return []
        results: list[dict | None] = []
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            try:
                data = {"symbols": ",".join(chunk)}
                response = self.model.quotes(data=data)
                if response.get("s") == "ok" and response.get("d"):
                    d_list = response["d"]
                    for j, sym in enumerate(chunk):
                        if j < len(d_list):
                            results.append(d_list[j].get("v", {}))
                        else:
                            results.append(None)
                    # Help debug bad symbols: all quotes empty / no lp
                    if chunk and d_list:
                        def _has_lp(v: dict | None) -> bool:
                            if not v:
                                return False
                            return (
                                v.get("lp") is not None
                                or v.get("ltp") is not None
                                or v.get("last_price") is not None
                            )

                        if not any(
                            _has_lp(d_list[j].get("v") if j < len(d_list) else None)
                            for j in range(min(len(chunk), len(d_list)))
                        ):
                            r0 = d_list[0] if d_list else {}
                            logger.warning(
                                "Fyers quotes: no lp/ltp in chunk (first_symbol=%s quote_v=%s)",
                                chunk[0],
                                r0.get("v") if isinstance(r0, dict) else r0,
                            )
                else:
                    logger.warning(
                        "Fyers quotes batch failed: s=%s msg=%s first_symbol=%s",
                        response.get("s"),
                        response.get("message") or response.get("msg"),
                        chunk[0] if chunk else None,
                    )
                    results.extend([None] * len(chunk))
            except Exception as exc:
                logger.debug("Fyers batch quote error: %s", exc)
                results.extend([None] * len(chunk))
        return results

    def get_positions(self) -> dict | None:
        """Get positions"""
        try:
            return self.model.positions()
        except Exception as exc:
            logger.error("Error fetching positions: %s", exc)
            return None

    def place_order(self, symbol: str, side: str, qty: int, order_type: str = "MARKET", price: float = 0.0):
        """Place order. Fyers API: type 1=Limit, 2=Market (see fyersModel.place_order docstring)."""
        try:
            order_data = {
                "symbol": symbol,
                "qty": qty,
                "type": 1 if order_type == "LIMIT" else 2,
                "side": 1 if side == "BUY" else -1,
                "productType": "INTRADAY",
                "limitPrice": price,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
            }
            resp = self.model.place_order(data=order_data)
            logger.info("Fyers place_order %s %s qty=%s | response=%s", side, symbol, qty, resp)
            if isinstance(resp, dict) and resp.get("code") != 200:
                logger.error("Fyers order rejected: %s", resp)
            return resp
        except Exception as exc:
            logger.error("Error placing order: %s", exc)
            return None

    def get_nearest_expiry(self) -> date | None:
        """
        Return the nearest Nifty weekly option expiry using ``NIFTY_WEEKLY_EXPIRY_WEEKDAY``
        from config (default Tuesday). For a specific calendar date, set ``NIFTY_EXPIRY_DATE`` in ``.env``;
        ``market.expiry.get_nearest_expiry`` applies that before calling the broker.
        """
        w = config.NIFTY_WEEKLY_EXPIRY_WEEKDAY
        if not (0 <= w <= 6):
            w = 1
        today = date.today()
        days_ahead = w - today.weekday()
        if days_ahead < 0:
            days_ahead += 7
        return today + timedelta(days=days_ahead)

    def get_option_symbol(self, expiry_date, strike_price, option_type: str = "CE") -> str:
        """NSE:NIFTY ticker from expiry date (auto weekly vs monthly; see ``fyers_symbol_format``)."""
        return build_fyers_option_symbol(expiry_date, int(strike_price), option_type)

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
